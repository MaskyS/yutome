from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from yutome.channels import LibraryChannel
from yutome.config import AppConfig, GeminiConfig, ProxyConfig
from yutome.hashing import sha256_json
from yutome.hosted.control_plane import Job, Source
from yutome.hosted.gate import UsageGate
from yutome.hosted.mcp_query import HostedMcpUsageContext
from yutome.hosted.models import (
    EntitlementPolicy,
    ProviderAllocation,
    UsageDecision,
    UsageEvent,
    UsageNormalization,
    UsageReservation,
    WorkspaceBalance,
)
from yutome.hosted.provider_wrappers import ProviderCallContext, UsageReservationDenied, execute_provider_call
from yutome.hosted.youtube_oauth_service import HostedYouTubeOAuthError
from yutome.hosted.indexing import (
    DEFAULT_EMBEDDING_DIMENSION,
    HostedIndexingError,
    HostedIndexingExecutor,
    HostedSourceDiscoveryExecutor,
    HostedVideoInput,
    IndexProfileInput,
    TranscriptChunkInput,
    _hosted_ytdlp_published_at,
    plan_real_hosted_public_indexing,
    source_from_public_youtube_input,
)
from yutome.youtube import DiscoveredVideo, TranscriptFetchResult


NOW = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
ALWAYS_CLEAN_SOURCE_METADATA = {"gemini_cleanup": "always"}
NEVER_CLEAN_SOURCE_METADATA = {"gemini_cleanup": "never"}


def _jsonb_obj(value: object) -> object:
    assert isinstance(value, Jsonb)
    return value.obj


