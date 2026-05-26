from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from yutome.hosted.control_plane import Job, Source
from yutome.hosted.gate import UsageGate
from yutome.hosted.models import EntitlementPolicy, WorkspaceBalance
from yutome.hosted.indexing import (
    DEFAULT_EMBEDDING_DIMENSION,
    HostedVideoInput,
    IndexProfileInput,
    TranscriptChunkInput,
    mock_embedding_vector,
    plan_mock_hosted_public_indexing,
    source_from_public_youtube_input,
)


NOW = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)


class RecordingGate(UsageGate):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def reserve(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(dict(kwargs))
        return super().reserve(**kwargs)


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

    assert video_source.is_public_source is True
    assert video_source.source_type == "video"
    assert video_source.canonical_video_id == "OEDoJyhQhXs"
    assert video_source.source_url == "https://www.youtube.com/watch?v=OEDoJyhQhXs"
    assert handle_source.is_public_source is True
    assert handle_source.source_type == "handle"
    assert handle_source.source_url == "https://www.youtube.com/@leoandlongevity"


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
    assert embedding_statement.params["index_profile_id"] == "sip_voyage4lite_bm25_default"
    assert len(embedding_vector) == 1024
    assert [call["estimated_units"].get("query_vector_dimensions") for call in gate.calls] == [None, None, 1024.0]


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
    sql = "\n".join(operation.statement.sql for operation in plan.sql_operations)
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
    assert "INSERT INTO videos" in sql
    assert "INSERT INTO transcript_versions" in sql
    assert "INSERT INTO chunks" in sql
    assert "INSERT INTO chunk_embeddings" in sql
    assert "UPDATE videos" in sql
    assert "active_transcript_version_id = upserted.id" in sql
    assert search_plan.mode == "hybrid"
    assert "FULL OUTER JOIN semantic USING (chunk_id)" in search_plan.statement.sql
    assert search_plan.statement.params["workspace_id"] == "ws_alice"
    assert search_plan.usage.operation == "hybrid_query"


def test_hosted_indexing_module_has_no_local_store_backend_references() -> None:
    module_text = Path("src/yutome/hosted/indexing.py").read_text(encoding="utf-8").lower()

    assert "sqlite" not in module_text
    assert "lancedb" not in module_text
