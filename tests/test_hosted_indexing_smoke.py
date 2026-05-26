from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from yutome.config import AppConfig
from yutome.hosted.control_plane import Job, Source
from yutome.hosted.gate import UsageGate
from yutome.hosted.mcp_query import HostedMcpUsageContext
from yutome.hosted.models import EntitlementPolicy, ProviderAllocation, UsageEvent, UsageNormalization, WorkspaceBalance
from yutome.hosted.provider_wrappers import ProviderCallContext, execute_provider_call
from yutome.hosted.indexing import (
    DEFAULT_EMBEDDING_DIMENSION,
    HostedIndexingExecutor,
    HostedSourceDiscoveryExecutor,
    HostedVideoInput,
    IndexProfileInput,
    TranscriptChunkInput,
    mock_embedding_vector,
    plan_mock_hosted_public_indexing,
    plan_real_hosted_public_indexing,
    source_from_public_youtube_input,
)
from yutome.youtube import DiscoveredVideo, TranscriptFetchResult


NOW = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)


class RecordingGate(UsageGate):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def reserve(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(dict(kwargs))
        return super().reserve(**kwargs)


class RecordingLedger:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    def append(self, event: UsageEvent) -> None:
        self.events.append(event)


class HostedExecutorConnection:
    def __init__(self, *, policy: EntitlementPolicy, balance: WorkspaceBalance | None = None) -> None:
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
                    "metadata_json": {},
                    "status": "active",
                }
            ]
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
                remaining_units=json.loads(str((params or {})["remaining_units_jsonb"])),
                unlimited_units=self.balance.unlimited_units,
            )
            return [
                {
                    "workspace_id": self.balance.workspace_id,
                    "entitlement_policy_id": self.policy.id,
                    "remaining_units_jsonb": self.balance.remaining_units,
                    "reserved_units_jsonb": json.loads(str((params or {})["reserved_units_jsonb"])),
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


def _metadata_fetcher(video_id: str, _source: Source, _job: Job) -> HostedVideoInput:
    return HostedVideoInput(
        youtube_video_id=video_id,
        title="Real hosted executor video",
        url=f"https://www.youtube.com/watch?v={video_id}",
        channel_id="UCleoandlongevity",
        duration_seconds=60,
        metadata={"channel_title": "Leo and Longevity"},
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
        display_name="Real-world smoke video",
    )


def _job(source: Source) -> Job:
    return Job(
        id="job_oedo_index",
        workspace_id=source.workspace_id,
        source_id=source.id,
        job_type="index_video",
        status="queued",
        idempotency_key="ws_alice:src_oedo:index_video:mock",
        created_at=NOW,
    )


def _video() -> HostedVideoInput:
    return HostedVideoInput(
        youtube_video_id="OEDoJyhQhXs",
        title="Mocked hosted public indexing smoke",
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


def test_mock_hosted_indexing_plan_is_idempotent_and_operation_scoped() -> None:
    source = _source()
    job = _job(source)

    left = plan_mock_hosted_public_indexing(source=source, job=job, video=_video(), chunks=_chunks())
    right = plan_mock_hosted_public_indexing(source=source, job=job, video=_video(), chunks=list(reversed(_chunks())))

    assert left.hosted_video_id == right.hosted_video_id
    assert left.transcript_version_id == right.transcript_version_id
    assert left.operation_ids == right.operation_ids
    assert [reservation.id for reservation in left.usage_reservations] == [
        reservation.id for reservation in right.usage_reservations
    ]
    assert [reservation.idempotency_key for reservation in left.usage_reservations] == [
        reservation.idempotency_key for reservation in right.usage_reservations
    ]
    assert {operation.operation for operation in left.job_operations} == {
        "voyage.embed_documents",
        "search_store.index_write",
        "search_store.hybrid_query",
    }
    assert all(operation.id.startswith("op_") for operation in left.job_operations)


def test_mock_hosted_indexing_defaults_to_fixed_hosted_vector_dimension() -> None:
    source = _source()
    gate = RecordingGate()
    policy, balance = _reservation_grants(source.workspace_id)

    plan = plan_mock_hosted_public_indexing(
        source=source,
        job=_job(source),
        video=_video(),
        chunks=_chunks(),
        policy=policy,
        balance=balance,
        gate=gate,
    )
    embedding_statement = next(operation.statement for operation in plan.sql_operations if operation.name == "chunk_embeddings.upsert")
    embedding_vector = embedding_statement.params["embedding"].strip("[]").split(",")

    assert DEFAULT_EMBEDDING_DIMENSION == 1024
    assert len(mock_embedding_vector("hosted vector contract")) == 1024
    assert plan.index_profile.embedding_dimension == 1024
    assert plan.search_operations[0].statement.params["embedding_dimension"] == 1024
    assert plan.search_operations[0].usage.units["query_vector_dimensions"] == 1024
    assert embedding_statement.params["index_profile_id"].startswith("sip_")
    assert embedding_statement.params["index_profile_id"] != "sip_voyage4lite_bm25_default"
    assert len(embedding_vector) == 1024
    assert [call["estimated_units"].get("query_vector_dimensions") for call in gate.calls] == [None, None, 1024.0]


def test_default_index_profile_ids_are_workspace_scoped_for_search_joins() -> None:
    source = _source()
    other_source = _source().model_copy(update={"workspace_id": "ws_bob", "id": "src_bob"})

    first = plan_real_hosted_public_indexing(
        source=source,
        job=_job(source),
        video=_video(),
        chunks=_chunks(),
        embedding_vectors=[mock_embedding_vector(chunk.text) for chunk in _chunks()],
        transcript_source="youtube_transcript",
        language_code="en",
    )
    second = plan_real_hosted_public_indexing(
        source=other_source,
        job=_job(other_source),
        video=_video(),
        chunks=_chunks(),
        embedding_vectors=[mock_embedding_vector(chunk.text) for chunk in _chunks()],
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


def test_mock_hosted_indexing_rejects_unsupported_embedding_profile_before_reservations() -> None:
    source = _source()
    gate = RecordingGate()
    policy, balance = _reservation_grants(source.workspace_id)

    with pytest.raises(ValueError, match=r"unsupported embedding profile .*vector\(1024\)"):
        plan_mock_hosted_public_indexing(
            source=source,
            job=_job(source),
            video=_video(),
            chunks=_chunks(),
            index_profile=IndexProfileInput(embedding_dimension=8),
            policy=policy,
            balance=balance,
            gate=gate,
        )

    assert gate.calls == []


def test_public_source_validity_is_enforced_before_planning() -> None:
    source = _source().model_copy(update={"status": "disabled"})

    with pytest.raises(ValueError, match="source is not public and discoverable"):
        plan_mock_hosted_public_indexing(source=source, job=_job(source), video=_video(), chunks=_chunks())

    oauth_source = Source(
        id="src_oauth_subs",
        workspace_id="ws_alice",
        source_type="subscriptions",
        source_url="youtube://subscriptions/mine",
        import_source="youtube_oauth",
        auth_grant_id="yt_grant_alice",
    )
    with pytest.raises(ValueError, match="source is not public and discoverable"):
        plan_mock_hosted_public_indexing(source=oauth_source, job=_job(oauth_source), video=_video(), chunks=_chunks())


def test_usage_reservation_hook_receives_operation_ids_and_stable_keys() -> None:
    source = _source()
    gate = RecordingGate()
    policy, balance = _reservation_grants(source.workspace_id)

    plan = plan_mock_hosted_public_indexing(
        source=source,
        job=_job(source),
        video=_video(),
        chunks=_chunks(),
        policy=policy,
        balance=balance,
        gate=gate,
    )

    assert [call["subject"] for call in gate.calls] == ["voyage", "search_store", "search_store"]
    assert [call["operation"] for call in gate.calls] == ["embed_documents", "index_write", "hybrid_query"]
    assert all(call["idempotency_key"].startswith("ws_alice:OEDoJyhQhXs:") for call in gate.calls)
    assert {reservation.status for reservation in plan.usage_reservations} == {"reserved"}
    assert {
        operation.metadata_jsonb["usage_reservation_id"] for operation in plan.job_operations
    } == {reservation.id for reservation in plan.usage_reservations}


def test_generated_postgres_and_search_store_operations_are_queryable() -> None:
    source = _source()
    plan = plan_mock_hosted_public_indexing(source=source, job=_job(source), video=_video(), chunks=_chunks())

    operation_names = [operation.name for operation in plan.sql_operations]
    search_plan = plan.search_operations[0]

    assert operation_names[:2] == ["videos.upsert", "search_index_profiles.upsert"]
    assert "usage_reservations.voyage.embed_documents" in operation_names
    assert "usage_reservations.search_store.index_write" in operation_names
    assert "job_operations.search_store.index_write" in operation_names
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
    assert search_plan.mode == "hybrid"
    assert search_plan.statement.params["workspace_id"] == "ws_alice"
    assert search_plan.usage.operation == "hybrid_query"


def test_real_hosted_executor_orders_provider_calls_before_transactional_writes() -> None:
    provider_calls: list[str] = []
    ledger = RecordingLedger()
    connection = HostedExecutorConnection(policy=_executor_policy())
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
        json.loads(params["metadata_json"])
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


def test_real_hosted_executor_denies_before_gemini_or_voyage_provider_calls() -> None:
    provider_calls: list[str] = []
    policy = _executor_policy({"gemini.cleanup_transcript": {"total_tokens": 1}})
    connection = HostedExecutorConnection(policy=policy)
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
    connection = HostedExecutorConnection(policy=policy)
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
    connection = HostedExecutorConnection(policy=policy)
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
            super().__init__(policy=_executor_policy())
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

    connection = ProviderSucceededConnection(policy=_executor_policy())
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


def test_hosted_metadata_fetch_reserves_youtube_operation_without_webshare(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = RecordingGate()
    ledger = RecordingLedger()

    class AllowYouTubeUsage:
        def for_subject(self, *, auth, subject, operation, estimated_units):  # noqa: ANN001, ANN202
            return HostedMcpUsageContext(
                allocation=ProviderAllocation(
                    id=f"alloc_{subject}_{operation}",
                    workspace_id=auth.workspace_id,
                    provider=subject,
                    operation=operation,
                ),
                policy=EntitlementPolicy(id="policy", workspace_id=auth.workspace_id, allowed_operations={f"{subject}.{operation}"}),
                balance=WorkspaceBalance(workspace_id=auth.workspace_id, remaining_units={"request_count": 10}),
            )

    def fake_discover_video(**kwargs):  # noqa: ANN003, ANN202
        assert kwargs["hosted_context"] is None
        return DiscoveredVideo(
            video_id="OEDoJyhQhXs",
            title="Metered metadata",
            url="https://www.youtube.com/watch?v=OEDoJyhQhXs",
            channel_id="UCleo",
            channel_title="Leo",
            channel_handle="@leoandlongevity",
            duration_seconds=60,
            playlist_tab="video",
            raw={},
        )

    monkeypatch.setattr("yutome.hosted.indexing.discover_video", fake_discover_video)
    executor = HostedIndexingExecutor(
        connection=HostedExecutorConnection(policy=_executor_policy()),
        config=AppConfig(),
        gate=gate,
        ledger=ledger,
        usage_context_provider=AllowYouTubeUsage(),
    )

    video = executor._fetch_video_metadata("OEDoJyhQhXs", _source(), _executor_job())

    assert video.youtube_video_id == "OEDoJyhQhXs"
    assert [(call["subject"], call["operation"]) for call in gate.calls] == [("youtube", "metadata_fetch")]
    assert [event.status for event in ledger.events] == ["started", "succeeded"]


def test_hosted_transcript_fetch_reserves_youtube_operation_without_webshare(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = RecordingGate()
    ledger = RecordingLedger()

    class AllowYouTubeUsage:
        def for_subject(self, *, auth, subject, operation, estimated_units):  # noqa: ANN001, ANN202
            return HostedMcpUsageContext(
                allocation=ProviderAllocation(
                    id=f"alloc_{subject}_{operation}",
                    workspace_id=auth.workspace_id,
                    provider=subject,
                    operation=operation,
                ),
                policy=EntitlementPolicy(id="policy", workspace_id=auth.workspace_id, allowed_operations={f"{subject}.{operation}"}),
                balance=WorkspaceBalance(workspace_id=auth.workspace_id, remaining_units={"request_count": 10}),
            )

    def fake_fetch_transcript(**kwargs):  # noqa: ANN003, ANN202
        assert kwargs["hosted_context"] is None
        return TranscriptFetchResult(
            raw_snippets=[{"start": 0.0, "duration": 4.0, "text": "Metered transcript fetch."}],
            source="youtube-transcript-api",
            language="en",
            is_generated=True,
        )

    monkeypatch.setattr("yutome.hosted.indexing.fetch_transcript", fake_fetch_transcript)
    executor = HostedIndexingExecutor(
        connection=HostedExecutorConnection(policy=_executor_policy()),
        config=AppConfig(),
        gate=gate,
        ledger=ledger,
        usage_context_provider=AllowYouTubeUsage(),
    )

    result = executor._fetch_transcript("OEDoJyhQhXs", _source(), _executor_job())

    assert result.raw_snippets[0]["text"] == "Metered transcript fetch."
    assert [(call["subject"], call["operation"]) for call in gate.calls] == [("youtube", "transcript_fetch")]
    assert [event.status for event in ledger.events] == ["started", "succeeded"]


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
        config=AppConfig(),
        gate=UsageGate(),
        ledger=RecordingLedger(),
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
    assert [json.loads(params["metadata_json"])["youtube_video_id"] for params in job_inserts] == [
        "OEDoJyhQhXs",
        "abcdefghijk",
    ]
    finish_sql = "\n".join(statement for statement, _params in connection.calls if "UPDATE source_refresh_policies" in statement)
    assert "cursor_json = cursor_json ||" in finish_sql
    assert "cursor_jsonb" not in finish_sql


def test_real_hosted_executor_replay_uses_stable_ids_and_upserts() -> None:
    connection = HostedExecutorConnection(policy=_executor_policy())
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

    connection = HostedExecutorConnection(policy=_executor_policy())
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

    assert "sqlite" not in module_text
    assert "lancedb" not in module_text