class RecordingGate(UsageGate):
    def __init__(self, order: list[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.order = order

    def reserve(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(dict(kwargs))
        if self.order is not None:
            self.order.append(f"reserve:{kwargs['subject']}.{kwargs['operation']}")
        return super().reserve(**kwargs)


class RecordingLedger:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    def append(self, event: UsageEvent) -> None:
        self.events.append(event)


class HostedExecutorConnection:
    def __init__(
        self,
        *,
        policy: EntitlementPolicy,
        balance: WorkspaceBalance | None = None,
        source_metadata_json: dict[str, Any] | None = None,
    ) -> None:
        self.policy = policy
        self.balance = balance or WorkspaceBalance(
            workspace_id="ws_alice",
            remaining_units={
                "total_tokens": 100_000,
                "vectors": 100,
                "transcript_versions": 10,
                "chunks": 100,
                "embeddings": 100,
            },
        )
        self.source_metadata_json = dict(source_metadata_json or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.transaction_events: list[str] = []

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((statement, dict(params or {})))
        if "UPDATE jobs" in statement and "lease_expires_at = %(lease_expires_at)s" in statement:
            return [{"id": (params or {}).get("job_id"), "lease_owner": (params or {}).get("lease_owner")}]
        if "SELECT id" in statement and "FROM jobs" in statement and "FOR UPDATE" in statement:
            return [{"id": (params or {}).get("job_id")}]
        if "FROM sources" in statement:
            return [
                {
                    "id": "src_oedo",
                    "workspace_id": "ws_alice",
                    "source_type": "video",
                    "source_url": "https://www.youtube.com/watch?v=OEDoJyhQhXs",
                    "canonical_video_id": "OEDoJyhQhXs",
                    "display_name": "Real-world smoke video",
                    "selected": True,
                    "auto_index_allowed": True,
                    "import_source": "manual_url",
                    "metadata_json": self.source_metadata_json,
                    "status": "active",
                }
            ]
        if "FROM workspaces" in statement:
            return [{"subscription_status": "trialing", "trial_ends_at": "2999-01-01T00:00:00+00:00"}]
        if "FROM provider_allocations" in statement:
            return [
                {
                    "id": "alloc_gemini",
                    "workspace_id": "ws_alice",
                    "provider": "gemini",
                    "operation": "cleanup_transcript",
                    "credential_mode": "hosted",
                    "status": "active",
                    "model_or_plan": "gemini-3.1-flash-lite",
                    "metadata_json": {},
                },
                {
                    "id": "alloc_voyage",
                    "workspace_id": "ws_alice",
                    "provider": "voyage",
                    "operation": "embed_documents",
                    "credential_mode": "hosted",
                    "status": "active",
                    "model_or_plan": "voyage-4-lite",
                    "metadata_json": {},
                },
            ]
        if "FROM service_allocations" in statement:
            return [
                {
                    "id": "svc_search",
                    "workspace_id": "ws_alice",
                    "service": "search_store",
                    "operation": "index_write",
                    "credential_mode": "service_internal",
                    "status": "active",
                    "backend": "postgres_vectorchord",
                    "index_profile_ref": "sip_voyage4lite_bm25_default",
                    "metadata_json": {},
                }
            ]
        if "FROM entitlement_policies" in statement:
            return [
                {
                    "id": self.policy.id,
                    "workspace_id": self.policy.workspace_id,
                    "allowed_operations": list(self.policy.allowed_operations),
                    "hard_limits_jsonb": self.policy.hard_limits_by_operation,
                    "soft_limits_jsonb": self.policy.soft_limits_by_operation,
                }
            ]
        if "FROM workspace_balances" in statement:
            return [
                {
                    "workspace_id": self.balance.workspace_id,
                    "entitlement_policy_id": self.policy.id,
                    "remaining_units_jsonb": self.balance.remaining_units,
                    "reserved_units_jsonb": {},
                    "unlimited_units": list(self.balance.unlimited_units),
                }
            ]
        if "UPDATE workspace_balances" in statement:
            self.balance = WorkspaceBalance(
                workspace_id=self.balance.workspace_id,
                remaining_units=_jsonb_obj((params or {})["remaining_units_jsonb"]),
                unlimited_units=self.balance.unlimited_units,
            )
            return [
                {
                    "workspace_id": self.balance.workspace_id,
                    "entitlement_policy_id": self.policy.id,
                    "remaining_units_jsonb": self.balance.remaining_units,
                    "reserved_units_jsonb": _jsonb_obj((params or {})["reserved_units_jsonb"]),
                    "unlimited_units": list(self.balance.unlimited_units),
                }
            ]
        if "UPDATE job_operations" in statement:
            return [{"id": (params or {}).get("operation_id"), "status": (params or {}).get("status", "succeeded")}]
        if "FROM usage_events" in statement and "provider_attempt_succeeded" in statement:
            return []
        return []

    def transaction(self):
        connection = self

        class Tx:
            def __enter__(self) -> None:
                connection.transaction_events.append("begin")

            def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
                connection.transaction_events.append("rollback" if exc_type else "commit")

        return Tx()


def _executor_job() -> Job:
    return Job(
        id="job_oedo_index",
        workspace_id="ws_alice",
        source_id="src_oedo",
        job_type="index_video",
        status="queued",
        idempotency_key="ws_alice:src_oedo:index_video:real",
        lease_owner="worker-1",
        created_at=NOW,
    )


def _executor_policy(
    limits: dict[str, dict[str, float]] | None = None,
    soft_limits: dict[str, dict[str, float]] | None = None,
) -> EntitlementPolicy:
    return EntitlementPolicy(
        id="policy_ws_alice",
        workspace_id="ws_alice",
        allowed_operations={"gemini.cleanup_transcript", "voyage.embed_documents", "search_store.index_write"},
        hard_limits_by_operation=limits or {},
        soft_limits_by_operation=soft_limits or {},
    )


def _webshare_config() -> AppConfig:
    return AppConfig(
        proxy=ProxyConfig(
            enabled=True,
            kind="webshare",
            webshare_username="proxy-user",
            webshare_password="proxy-pass",
        )
    )


class AllowAnyProviderUsage:
    def for_subject(self, *, auth, subject, operation, estimated_units):  # noqa: ANN001, ANN202
        return HostedMcpUsageContext(
            allocation=ProviderAllocation(
                id=f"alloc_{subject}_{operation}",
                workspace_id=auth.workspace_id,
                provider=subject,
                operation=operation,
            ),
            policy=EntitlementPolicy(
                id=f"policy_{subject}_{operation}",
                workspace_id=auth.workspace_id,
                allowed_operations={f"{subject}.{operation}"},
            ),
            balance=WorkspaceBalance(
                workspace_id=auth.workspace_id,
                remaining_units={key: 1_000_000_000 for key in estimated_units},
            ),
        )


def _metadata_fetcher(video_id: str, _source: Source, _job: Job) -> HostedVideoInput:
    return HostedVideoInput(
        youtube_video_id=video_id,
        title="Real hosted executor video",
        url=f"https://www.youtube.com/watch?v={video_id}",
        channel_id="UCleoandlongevity",
        duration_seconds=60,
        metadata={"channel_title": "Leo and Longevity"},
    )


def test_hosted_ytdlp_published_at_prefers_exact_date_fields_then_timestamp() -> None:
    assert _hosted_ytdlp_published_at(
        {"upload_date": "20220201", "release_date": "20210101", "timestamp": 100}
    ) == datetime(2022, 2, 1, tzinfo=timezone.utc)
    assert _hosted_ytdlp_published_at({"release_date": "20210101", "timestamp": 100}) == datetime(
        2021, 1, 1, tzinfo=timezone.utc
    )
    assert _hosted_ytdlp_published_at({"modified_date": "20200102", "timestamp": 100}) == datetime(
        2020, 1, 2, tzinfo=timezone.utc
    )
    assert _hosted_ytdlp_published_at({"timestamp": 1643750702}) == datetime.fromtimestamp(
        1643750702, tz=timezone.utc
    )


def _transcript_fetcher(_video_id: str, _source: Source, _job: Job) -> TranscriptFetchResult:
    return TranscriptFetchResult(
        raw_snippets=[
            {"start": 0.0, "duration": 4.0, "text": "Hosted indexing fetches a real YouTube transcript."},
            {"start": 4.0, "duration": 5.0, "text": "Gemini cleanup and Voyage embeddings are metered before writes."},
        ],
        source="youtube-transcript-api",
        language="en",
        is_generated=False,
    )


def _fake_gemini_cleaner(calls: list[str]):
    def clean(transcript, _video: HostedVideoInput, context: ProviderCallContext):  # noqa: ANN001, ANN202
        def call():
            calls.append("gemini")
            return transcript

        return execute_provider_call(
            context,
            call,
            normalize_usage=lambda _result: UsageNormalization(
                subject="gemini",
                operation="cleanup_transcript",
                actual_units={"total_tokens": 8},
                provider_request_id="gemini_req_1",
            ),
        )

    return clean


def _fake_voyage_embedder(calls: list[str]):
    def embed(chunks: list[TranscriptChunkInput], _video: HostedVideoInput, context: ProviderCallContext) -> list[list[float]]:
        def call():
            calls.append("voyage")
            return [[0.001 * (index + 1)] * DEFAULT_EMBEDDING_DIMENSION for index, _chunk in enumerate(chunks)]

        return execute_provider_call(
            context,
            call,
            normalize_usage=lambda result: UsageNormalization(
                subject="voyage",
                operation="embed_documents",
                actual_units={"total_tokens": 12, "vectors": len(result)},
                provider_request_id="voyage_req_1",
            ),
        )

    return embed


def _source() -> Source:
    return source_from_public_youtube_input(
        workspace_id="ws_alice",
        source_id="src_oedo",
        value="https://www.youtube.com/watch?v=OEDoJyhQhXs",
        display_name="Real-world indexing fixture",
    )


def _corrupt_transcript_fetcher(_video_id: str, _source: Source, _job: Job) -> TranscriptFetchResult:
    return TranscriptFetchResult(
        raw_snippets=[
            {"start": 0.0, "duration": 2.0, "text": "my brain is so scatterrained these days"},
            {"start": 2.0, "duration": 2.0, "text": "I could publish the eepop format"},
            {"start": 4.0, "duration": 2.0, "text": "the essay is about braed loops"},
        ],
        source="youtube-transcript-api",
        language="en",
        is_generated=True,
    )


def _job(source: Source) -> Job:
    return Job(
        id="job_oedo_index",
        workspace_id=source.workspace_id,
        source_id=source.id,
        job_type="index_video",
        status="queued",
        idempotency_key="ws_alice:src_oedo:index_video",
        created_at=NOW,
    )


def _video() -> HostedVideoInput:
    return HostedVideoInput(
        youtube_video_id="OEDoJyhQhXs",
        title="Hosted public indexing fixture",
        url="https://www.youtube.com/watch?v=OEDoJyhQhXs",
        channel_id="UCleoandlongevity",
        duration_seconds=1200,
        metadata={"source_parse": "real_world_url"},
    )


def _chunks() -> list[TranscriptChunkInput]:
    return [
        TranscriptChunkInput(
            chunk_index=0,
            start_seconds=0,
            end_seconds=12.5,
            text="Hosted indexing should write transcript chunks and embeddings.",
        ),
        TranscriptChunkInput(
            chunk_index=1,
            start_seconds=12.5,
            end_seconds=25,
            text="Hybrid search should be queryable from generated Postgres operations.",
        ),
    ]


def _embedding_vector(text: str, dimension: int = DEFAULT_EMBEDDING_DIMENSION) -> list[float]:
    seed = (sum(ord(char) for char in text) % 1000) / 1000
    return [seed] * dimension


def _embedding_vectors(chunks: list[TranscriptChunkInput]) -> list[list[float]]:
    return [_embedding_vector(chunk.text) for chunk in chunks]


def _reservation_grants(workspace_id: str) -> tuple[EntitlementPolicy, WorkspaceBalance]:
    return (
        EntitlementPolicy(
            id=f"policy_{workspace_id}",
            workspace_id=workspace_id,
            allowed_operations={
                "voyage.embed_documents",
                "search_store.index_write",
                "search_store.hybrid_query",
            },
        ),
        WorkspaceBalance(
            workspace_id=workspace_id,
            remaining_units={
                "total_tokens": 10_000,
                "vectors": 100,
                "transcript_versions": 10,
                "chunks": 100,
                "embeddings": 100,
                "queries": 10,
                "candidate_limit": 100,
                "query_vector_dimensions": 4096,
            },
        ),
    )


def test_real_world_youtube_url_and_handle_parse_without_media_fetch() -> None:
    video_source = _source()
    handle_source = source_from_public_youtube_input(
        workspace_id="ws_alice",
        source_id="src_leo_handle",
        value="leoandlongevity",
    )
    playlist_source = source_from_public_youtube_input(
        workspace_id="ws_alice",
        source_id="src_playlist",
        value="https://www.youtube.com/playlist?list=PL1234567890",
    )
    channel_source = source_from_public_youtube_input(
        workspace_id="ws_alice",
        source_id="src_channel",
        value="https://www.youtube.com/channel/UC1234567890123456789012",
    )

    assert video_source.is_public_source is True
    assert video_source.source_type == "video"
    assert video_source.canonical_video_id == "OEDoJyhQhXs"
    assert video_source.source_url == "https://www.youtube.com/watch?v=OEDoJyhQhXs"
    assert handle_source.is_public_source is True
    assert handle_source.source_type == "handle"
    assert handle_source.source_url == "https://www.youtube.com/@leoandlongevity"
    assert playlist_source.source_type == "playlist"
    assert playlist_source.canonical_playlist_id == "PL1234567890"
    assert channel_source.source_type == "channel"
    assert channel_source.canonical_channel_id == "UC1234567890123456789012"


def test_real_hosted_indexing_plan_is_idempotent_for_same_transcript() -> None:
    source = _source()
    job = _job(source)
    chunks = _chunks()

    left = plan_real_hosted_public_indexing(
        source=source,
        job=job,
        video=_video(),
        chunks=chunks,
        embedding_vectors=_embedding_vectors(chunks),
        transcript_source="youtube_transcript",
        language_code="en",
    )
    right = plan_real_hosted_public_indexing(
        source=source,
        job=job,
        video=_video(),
        chunks=list(reversed(chunks)),
        embedding_vectors=list(reversed(_embedding_vectors(chunks))),
        transcript_source="youtube_transcript",
        language_code="en",
    )

    assert left.hosted_video_id == right.hosted_video_id
    assert left.transcript_version_id == right.transcript_version_id
    assert left.transcript_content_hash == right.transcript_content_hash
    assert [operation.name for operation in left.sql_operations] == [operation.name for operation in right.sql_operations]
    assert left.job_operations == ()
    assert left.usage_reservations == ()


def test_real_hosted_indexing_defaults_to_fixed_hosted_vector_dimension() -> None:
    source = _source()
    chunks = _chunks()

    plan = plan_real_hosted_public_indexing(
        source=source,
        job=_job(source),
        video=_video(),
        chunks=chunks,
        embedding_vectors=_embedding_vectors(chunks),
        transcript_source="youtube_transcript",
        language_code="en",
    )
    embedding_statement = next(operation.statement for operation in plan.sql_operations if operation.name == "chunk_embeddings.upsert")
    embedding_vector = embedding_statement.params["embedding"].strip("[]").split(",")

    assert DEFAULT_EMBEDDING_DIMENSION == 1024
    assert len(_embedding_vector("hosted vector contract")) == 1024
    assert plan.index_profile.embedding_dimension == 1024
    assert embedding_statement.params["index_profile_id"].startswith("sip_")
    assert embedding_statement.params["index_profile_id"] != "sip_voyage4lite_bm25_default"
    assert len(embedding_vector) == 1024


def test_default_index_profile_ids_are_workspace_scoped_for_search_joins() -> None:
    source = _source()
    other_source = _source().model_copy(update={"workspace_id": "ws_bob", "id": "src_bob"})

    first = plan_real_hosted_public_indexing(
        source=source,
        job=_job(source),
        video=_video(),
        chunks=_chunks(),
        embedding_vectors=_embedding_vectors(_chunks()),
        transcript_source="youtube_transcript",
        language_code="en",
    )
    second = plan_real_hosted_public_indexing(
        source=other_source,
        job=_job(other_source),
        video=_video(),
        chunks=_chunks(),
        embedding_vectors=_embedding_vectors(_chunks()),
        transcript_source="youtube_transcript",
        language_code="en",
    )
    first_profile_upsert = next(operation.statement for operation in first.sql_operations if operation.name == "search_index_profiles.upsert")
    first_chunk_upsert = next(operation.statement for operation in first.sql_operations if operation.name == "chunks.upsert")

    assert first.index_profile.id != second.index_profile.id
    assert first.index_profile.id.startswith("sip_")
    assert first.index_profile.id != "sip_voyage4lite_bm25_default"
    assert first_profile_upsert.params["workspace_id"] == source.workspace_id
    assert first_profile_upsert.params["id"] == first.index_profile.id
    assert first_chunk_upsert.params["index_profile_id"] == first.index_profile.id


def test_real_hosted_indexing_rejects_unsupported_embedding_profile() -> None:
    source = _source()

    with pytest.raises(ValueError, match=r"unsupported embedding profile .*vector\(1024\)"):
        plan_real_hosted_public_indexing(
            source=source,
            job=_job(source),
            video=_video(),
            chunks=_chunks(),
            embedding_vectors=[[0.1] * 8 for _chunk in _chunks()],
            index_profile=IndexProfileInput(embedding_dimension=8),
            transcript_source="youtube_transcript",
            language_code="en",
        )


def test_public_source_validity_is_enforced_before_planning() -> None:
    source = _source().model_copy(update={"status": "disabled"})

    with pytest.raises(ValueError, match="source is not public and discoverable"):
        plan_real_hosted_public_indexing(
            source=source,
            job=_job(source),
            video=_video(),
            chunks=_chunks(),
            embedding_vectors=_embedding_vectors(_chunks()),
            transcript_source="youtube_transcript",
            language_code="en",
        )

    oauth_source = Source(
        id="src_oauth_subs",
        workspace_id="ws_alice",
        source_type="subscriptions",
        source_url="youtube://subscriptions/mine",
        import_source="youtube_oauth",
        auth_grant_id="yt_grant_alice",
    )
    with pytest.raises(ValueError, match="source is not public and discoverable"):
        plan_real_hosted_public_indexing(
            source=oauth_source,
            job=_job(oauth_source),
            video=_video(),
            chunks=_chunks(),
            embedding_vectors=_embedding_vectors(_chunks()),
            transcript_source="youtube_transcript",
            language_code="en",
        )


def test_generated_postgres_and_search_store_operations_are_queryable() -> None:
    source = _source()
    chunks = _chunks()
    plan = plan_real_hosted_public_indexing(
        source=source,
        job=_job(source),
        video=_video(),
        chunks=chunks,
        embedding_vectors=_embedding_vectors(chunks),
        transcript_source="youtube_transcript",
        language_code="en",
    )

    operation_names = [operation.name for operation in plan.sql_operations]

    assert operation_names[:2] == ["videos.upsert", "search_index_profiles.upsert"]
    assert "transcript_versions.upsert_replacement" in operation_names
    assert "search_store.replace_active_transcript" in operation_names
    assert operation_names.count("chunks.upsert") == 2
    assert operation_names.count("chunk_embeddings.upsert") == 2
    transcript_index = operation_names.index("transcript_versions.upsert_replacement")
    swap_index = operation_names.index("search_store.replace_active_transcript")
    chunk_indexes = [index for index, name in enumerate(operation_names) if name == "chunks.upsert"]
    embedding_indexes = [index for index, name in enumerate(operation_names) if name == "chunk_embeddings.upsert"]
    assert transcript_index < min(chunk_indexes)
    assert swap_index > max([*chunk_indexes, *embedding_indexes])
    assert plan.search_operations == ()


def test_hosted_executor_skips_gemini_cleanup_when_quality_is_ok() -> None:
    provider_calls: list[str] = []
    connection = HostedExecutorConnection(policy=_executor_policy())
    executor = HostedIndexingExecutor(
        connection=connection,
        config=AppConfig(),
        gate=UsageGate(),
        ledger=RecordingLedger(),
        metadata_fetcher=_metadata_fetcher,
        transcript_fetcher=_transcript_fetcher,
        gemini_cleaner=_fake_gemini_cleaner(provider_calls),
        voyage_embedder=_fake_voyage_embedder(provider_calls),
    )

    result = executor.execute(_executor_job(), lease_owner="worker-1", now=NOW)
    inserted_operations = [
        params.get("operation")
        for statement, params in connection.calls
        if statement.startswith("INSERT INTO job_operations")
    ]

    assert result.status == "succeeded"
    assert provider_calls == ["voyage"]
    assert result.chunks_written >= 1
    assert "gemini.cleanup_transcript" not in inserted_operations


def test_hosted_executor_runs_gemini_cleanup_when_source_opts_in_always() -> None:
    provider_calls: list[str] = []
    connection = HostedExecutorConnection(
        policy=_executor_policy(),
        source_metadata_json=ALWAYS_CLEAN_SOURCE_METADATA,
    )
    executor = HostedIndexingExecutor(
        connection=connection,
        config=AppConfig(),
        gate=UsageGate(),
        ledger=RecordingLedger(),
        metadata_fetcher=_metadata_fetcher,
        transcript_fetcher=_transcript_fetcher,
        gemini_cleaner=_fake_gemini_cleaner(provider_calls),
        voyage_embedder=_fake_voyage_embedder(provider_calls),
    )

    result = executor.execute(_executor_job(), lease_owner="worker-1", now=NOW)

    assert result.status == "succeeded"
    assert provider_calls == ["gemini", "voyage"]


def test_hosted_executor_skips_gemini_cleanup_when_source_opts_out_never() -> None:
    provider_calls: list[str] = []
    connection = HostedExecutorConnection(
        policy=_executor_policy(),
        source_metadata_json=NEVER_CLEAN_SOURCE_METADATA,
    )
    executor = HostedIndexingExecutor(
        connection=connection,
        config=AppConfig(),
        gate=UsageGate(),
        ledger=RecordingLedger(),
        metadata_fetcher=_metadata_fetcher,
        transcript_fetcher=_corrupt_transcript_fetcher,
        gemini_cleaner=_fake_gemini_cleaner(provider_calls),
        voyage_embedder=_fake_voyage_embedder(provider_calls),
    )

    result = executor.execute(_executor_job(), lease_owner="worker-1", now=NOW)

    assert result.status == "succeeded"
    assert provider_calls == ["voyage"]


def test_hosted_executor_runs_gemini_cleanup_when_quality_needs_cleanup_in_auto() -> None:
    provider_calls: list[str] = []
    connection = HostedExecutorConnection(policy=_executor_policy())
    executor = HostedIndexingExecutor(
        connection=connection,
        config=AppConfig(),
        gate=UsageGate(),
        ledger=RecordingLedger(),
        metadata_fetcher=_metadata_fetcher,
        transcript_fetcher=_corrupt_transcript_fetcher,
        gemini_cleaner=_fake_gemini_cleaner(provider_calls),
        voyage_embedder=_fake_voyage_embedder(provider_calls),
    )

    result = executor.execute(_executor_job(), lease_owner="worker-1", now=NOW)

    assert result.status == "succeeded"
    assert provider_calls == ["gemini", "voyage"]


def test_real_hosted_executor_orders_provider_calls_before_transactional_writes() -> None:
    provider_calls: list[str] = []
    ledger = RecordingLedger()
    connection = HostedExecutorConnection(
        policy=_executor_policy(),
        source_metadata_json=ALWAYS_CLEAN_SOURCE_METADATA,
    )
    executor = HostedIndexingExecutor(
        connection=connection,
        config=AppConfig(),
        gate=UsageGate(),
        ledger=ledger,
        metadata_fetcher=_metadata_fetcher,
        transcript_fetcher=_transcript_fetcher,
        gemini_cleaner=_fake_gemini_cleaner(provider_calls),
        voyage_embedder=_fake_voyage_embedder(provider_calls),
    )

    result = executor.execute(_executor_job(), lease_owner="worker-1", now=NOW)
    statements = [sql for sql, _params in connection.calls]
    first_write_index = next(index for index, statement in enumerate(statements) if statement.startswith("INSERT INTO videos"))
    transaction_sql = statements[first_write_index:]
    operation_reservation_ids = [
        params.get("usage_reservation_id")
        for statement, params in connection.calls
        if statement.startswith("INSERT INTO job_operations")
    ]
    output_reservation_ids = [
        params.get("usage_reservation_id")
        for statement, params in connection.calls
        if statement.startswith("UPDATE job_operations") and "output_json" in statement
    ]
    embedding_metadata = [
        _jsonb_obj(params["metadata_json"])
        for statement, params in connection.calls
        if statement.startswith("INSERT INTO chunk_embeddings")
    ]
    chunk_params = [params for statement, params in connection.calls if statement.startswith("INSERT INTO chunks")]

    assert result.status == "succeeded"
    assert result.chunks_written == 1
    assert result.embeddings_written == 1
    assert provider_calls == ["gemini", "voyage"]
    assert len(operation_reservation_ids) == 3
    assert operation_reservation_ids[:2] == [None, None]
    assert operation_reservation_ids[2] and str(operation_reservation_ids[2]).startswith("res_")
    assert output_reservation_ids == [None, None]
    assert embedding_metadata[0]["usage_reservation_id"].startswith("res_")
    assert chunk_params[0]["tokenizer"] == "yutome_llmlingua2"
    assert connection.transaction_events == ["begin", "commit", "begin", "commit", "begin", "commit"]
    assert "INSERT INTO videos" in "\n".join(transaction_sql)
    assert "INSERT INTO chunks" in "\n".join(transaction_sql)
    assert "INSERT INTO chunk_embeddings" in "\n".join(transaction_sql)
    assert any(params.get("status") == "succeeded" for _sql, params in connection.calls)
    index_events = [event for event in ledger.events if event.operation_key == "search_store.index_write"]
    assert index_events[-1].status == "succeeded"
    assert index_events[-1].event_type == "service_operation_succeeded"


def test_real_hosted_executor_reserves_usage_before_paid_calls_and_index_writes() -> None:
    order: list[str] = []

    class OrderedConnection(HostedExecutorConnection):
        def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            if statement.startswith("INSERT INTO videos"):
                order.append("write:videos")
            return super().execute(statement, params)

    def cleaner(transcript, _video: HostedVideoInput, context: ProviderCallContext):  # noqa: ANN001, ANN202
        def call():
            order.append("call:gemini.cleanup_transcript")
            return transcript

        return execute_provider_call(
            context,
            call,
            normalize_usage=lambda _result: UsageNormalization(
                subject="gemini",
                operation="cleanup_transcript",
                actual_units={"total_tokens": 8},
            ),
        )

    def embedder(chunks: list[TranscriptChunkInput], _video: HostedVideoInput, context: ProviderCallContext) -> list[list[float]]:
        def call() -> list[list[float]]:
            order.append("call:voyage.embed_documents")
            return [[0.001 * (index + 1)] * DEFAULT_EMBEDDING_DIMENSION for index, _chunk in enumerate(chunks)]

        return execute_provider_call(
            context,
            call,
            normalize_usage=lambda result: UsageNormalization(
                subject="voyage",
                operation="embed_documents",
                actual_units={"total_tokens": 12, "vectors": len(result)},
            ),
        )

    connection = OrderedConnection(
        policy=_executor_policy(),
        source_metadata_json=ALWAYS_CLEAN_SOURCE_METADATA,
    )
    executor = HostedIndexingExecutor(
        connection=connection,
        config=AppConfig(),
        gate=RecordingGate(order),
        ledger=RecordingLedger(),
        metadata_fetcher=_metadata_fetcher,
        transcript_fetcher=_transcript_fetcher,
        gemini_cleaner=cleaner,
        voyage_embedder=embedder,
    )

    result = executor.execute(_executor_job(), lease_owner="worker-1", now=NOW)

    assert result.status == "succeeded"
    assert order.index("reserve:gemini.cleanup_transcript") < order.index("call:gemini.cleanup_transcript")
    assert order.index("reserve:voyage.embed_documents") < order.index("call:voyage.embed_documents")
    assert order.index("reserve:search_store.index_write") < order.index("write:videos")


def test_real_hosted_executor_denies_before_gemini_or_voyage_provider_calls() -> None:
    provider_calls: list[str] = []
    policy = _executor_policy({"gemini.cleanup_transcript": {"total_tokens": 1}})
    connection = HostedExecutorConnection(policy=policy, source_metadata_json=ALWAYS_CLEAN_SOURCE_METADATA)
    executor = HostedIndexingExecutor(
        connection=connection,
        config=AppConfig(),
        gate=UsageGate(),
        ledger=RecordingLedger(),
        metadata_fetcher=_metadata_fetcher,
        transcript_fetcher=_transcript_fetcher,
        gemini_cleaner=_fake_gemini_cleaner(provider_calls),
        voyage_embedder=_fake_voyage_embedder(provider_calls),
    )

    result = executor.execute(_executor_job(), lease_owner="worker-1", now=NOW)

    assert result.status == "denied"
    assert result.denied_operation == "gemini.cleanup_transcript"
    assert result.error_code == "usage_limit_exceeded"
    assert provider_calls == []
    assert connection.transaction_events == []
    assert any(params.get("status") == "denied" for _statement, params in connection.calls)


def test_real_hosted_executor_uses_entitlement_soft_limits_from_postgres_provider() -> None:
    provider_calls: list[str] = []
    policy = _executor_policy(soft_limits={"gemini.cleanup_transcript": {"total_tokens": 1}})
    connection = HostedExecutorConnection(policy=policy, source_metadata_json=ALWAYS_CLEAN_SOURCE_METADATA)
    executor = HostedIndexingExecutor(
        connection=connection,
        config=AppConfig(),
        gate=UsageGate(),
        ledger=RecordingLedger(),
        metadata_fetcher=_metadata_fetcher,
        transcript_fetcher=_transcript_fetcher,
        gemini_cleaner=_fake_gemini_cleaner(provider_calls),
        voyage_embedder=_fake_voyage_embedder(provider_calls),
    )

    result = executor.execute(_executor_job(), lease_owner="worker-1", now=NOW)

    assert result.status == "denied"
    assert result.denied_operation == "gemini.cleanup_transcript"
    assert result.error_code == "soft_limit_exceeded"
    assert provider_calls == []
    assert connection.transaction_events == []


def test_real_hosted_executor_denies_search_write_before_transaction() -> None:
    provider_calls: list[str] = []
    policy = _executor_policy({"search_store.index_write": {"chunks": 0}})
    connection = HostedExecutorConnection(policy=policy, source_metadata_json=ALWAYS_CLEAN_SOURCE_METADATA)
    executor = HostedIndexingExecutor(
        connection=connection,
        config=AppConfig(),
        gate=UsageGate(),
        ledger=RecordingLedger(),
        metadata_fetcher=_metadata_fetcher,
        transcript_fetcher=_transcript_fetcher,
        gemini_cleaner=_fake_gemini_cleaner(provider_calls),
        voyage_embedder=_fake_voyage_embedder(provider_calls),
    )

    result = executor.execute(_executor_job(), lease_owner="worker-1", now=NOW)

    assert result.status == "denied"
    assert result.denied_operation == "search_store.index_write"
    assert provider_calls == ["gemini", "voyage"]
    assert connection.transaction_events == ["begin", "commit", "begin", "commit"]
    assert all(not statement.startswith("INSERT INTO videos") for statement, _params in connection.calls)


def test_real_hosted_executor_reuses_persisted_provider_outputs() -> None:
    provider_calls: list[str] = []

    class CachedOutputConnection(HostedExecutorConnection):
        def __init__(self) -> None:
            super().__init__(policy=_executor_policy(), source_metadata_json=ALWAYS_CLEAN_SOURCE_METADATA)
            self.output_reads = 0

        def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            if "SELECT status, output_json" in statement:
                self.calls.append((statement, dict(params or {})))
                self.output_reads += 1
                if self.output_reads == 1:
                    return [
                        {
                            "status": "succeeded",
                            "output_json": {
                                "transcript": {
                                    "version_id": "cached_tx",
                                    "video_id": "OEDoJyhQhXs",
                                    "source": "youtube-transcript-api",
                                    "language": "en",
                                    "is_generated": False,
                                    "text_hash": "cached_hash",
                                    "segments": [
                                        {
                                            "segment_id": "seg_1",
                                            "sequence": 0,
                                            "start_ms": 0,
                                            "end_ms": 5000,
                                            "text": "Cached cleaned transcript text for hosted indexing.",
                                        }
                                    ],
                                }
                            },
                        }
                    ]
                return [
                    {
                        "status": "succeeded",
                        "output_json": {"vectors": [[0.25] * DEFAULT_EMBEDDING_DIMENSION]},
                    }
                ]
            return super().execute(statement, params)

    connection = CachedOutputConnection()
    executor = HostedIndexingExecutor(
        connection=connection,
        config=AppConfig(),
        gate=UsageGate(),
        ledger=RecordingLedger(),
        metadata_fetcher=_metadata_fetcher,
        transcript_fetcher=_transcript_fetcher,
        gemini_cleaner=_fake_gemini_cleaner(provider_calls),
        voyage_embedder=_fake_voyage_embedder(provider_calls),
    )

    result = executor.execute(_executor_job(), lease_owner="worker-1", now=NOW)

    assert result.status == "succeeded"
    assert provider_calls == []
    assert connection.output_reads == 2
    assert result.chunks_written == 1


def test_real_hosted_executor_refuses_provider_replay_after_success_event_without_output() -> None:
    provider_calls: list[str] = []

    class ProviderSucceededConnection(HostedExecutorConnection):
        def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            if "FROM usage_events" in statement and "provider_attempt_succeeded" in statement:
                self.calls.append((statement, dict(params or {})))
                return [{"id": "evt_provider_success"}]
            return super().execute(statement, params)

    connection = ProviderSucceededConnection(
        policy=_executor_policy(),
        source_metadata_json=ALWAYS_CLEAN_SOURCE_METADATA,
    )
    executor = HostedIndexingExecutor(
        connection=connection,
        config=AppConfig(),
        gate=UsageGate(),
        ledger=RecordingLedger(),
        metadata_fetcher=_metadata_fetcher,
        transcript_fetcher=_transcript_fetcher,
        gemini_cleaner=_fake_gemini_cleaner(provider_calls),
        voyage_embedder=_fake_voyage_embedder(provider_calls),
    )

    result = executor.execute(_executor_job(), lease_owner="worker-1", now=NOW)

    assert result.status == "failed"
    assert result.error_code == "provider_output_missing"
    assert provider_calls == []


def test_hosted_metadata_fetch_requires_webshare_before_provider_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = RecordingGate()
    ledger = RecordingLedger()

    def fail_discover_video(**_kwargs):  # noqa: ANN003, ANN202
        raise AssertionError("metadata fetch must not call YouTube without hosted Webshare")

    monkeypatch.setattr("yutome.hosted.indexing.discover_video", fail_discover_video)
    executor = HostedIndexingExecutor(
        connection=HostedExecutorConnection(policy=_executor_policy()),
        config=AppConfig(),
        gate=gate,
        ledger=ledger,
        usage_context_provider=AllowAnyProviderUsage(),
    )

    with pytest.raises(HostedIndexingError, match="Webshare residential proxy credentials"):
        executor._fetch_video_metadata("OEDoJyhQhXs", _source(), _executor_job())

    assert gate.calls == []
    assert ledger.events == []


def test_hosted_metadata_fetch_routes_through_webshare_and_maps_selected_ytdlp_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = RecordingGate()
    ledger = RecordingLedger()
    raw_metadata = {
        "id": "OEDoJyhQhXs",
        "title": "Metered metadata",
        "description": "Full YouTube description",
        "duration": 60,
        "upload_date": "20220201",
        "timestamp": 1643750702,
        "webpage_url": "https://www.youtube.com/watch?v=OEDoJyhQhXs",
        "live_status": "not_live",
        "thumbnails": [{"url": "https://img.youtube.com/small.jpg"}, {"url": "https://img.youtube.com/large.jpg"}],
        "formats": [{"format_id": "bad-for-hosted-row"}],
        "requested_formats": [{"format_id": "also-too-large"}],
        "subtitles": {"en": []},
        "automatic_captions": {"en": []},
        "heatmap": [],
        "http_headers": {"User-Agent": "volatile"},
    }

    def fake_discover_video(**kwargs):  # noqa: ANN003, ANN202
        assert kwargs["proxy"].kind == "webshare"
        assert kwargs["proxy"].webshare_username == "proxy-user"
        assert kwargs["hosted_context"] is not None
        return DiscoveredVideo(
            video_id="OEDoJyhQhXs",
            title="Metered metadata",
            url="https://www.youtube.com/watch?v=OEDoJyhQhXs",
            channel_id="UCleo",
            channel_title="Leo",
            channel_handle="@leoandlongevity",
            duration_seconds=60,
            playlist_tab="video",
            raw=raw_metadata,
        )

    monkeypatch.setattr("yutome.hosted.indexing.discover_video", fake_discover_video)
    executor = HostedIndexingExecutor(
        connection=HostedExecutorConnection(policy=_executor_policy()),
        config=_webshare_config(),
        gate=gate,
        ledger=ledger,
        usage_context_provider=AllowAnyProviderUsage(),
    )

    video = executor._fetch_video_metadata("OEDoJyhQhXs", _source(), _executor_job())

    assert video.youtube_video_id == "OEDoJyhQhXs"
    assert video.description == "Full YouTube description"
    assert video.published_at == datetime(2022, 2, 1, tzinfo=timezone.utc)
    assert video.metadata == {
        "source": "yt_dlp",
        "channel_title": "Leo",
        "channel_handle": "@leoandlongevity",
        "playlist_tab": "video",
        "thumbnail_url": "https://img.youtube.com/large.jpg",
        "webpage_url": "https://www.youtube.com/watch?v=OEDoJyhQhXs",
        "live_status": "not_live",
        "upload_date": "20220201",
        "timestamp": 1643750702,
        "metadata_hash": sha256_json(raw_metadata),
    }
    assert "formats" not in video.metadata
    assert "automatic_captions" not in video.metadata
    assert "http_headers" not in video.metadata
    assert [(call["subject"], call["operation"]) for call in gate.calls] == [("youtube", "metadata_fetch")]
    assert [event.status for event in ledger.events] == ["started", "succeeded"]


def test_hosted_transcript_fetch_requires_webshare_before_provider_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = RecordingGate()
    ledger = RecordingLedger()

    def fail_fetch_transcript(**_kwargs):  # noqa: ANN003, ANN202
        raise AssertionError("transcript fetch must not call YouTube without hosted Webshare")

    monkeypatch.setattr("yutome.hosted.indexing.fetch_transcript", fail_fetch_transcript)
    executor = HostedIndexingExecutor(
        connection=HostedExecutorConnection(policy=_executor_policy()),
        config=AppConfig(),
        gate=gate,
        ledger=ledger,
        usage_context_provider=AllowAnyProviderUsage(),
    )

    with pytest.raises(HostedIndexingError, match="Webshare residential proxy credentials"):
        executor._fetch_transcript("OEDoJyhQhXs", _source(), _executor_job())

    assert gate.calls == []
    assert ledger.events == []


def test_hosted_transcript_fetch_routes_through_webshare(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = RecordingGate()
    ledger = RecordingLedger()

    def fake_fetch_transcript(**kwargs):  # noqa: ANN003, ANN202
        assert kwargs["proxy"].kind == "webshare"
        assert kwargs["hosted_context"] is not None
        return TranscriptFetchResult(
            raw_snippets=[{"start": 0.0, "duration": 4.0, "text": "Metered transcript fetch."}],
            source="youtube-transcript-api",
            language="en",
            is_generated=True,
        )

    monkeypatch.setattr("yutome.hosted.indexing.fetch_transcript", fake_fetch_transcript)
    executor = HostedIndexingExecutor(
        connection=HostedExecutorConnection(policy=_executor_policy()),
        config=_webshare_config(),
        gate=gate,
        ledger=ledger,
        usage_context_provider=AllowAnyProviderUsage(),
    )

    result = executor._fetch_transcript("OEDoJyhQhXs", _source(), _executor_job())

    assert result.raw_snippets[0]["text"] == "Metered transcript fetch."
    assert [(call["subject"], call["operation"]) for call in gate.calls] == [("youtube", "transcript_fetch")]
    assert [event.status for event in ledger.events] == ["started", "succeeded"]


def test_hosted_transcript_fetch_cascades_api_to_ytdlp(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_fetch_transcript(**_kwargs):  # noqa: ANN003, ANN202
        calls.append("api")
        raise RuntimeError("transcript API blocked")

    def fake_fetch_ytdlp(**kwargs):  # noqa: ANN003, ANN202
        calls.append("yt-dlp")
        assert kwargs["hosted_context"] is not None
        return TranscriptFetchResult(
            raw_snippets=[{"start": 0.0, "duration": 4.0, "text": "Cascaded yt-dlp transcript."}],
            source="yt-dlp-json3:en",
            language="en",
            is_generated=True,
        )

    def fail_gemini(**_kwargs):  # noqa: ANN003, ANN202
        raise AssertionError("Gemini fallback should not run when yt-dlp captions succeed")

    monkeypatch.setattr("yutome.hosted.indexing.fetch_transcript", fake_fetch_transcript)
    monkeypatch.setattr("yutome.hosted.indexing.fetch_subtitle_transcript_with_ytdlp", fake_fetch_ytdlp)
    monkeypatch.setattr("yutome.hosted.indexing.transcribe_youtube_url_with_gemini", fail_gemini)
    executor = HostedIndexingExecutor(
        connection=HostedExecutorConnection(policy=_executor_policy()),
        config=_webshare_config(),
        gate=UsageGate(),
        ledger=RecordingLedger(),
        usage_context_provider=AllowAnyProviderUsage(),
    )

    result = executor._fetch_transcript("OEDoJyhQhXs", _source(), _executor_job())

    assert calls == ["api", "yt-dlp"]
    assert result.source == "yt-dlp-json3:en"


def test_hosted_transcript_fetch_falls_back_to_gemini_after_both_caption_providers_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_fetch_transcript(**_kwargs):  # noqa: ANN003, ANN202
        calls.append("api")
        raise RuntimeError("transcript API blocked")

    def fake_fetch_ytdlp(**_kwargs):  # noqa: ANN003, ANN202
        calls.append("yt-dlp")
        raise RuntimeError("yt-dlp did not write json3 subtitles for OEDoJyhQhXs")

    def fake_gemini(**kwargs):  # noqa: ANN003, ANN202
        calls.append("gemini")
        assert kwargs["hosted_context"].subject == "gemini"
        return TranscriptFetchResult(
            raw_snippets=[{"start": 0.0, "duration": 4.0, "text": "Gemini media transcript."}],
            source="gemini-media",
            language="en",
            is_generated=True,
        )

    monkeypatch.setattr("yutome.hosted.indexing.fetch_transcript", fake_fetch_transcript)
    monkeypatch.setattr("yutome.hosted.indexing.fetch_subtitle_transcript_with_ytdlp", fake_fetch_ytdlp)
    monkeypatch.setattr("yutome.hosted.indexing.transcribe_youtube_url_with_gemini", fake_gemini)
    webshare_config = _webshare_config()
    executor = HostedIndexingExecutor(
        connection=HostedExecutorConnection(policy=_executor_policy()),
        config=AppConfig(proxy=webshare_config.proxy, gemini=GeminiConfig(fallback_enabled=True)),
        gate=UsageGate(),
        ledger=RecordingLedger(),
        usage_context_provider=AllowAnyProviderUsage(),
    )

    result = executor._fetch_transcript("OEDoJyhQhXs", _source(), _executor_job())

    assert calls == ["api", "yt-dlp", "gemini"]
    assert result.source == "gemini-media"


def test_hosted_transcript_fetch_does_not_cascade_on_usage_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_fetch_transcript(**_kwargs):  # noqa: ANN003, ANN202
        calls.append("api")
        reservation = UsageReservation(
            workspace_id="ws_alice",
            subject="youtube",
            operation="transcript_fetch",
            credential_mode="hosted",
            idempotency_key="denied",
            status="denied",
            decision=UsageDecision(allowed=False, reason="usage_limit_exceeded"),
        )
        event = UsageEvent(
            workspace_id="ws_alice",
            subject="youtube",
            operation="transcript_fetch",
            event_type="provider_attempt_denied",
            status="denied",
        )
        raise UsageReservationDenied(reservation, event)

    def fake_fetch_ytdlp(**_kwargs):  # noqa: ANN003, ANN202
        calls.append("yt-dlp")
        raise AssertionError("secondary caption provider should not run after usage denial")

    monkeypatch.setattr("yutome.hosted.indexing.fetch_transcript", fake_fetch_transcript)
    monkeypatch.setattr("yutome.hosted.indexing.fetch_subtitle_transcript_with_ytdlp", fake_fetch_ytdlp)
    executor = HostedIndexingExecutor(
        connection=HostedExecutorConnection(policy=_executor_policy()),
        config=_webshare_config(),
        gate=UsageGate(),
        ledger=RecordingLedger(),
        usage_context_provider=AllowAnyProviderUsage(),
    )

    with pytest.raises(UsageReservationDenied):
        executor._fetch_transcript("OEDoJyhQhXs", _source(), _executor_job())

    assert calls == ["api"]


def test_source_discovery_executor_enqueues_real_index_video_jobs() -> None:
    class DiscoveryConnection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            params = dict(params or {})
            self.calls.append((statement, params))
            if "UPDATE jobs" in statement and "lease_expires_at = %(lease_expires_at)s" in statement:
                return [{"id": params.get("job_id"), "lease_owner": params.get("lease_owner")}]
            if "FROM sources" in statement:
                return [
                    {
                        "id": "src_leo",
                        "workspace_id": "ws_alice",
                        "source_type": "handle",
                        "source_url": "https://www.youtube.com/@leoandlongevity",
                        "canonical_channel_id": None,
                        "canonical_playlist_id": None,
                        "canonical_video_id": None,
                        "display_name": "@leoandlongevity",
                        "selected": True,
                        "auto_index_allowed": True,
                        "import_source": "cli",
                        "metadata_json": {},
                        "status": "active",
                    }
                ]
            if statement.startswith("INSERT INTO jobs"):
                return [{"id": params["id"]}]
            return []

    def discoverer(_source: Source, _context: ProviderCallContext | None, limit: int | None) -> list[DiscoveredVideo]:
        assert limit == 2
        assert _context is not None
        return [
            DiscoveredVideo(
                video_id="OEDoJyhQhXs",
                title="One",
                url="https://www.youtube.com/watch?v=OEDoJyhQhXs",
                channel_id="UC1",
                channel_title="Leo",
                channel_handle="@leoandlongevity",
                duration_seconds=60,
                playlist_tab="videos",
                raw={},
            ),
            DiscoveredVideo(
                video_id="abcdefghijk",
                title="Two",
                url="https://www.youtube.com/watch?v=abcdefghijk",
                channel_id="UC1",
                channel_title="Leo",
                channel_handle="@leoandlongevity",
                duration_seconds=90,
                playlist_tab="streams",
                raw={},
            ),
        ]

    connection = DiscoveryConnection()
    executor = HostedSourceDiscoveryExecutor(
        connection=connection,
        config=_webshare_config(),
        gate=UsageGate(),
        ledger=RecordingLedger(),
        usage_context_provider=AllowAnyProviderUsage(),
        video_discoverer=discoverer,
    )
    job = Job(
        id="job_discover",
        workspace_id="ws_alice",
        source_id="src_leo",
        job_type="discover_source",
        status="queued",
        priority=10,
        idempotency_key="ws_alice:src_leo:discover_source",
        lease_owner="worker-1",
        metadata_jsonb={"source_refresh_policy_id": "srp_1", "max_new_videos_per_run": 2},
        created_at=NOW,
    )

    result = executor.execute(job, lease_owner="worker-1", now=NOW)
    job_inserts = [params for statement, params in connection.calls if statement.startswith("INSERT INTO jobs")]

    assert result.status == "succeeded"
    assert result.enqueued_jobs == 2
    assert result.video_ids == ("OEDoJyhQhXs", "abcdefghijk")
    assert [_jsonb_obj(params["metadata_json"])["youtube_video_id"] for params in job_inserts] == [
        "OEDoJyhQhXs",
        "abcdefghijk",
    ]
    finish_sql = "\n".join(statement for statement, _params in connection.calls if "UPDATE source_refresh_policies" in statement)
    assert "cursor_json = cursor_json ||" in finish_sql
    assert "cursor_jsonb" not in finish_sql


class _SubscriptionDiscoveryConnection:
    def __init__(
        self,
        *,
        auth_grant_id: str | None = "ytg_alice",
        grant_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.auth_grant_id = auth_grant_id
        self.grant_rows = list(grant_rows or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params = dict(params or {})
        self.calls.append((statement, params))
        if "UPDATE jobs" in statement and "lease_expires_at = %(lease_expires_at)s" in statement:
            return [{"id": params.get("job_id"), "lease_owner": params.get("lease_owner")}]
        if "FROM sources" in statement:
            return [
                {
                    "id": "src_subs",
                    "workspace_id": "ws_alice",
                    "source_type": "subscription_collection",
                    "source_url": "youtube://subscriptions/mine",
                    "canonical_channel_id": None,
                    "canonical_playlist_id": None,
                    "canonical_video_id": None,
                    "display_name": "Alice subscriptions",
                    "selected": True,
                    "auto_index_allowed": True,
                    "import_source": "oauth_sync",
                    "auth_grant_id": self.auth_grant_id,
                    "metadata_json": {},
                    "status": "active",
                }
            ]
        if "FROM youtube_grants" in statement:
            return self.grant_rows
        if statement.startswith("INSERT INTO sources"):
            return [
                {
                    "id": params["id"],
                    "source_type": params.get("source_type"),
                    "source_url": params.get("source_url"),
                }
            ]
        if statement.startswith("INSERT INTO source_refresh_policies"):
            return [
                {
                    "id": params["id"],
                    "enabled": params.get("enabled"),
                    "cadence_seconds": params.get("cadence_seconds"),
                }
            ]
        if statement.startswith("INSERT INTO jobs"):
            return [
                {
                    "id": params["id"],
                    "job_type": params.get("job_type"),
                    "status": params.get("status"),
                    "source_id": params.get("source_id"),
                }
            ]
        return []


def _subscription_discovery_job() -> Job:
    return Job(
        id="job_discover_subs",
        workspace_id="ws_alice",
        source_id="src_subs",
        job_type="discover_source",
        status="queued",
        priority=10,
        idempotency_key="ws_alice:src_subs:discover_source",
        lease_owner="worker-1",
        metadata_jsonb={"source_refresh_policy_id": "srp_subs", "max_new_videos_per_run": 5},
        created_at=NOW,
    )


def test_subscription_collection_discovery_fans_out_to_channel_sources() -> None:
    def lister(_connection: Any, source: Source) -> list[LibraryChannel]:
        assert source.auth_grant_id == "ytg_alice"
        return [
            LibraryChannel(
                library_channel_id="c1",
                source="youtube:channel:UCaaaaaaaaaaaaaaaaaaaaaa",
                source_url="https://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa",
                channel_id="UCaaaaaaaaaaaaaaaaaaaaaa",
                title="A",
            ),
            LibraryChannel(
                library_channel_id="c2",
                source="youtube:channel:UCbbbbbbbbbbbbbbbbbbbbbb",
                source_url="https://www.youtube.com/channel/UCbbbbbbbbbbbbbbbbbbbbbb",
                channel_id="UCbbbbbbbbbbbbbbbbbbbbbb",
                title="B",
            ),
        ]

    connection = _SubscriptionDiscoveryConnection()
    executor = HostedSourceDiscoveryExecutor(
        connection=connection,
        config=AppConfig(),
        gate=UsageGate(),
        ledger=RecordingLedger(),
        subscription_channel_lister=lister,
    )

    result = executor.execute(_subscription_discovery_job(), lease_owner="worker-1", now=NOW)

    source_inserts = [params for statement, params in connection.calls if statement.startswith("INSERT INTO sources")]
    job_inserts = [params for statement, params in connection.calls if statement.startswith("INSERT INTO jobs")]
    child_discover_jobs = [params for params in job_inserts if params.get("job_type") == "discover_source"]

    assert result.status == "succeeded"
    assert result.discovered_videos == 2
    assert result.enqueued_jobs == 2
    assert len(source_inserts) == 2
    assert {params["import_source"] for params in source_inserts} == {"public_api"}
    assert {params.get("auth_grant_id") for params in source_inserts} == {None}
    assert len(child_discover_jobs) == 2
    assert {params["source_id"] for params in child_discover_jobs} == {params["id"] for params in source_inserts}
    assert [params for params in job_inserts if params.get("job_type") == "index_video"] == []
    finish_sql = "\n".join(statement for statement, _params in connection.calls if "UPDATE source_refresh_policies" in statement)
    assert "cursor_json = cursor_json ||" in finish_sql


def test_subscription_collection_discovery_marks_source_auth_failed_on_revoked_grant() -> None:
    def lister(_connection: Any, _source: Source) -> list[LibraryChannel]:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_reconnect_required",
            message="reconnect",
            status_code=401,
        )

    connection = _SubscriptionDiscoveryConnection()
    executor = HostedSourceDiscoveryExecutor(
        connection=connection,
        config=AppConfig(),
        gate=UsageGate(),
        ledger=RecordingLedger(),
        subscription_channel_lister=lister,
    )

    result = executor.execute(_subscription_discovery_job(), lease_owner="worker-1", now=NOW)

    assert result.status == "denied"
    assert result.error_code == "source_auth_failed"
    assert any("UPDATE sources" in statement and "status = 'auth_failed'" in statement for statement, _params in connection.calls)
    assert any(
        "UPDATE jobs" in statement and params.get("status") == "denied" and params.get("error_code") == "source_auth_failed"
        for statement, params in connection.calls
    )
    assert not any("UPDATE jobs" in statement and params.get("status") == "retry_wait" for statement, params in connection.calls)
    assert not any("retry_after" in params for _statement, params in connection.calls)
    assert not any(statement.startswith("INSERT INTO jobs") for statement, _params in connection.calls)


def test_subscription_collection_discovery_missing_grant_id_is_auth_failed() -> None:
    connection = _SubscriptionDiscoveryConnection(auth_grant_id=None, grant_rows=[])
    executor = HostedSourceDiscoveryExecutor(
        connection=connection,
        config=AppConfig(),
        gate=UsageGate(),
        ledger=RecordingLedger(),
        youtube_oauth_settings=None,
    )

    result = executor.execute(_subscription_discovery_job(), lease_owner="worker-1", now=NOW)

    assert result.status == "denied"
    assert result.error_code == "source_auth_failed"
    assert any("FROM youtube_grants" in statement for statement, _params in connection.calls)
    assert any("UPDATE sources" in statement and "status = 'auth_failed'" in statement for statement, _params in connection.calls)
    assert not any("UPDATE jobs" in statement and params.get("status") == "retry_wait" for statement, params in connection.calls)
    assert not any(statement.startswith("INSERT INTO jobs") for statement, _params in connection.calls)


def test_real_hosted_executor_replay_uses_stable_ids_and_upserts() -> None:
    connection = HostedExecutorConnection(
        policy=_executor_policy(),
        source_metadata_json=ALWAYS_CLEAN_SOURCE_METADATA,
    )
    results = []
    for _ in range(2):
        provider_calls: list[str] = []
        executor = HostedIndexingExecutor(
            connection=connection,
            config=AppConfig(),
            gate=UsageGate(),
            ledger=RecordingLedger(),
            metadata_fetcher=_metadata_fetcher,
            transcript_fetcher=_transcript_fetcher,
            gemini_cleaner=_fake_gemini_cleaner(provider_calls),
            voyage_embedder=_fake_voyage_embedder(provider_calls),
        )
        results.append(executor.execute(_executor_job(), lease_owner="worker-1", now=NOW))

    assert results[0].hosted_video_id == results[1].hosted_video_id
    assert results[0].transcript_version_id == results[1].transcript_version_id
    assert connection.transaction_events == [
        "begin",
        "commit",
        "begin",
        "commit",
        "begin",
        "commit",
        "begin",
        "commit",
        "begin",
        "commit",
        "begin",
        "commit",
    ]


def test_real_hosted_executor_redacts_provider_errors_before_persisting_job_failure() -> None:
    def failing_cleaner(
        _transcript: Any,
        _video: Any,
        _context: ProviderCallContext,
    ) -> Any:
        raise RuntimeError(
            "Proxy failed for http://webshare_user:SuperSecretPass@proxy.webshare.io:80 "
            "with api_key=pa-1234567890abcdef"
        )

    connection = HostedExecutorConnection(
        policy=_executor_policy(),
        source_metadata_json=ALWAYS_CLEAN_SOURCE_METADATA,
    )
    executor = HostedIndexingExecutor(
        connection=connection,
        config=AppConfig(),
        gate=UsageGate(),
        ledger=RecordingLedger(),
        metadata_fetcher=_metadata_fetcher,
        transcript_fetcher=_transcript_fetcher,
        gemini_cleaner=failing_cleaner,
        voyage_embedder=_fake_voyage_embedder([]),
    )

    result = executor.execute(_executor_job(), lease_owner="worker-1", now=NOW)
    persisted_messages = [
        str(params.get("error_message") or "")
        for statement, params in connection.calls
        if "UPDATE job" in statement and params.get("error_message")
    ]

    assert result.status == "failed"
    assert result.error_message is not None
    assert "SuperSecretPass" not in result.error_message
    assert "pa-1234567890abcdef" not in result.error_message
    assert "http://***:***@proxy.webshare.io:80" in result.error_message
    assert persisted_messages
    assert all("SuperSecretPass" not in message for message in persisted_messages)
    assert all("pa-1234567890abcdef" not in message for message in persisted_messages)


def test_hosted_indexing_module_has_no_local_store_backend_references() -> None:
    module_text = Path("src/yutome/hosted/indexing.py").read_text(encoding="utf-8").lower()

    assert "sql" "ite" not in module_text
    assert "lance" "db" not in module_text
