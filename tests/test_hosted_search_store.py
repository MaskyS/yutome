from __future__ import annotations

from typing import Any

import pytest

from yutome.hosted.search_store import (
    POSTGRES_FTS_PGVECTOR_FALLBACK_BACKEND,
    PGVECTOR_COMPATIBLE_SEMANTIC_BACKEND,
    POSTGRES_FTS_FALLBACK_LEXICAL_BACKEND,
    PostgresVectorChordSearchStore,
    VECTORCHORD_REQUIRED_EXTENSIONS,
    VECTORCHORD_BM25_CHUNKS_INDEX,
    VECTORCHORD_BM25_LEXICAL_BACKEND,
    VECTORCHORD_BM25_PGVECTOR_HYBRID_BACKEND,
    extension_check_sql,
    hybrid_query_plan,
    lexical_query_plan,
    replace_active_transcript_sql,
    semantic_query_plan,
    validate_supported_embedding_profile,
)
from yutome.hosted.resources import (
    channel_resource_sql,
    chunk_resource_sql,
    list_channels_sql,
    list_status_sql,
    list_videos_sql,
    source_resource_sql,
    transcript_resource_sql,
    video_resource_sql,
)


class RecordingConnection:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((statement, dict(params or {})))
        return self.rows


def test_extension_check_sql_targets_vectorchord_suite_extensions() -> None:
    statement = extension_check_sql()

    assert "FROM pg_extension" in statement.sql
    assert statement.params["extensions"] == list(VECTORCHORD_REQUIRED_EXTENSIONS)


def test_extension_check_returns_installed_extension_map() -> None:
    connection = RecordingConnection(rows=[{"extname": "vector"}, {"extname": "vchord"}])
    store = PostgresVectorChordSearchStore(connection)

    installed = store.extension_check()

    assert installed == {
        "vector": True,
        "vchord": True,
        "pg_tokenizer": False,
        "vchord_bm25": False,
    }


def test_lexical_query_plan_prefers_vectorchord_bm25_by_default() -> None:
    plan = lexical_query_plan(
        workspace_id="ws_alice",
        query="crohn probiotics",
        limit=5,
        index_profile_ref="sip_default",
    )

    assert plan.mode == "lexical"
    assert "bm25_catalog.bm25_limit" in plan.statement.sql
    assert "c.bm25_document <&> to_bm25query" in plan.statement.sql
    assert "tokenize(%(query)s, sip.tokenizer)::bm25vector" in plan.statement.sql
    assert "%(bm25_index_name)s::regclass" in plan.statement.sql
    assert "c.workspace_id = %(workspace_id)s" in plan.statement.sql
    assert "v.active_transcript_version_id = c.transcript_version_id" in plan.statement.sql
    assert "sip.backend = %(storage_backend)s" in plan.statement.sql
    assert "ORDER BY bm25_score ASC" in plan.statement.sql
    assert plan.statement.params == {
        "workspace_id": "ws_alice",
        "query": "crohn probiotics",
        "index_profile_ref": "sip_default",
        "bm25_index_name": VECTORCHORD_BM25_CHUNKS_INDEX,
        "bm25_limit": 5,
        "storage_backend": "postgres_vectorchord",
        "limit": 5,
    }
    assert plan.usage.operation == "lexical_query"
    assert plan.usage.units == {"queries": 1, "candidate_limit": 5}
    assert plan.usage.backend == VECTORCHORD_BM25_LEXICAL_BACKEND
    assert plan.usage.metadata == {
        "storage_backend": "postgres_vectorchord",
        "lexical_sql_backend": VECTORCHORD_BM25_LEXICAL_BACKEND,
        "bm25_index_name": VECTORCHORD_BM25_CHUNKS_INDEX,
        "configured_fallback": False,
    }


def test_lexical_query_plan_uses_fts_only_when_configured_as_fallback() -> None:
    plan = lexical_query_plan(
        workspace_id="ws_alice",
        query="crohn probiotics",
        limit=5,
        index_profile_ref="sip_default",
        lexical_backend=POSTGRES_FTS_FALLBACK_LEXICAL_BACKEND,
    )

    assert "websearch_to_tsquery" in plan.statement.sql
    assert "fts_document @@ query.tsquery" in plan.statement.sql
    assert "c.bm25_document <&> to_bm25query" not in plan.statement.sql
    assert plan.usage.backend == POSTGRES_FTS_FALLBACK_LEXICAL_BACKEND
    assert plan.usage.metadata == {
        "storage_backend": "postgres_vectorchord",
        "lexical_sql_backend": POSTGRES_FTS_FALLBACK_LEXICAL_BACKEND,
        "configured_fallback": True,
        "fallback_reason": "configured_lexical_backend",
    }


def test_semantic_query_plan_uses_vector_distance_and_records_dimensions() -> None:
    plan = semantic_query_plan(
        workspace_id="ws_alice",
        query_vector=[0.1] * 1024,
        limit=7,
    )

    assert "ce.embedding <-> %(query_vector)s::vector(1024)" in plan.statement.sql
    assert "sip.embedding_dimension = %(embedding_dimension)s" in plan.statement.sql
    assert "ORDER BY vector_distance ASC" in plan.statement.sql
    assert "v.active_transcript_version_id = c.transcript_version_id" in plan.statement.sql
    assert plan.statement.params["query_vector"] == "[" + ",".join(["0.1"] * 1024) + "]"
    assert plan.statement.params["embedding_dimension"] == 1024
    assert plan.statement.params["workspace_id"] == "ws_alice"
    assert plan.usage.operation == "semantic_query"
    assert plan.usage.units["query_vector_dimensions"] == 1024
    assert plan.usage.metadata["storage_backend"] == "postgres_vectorchord"
    assert plan.usage.metadata["semantic_sql_backend"] == PGVECTOR_COMPATIBLE_SEMANTIC_BACKEND


def test_hybrid_query_plan_uses_rrf_fusion_and_candidate_multiplier() -> None:
    plan = hybrid_query_plan(
        workspace_id="ws_alice",
        query="resistance training",
        query_vector=[1.0] * 1024,
        limit=10,
        candidate_multiplier=3,
        rrf_k=50,
    )

    assert "FULL OUTER JOIN semantic USING (chunk_id)" in plan.statement.sql
    assert "1.0 / (%(rrf_k)s + lexical.lexical_rank)" in plan.statement.sql
    assert "1.0 / (%(rrf_k)s + semantic.semantic_rank)" in plan.statement.sql
    assert "c.bm25_document <&> to_bm25query" in plan.statement.sql
    assert "tokenize(%(query)s, sip.tokenizer)::bm25vector" in plan.statement.sql
    assert "ce.embedding <-> %(query_vector)s::vector(1024)" in plan.statement.sql
    assert "sip.embedding_dimension = %(embedding_dimension)s" in plan.statement.sql
    assert "c.workspace_id = %(workspace_id)s" in plan.statement.sql
    assert "ce.workspace_id = %(workspace_id)s" in plan.statement.sql
    assert plan.statement.sql.count("v.active_transcript_version_id = c.transcript_version_id") == 3
    assert plan.statement.params["candidate_limit"] == 30
    assert plan.statement.params["embedding_dimension"] == 1024
    assert plan.statement.params["bm25_index_name"] == VECTORCHORD_BM25_CHUNKS_INDEX
    assert plan.statement.params["bm25_limit"] == 30
    assert plan.statement.params["rrf_k"] == 50
    assert plan.usage.operation == "hybrid_query"
    assert plan.usage.backend == VECTORCHORD_BM25_PGVECTOR_HYBRID_BACKEND
    assert plan.usage.units["candidate_limit"] == 30
    assert plan.usage.units["result_limit"] == 10
    assert plan.usage.metadata["storage_backend"] == "postgres_vectorchord"
    assert plan.usage.metadata["lexical_sql_backend"] == VECTORCHORD_BM25_LEXICAL_BACKEND
    assert plan.usage.metadata["bm25_index_name"] == VECTORCHORD_BM25_CHUNKS_INDEX
    assert plan.usage.metadata["configured_fallback"] is False


def test_hybrid_query_plan_uses_fts_pgvector_only_when_configured_as_fallback() -> None:
    plan = hybrid_query_plan(
        workspace_id="ws_alice",
        query="resistance training",
        query_vector=[1.0] * 1024,
        limit=10,
        candidate_multiplier=3,
        rrf_k=50,
        lexical_backend=POSTGRES_FTS_FALLBACK_LEXICAL_BACKEND,
    )

    assert "fts_document @@ query.tsquery" in plan.statement.sql
    assert "c.bm25_document <&> to_bm25query" not in plan.statement.sql
    assert plan.usage.backend == POSTGRES_FTS_PGVECTOR_FALLBACK_BACKEND
    assert plan.usage.metadata["lexical_sql_backend"] == POSTGRES_FTS_FALLBACK_LEXICAL_BACKEND
    assert plan.usage.metadata["configured_fallback"] is True
    assert plan.usage.metadata["fallback_reason"] == "configured_lexical_backend"


def test_query_plans_cast_nullable_index_profile_ref_for_postgres() -> None:
    lexical = lexical_query_plan(workspace_id="ws_alice", query="resistance training", limit=5)
    semantic = semantic_query_plan(workspace_id="ws_alice", query_vector=[0.1] * 1024, limit=5)
    hybrid = hybrid_query_plan(workspace_id="ws_alice", query="resistance training", query_vector=[0.1] * 1024, limit=5)

    optional_profile_filter = "%(index_profile_ref)s::text IS NULL OR sip.id = %(index_profile_ref)s::text"
    assert optional_profile_filter in lexical.statement.sql
    assert optional_profile_filter in semantic.statement.sql
    assert hybrid.statement.sql.count(optional_profile_filter) == 2
    assert lexical.statement.params["index_profile_ref"] is None
    assert semantic.statement.params["index_profile_ref"] is None
    assert hybrid.statement.params["index_profile_ref"] is None


def test_search_store_executes_plan_and_adds_result_count_usage() -> None:
    connection = RecordingConnection(rows=[{"chunk_id": "chunk_1", "score": 1.0}])
    store = PostgresVectorChordSearchStore(connection, index_profile_ref="sip_default")

    rows, usage = store.lexical_search(workspace_id="ws_alice", query="crohn", limit=3)

    assert rows == [{"chunk_id": "chunk_1", "score": 1.0}]
    assert len(connection.calls) == 1
    assert connection.calls[0][1]["workspace_id"] == "ws_alice"
    assert connection.calls[0][1]["bm25_index_name"] == VECTORCHORD_BM25_CHUNKS_INDEX
    assert usage.operation == "lexical_query"
    assert usage.index_profile_ref == "sip_default"
    assert usage.backend == VECTORCHORD_BM25_LEXICAL_BACKEND
    assert usage.units["result_count"] == 1
    assert usage.units["latency_ms"] >= 0


def test_search_store_semantic_path_is_independent_of_lexical_backend() -> None:
    connection = RecordingConnection(rows=[{"chunk_id": "chunk_1", "score": 1.0}])
    store = PostgresVectorChordSearchStore(
        connection,
        lexical_backend=POSTGRES_FTS_FALLBACK_LEXICAL_BACKEND,
    )

    rows, usage = store.semantic_search(workspace_id="ws_alice", query_vector=[0.1] * 1024, limit=3)

    assert rows == [{"chunk_id": "chunk_1", "score": 1.0}]
    assert "fts_document" not in connection.calls[0][0]
    assert "ce.embedding <-> %(query_vector)s::vector(1024)" in connection.calls[0][0]
    assert usage.backend == "postgres_vectorchord"
    assert usage.metadata["semantic_sql_backend"] == PGVECTOR_COMPATIBLE_SEMANTIC_BACKEND


def test_search_store_hybrid_path_uses_configured_lexical_fallback() -> None:
    connection = RecordingConnection(rows=[{"chunk_id": "chunk_1", "score": 1.0}])
    store = PostgresVectorChordSearchStore(
        connection,
        lexical_backend=POSTGRES_FTS_FALLBACK_LEXICAL_BACKEND,
    )

    rows, usage = store.hybrid_search(
        workspace_id="ws_alice",
        query="crohn",
        query_vector=[0.1] * 1024,
        limit=3,
    )

    assert rows == [{"chunk_id": "chunk_1", "score": 1.0}]
    assert "fts_document @@ query.tsquery" in connection.calls[0][0]
    assert "c.bm25_document <&> to_bm25query" not in connection.calls[0][0]
    assert usage.backend == POSTGRES_FTS_PGVECTOR_FALLBACK_BACKEND
    assert usage.metadata["configured_fallback"] is True


def test_resource_sql_is_workspace_scoped_for_supported_hosts() -> None:
    statements = [
        chunk_resource_sql(workspace_id="ws_alice", chunk_id="chunk_1"),
        video_resource_sql(workspace_id="ws_alice", video_id="vid_1"),
        channel_resource_sql(workspace_id="ws_alice", channel_id="chan_1"),
        transcript_resource_sql(workspace_id="ws_alice", transcript_version_id="tx_1"),
        source_resource_sql(workspace_id="ws_alice", source_id="src_1"),
    ]

    assert all("%(workspace_id)s" in statement.sql for statement in statements)
    assert all(statement.params["workspace_id"] == "ws_alice" for statement in statements)
    assert "c.id = %(chunk_id)s" in statements[0].sql
    assert "v.id = %(video_id)s OR v.youtube_video_id = %(video_id)s" in statements[1].sql
    assert "v.channel_id = %(channel_id)s" in statements[2].sql
    assert "tv.id = %(transcript_version_id)s" in statements[3].sql
    assert "id = %(source_id)s" in statements[4].sql


def test_list_sql_helpers_are_workspace_scoped_and_parameterized() -> None:
    status = list_status_sql(workspace_id="ws_alice")
    videos = list_videos_sql(workspace_id="ws_alice", limit=5, offset=2, channel="chan_1", order_by="newest")
    channels = list_channels_sql(workspace_id="ws_alice", limit=6, offset=3, selected=True)

    assert "%(workspace_id)s" in status.sql
    assert "searchable_now" in status.sql
    assert "v.workspace_id = %(workspace_id)s" in videos.sql
    assert "ORDER BY v.published_at DESC NULLS LAST" in videos.sql
    assert videos.params == {
        "workspace_id": "ws_alice",
        "video_id": None,
        "channel": "chan_1",
        "limit": 5,
        "offset": 2,
    }
    assert "s.workspace_id = %(workspace_id)s" in channels.sql
    assert "s.selected = %(selected)s::boolean" in channels.sql
    assert channels.params["selected"] is True
    assert channels.params["limit"] == 6
    assert channels.params["offset"] == 3


def test_search_store_list_methods_format_postgres_rows() -> None:
    status_connection = RecordingConnection(
        rows=[
            {
                "searchable_now": 1,
                "still_indexing": 2,
                "needs_attention": 0,
                "channels": 3,
                "videos": 4,
                "chunks": 5,
                "transcript_versions": 6,
                "statuses": {"indexed": 1, "pending": 2},
            }
        ]
    )
    video_connection = RecordingConnection(
        rows=[
            {
                "video_id": "vid_1",
                "youtube_video_id": "yt_1",
                "source_id": "src_1",
                "active_transcript_version_id": "tx_1",
                "channel_id": "chan_1",
                "title": "Hosted Video",
                "description": "Description",
                "published_at": "2026-01-01T00:00:00Z",
                "duration_seconds": 60,
                "metadata_json": {"channel_title": "Hosted Channel"},
                "source_display_name": "Source Channel",
                "source_url": "https://youtube.com/@hosted",
                "source_type": "channel",
                "active_chunk_count": 2,
            }
        ]
    )
    channel_connection = RecordingConnection(
        rows=[
            {
                "channel_id": "chan_1",
                "title": "Hosted Channel",
                "channel_handle": "@hosted",
                "selected": True,
                "video_count": 4,
                "latest_published_at": "2026-01-01T00:00:00Z",
                "source_count": 1,
                "source_ids": ["src_1"],
            }
        ]
    )

    assert PostgresVectorChordSearchStore(status_connection).list_status(workspace_id="ws_alice")["videos"] == 4
    video = PostgresVectorChordSearchStore(video_connection).list_videos(workspace_id="ws_alice", limit=1)[0]
    channel = PostgresVectorChordSearchStore(channel_connection).list_channels(workspace_id="ws_alice", limit=1)[0]

    assert video["resource_uri"] == "yutome://video/vid_1"
    assert video["youtube_url"] == "https://youtube.com/watch?v=yt_1"
    assert video["channel_title"] == "Hosted Channel"
    assert channel["resource_uri"] == "yutome://channel/chan_1"
    assert channel["library_channel_id"] == "chan_1"
    assert channel["selected"] is True


def test_search_store_resource_methods_format_postgres_rows() -> None:
    connection = RecordingConnection(
        rows=[
            {
                "chunk_id": "chunk_1",
                "video_id": "vid_1",
                "youtube_video_id": "yt_1",
                "transcript_version_id": "tx_1",
                "chunk_index": 2,
                "start_seconds": 12,
                "end_seconds": 20,
                "text": "Hosted transcript text.",
                "chunk_metadata": {"token_count": 4, "chunking_version": "v1"},
                "title": "Hosted Video",
                "channel_id": "chan_1",
                "source_id": "src_1",
                "transcript_source": "captions",
                "language": "en",
                "transcript_metadata": {"is_generated": False},
            }
        ]
    )
    store = PostgresVectorChordSearchStore(connection)

    payload = store.resource_chunk(workspace_id="ws_alice", chunk_id="chunk_1")

    assert payload["resource_uri"] == "yutome://chunk/chunk_1"
    assert payload["youtube_url"] == "https://youtube.com/watch?v=yt_1&t=12s"
    assert payload["start_ms"] == 12000
    assert payload["end_ms"] == 20000
    assert payload["text"] == "Hosted transcript text."
    assert payload["token_count"] == 4
    assert payload["chunker_version"] == "v1"
    assert connection.calls[0][1] == {"workspace_id": "ws_alice", "chunk_id": "chunk_1"}


def test_transcript_resource_assembles_chunk_text_in_order() -> None:
    connection = RecordingConnection(
        rows=[
            {
                "transcript_version_id": "tx_1",
                "video_id": "vid_1",
                "youtube_video_id": "yt_1",
                "source": "captions",
                "language_code": "en",
                "content_hash": "hash_1",
                "metadata_json": {"is_generated": False},
                "active": True,
                "segment_count": 2,
                "chunk_id": "chunk_1",
                "chunk_index": 0,
                "start_seconds": 1,
                "end_seconds": 3,
                "text": "First chunk.",
            },
            {
                "transcript_version_id": "tx_1",
                "video_id": "vid_1",
                "youtube_video_id": "yt_1",
                "source": "captions",
                "language_code": "en",
                "content_hash": "hash_1",
                "metadata_json": {"is_generated": False},
                "active": True,
                "segment_count": 2,
                "chunk_id": "chunk_2",
                "chunk_index": 1,
                "start_seconds": 4,
                "end_seconds": 6,
                "text": "Second chunk.",
            },
        ]
    )
    store = PostgresVectorChordSearchStore(connection)

    payload = store.resource_transcript(workspace_id="ws_alice", transcript_version_id="tx_1", offset=0, limit=2)

    assert payload["resource_uri"] == "yutome://transcript/tx_1"
    assert payload["segment_count"] == 2
    assert payload["returned_segments"] == 2
    assert payload["text"] == "[0:01] First chunk.\n[0:04] Second chunk."
    assert connection.calls[0][1]["workspace_id"] == "ws_alice"
    assert connection.calls[0][1]["transcript_version_id"] == "tx_1"


def test_replace_active_transcript_sql_upserts_version_then_updates_video_pointer() -> None:
    statement = replace_active_transcript_sql(
        workspace_id="ws_alice",
        video_id="vid_1",
        transcript_version_id="tx_1",
        source="captions",
        language_code="en",
        content_hash="hash_1",
        metadata={"job_id": "job_1"},
    )

    assert "INSERT INTO transcript_versions" in statement.sql
    assert "is_active" not in statement.sql
    assert "ON CONFLICT (id) DO UPDATE" in statement.sql
    assert "UPDATE videos" in statement.sql
    assert "active_transcript_version_id = upserted.id" in statement.sql
    assert statement.params["workspace_id"] == "ws_alice"
    assert statement.params["metadata_json"] == '{"job_id":"job_1"}'


def test_query_plans_reject_non_positive_limits() -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        lexical_query_plan(workspace_id="ws", query="x", limit=0)


def test_vector_plans_reject_query_dimensions_that_do_not_match_profile() -> None:
    with pytest.raises(ValueError, match="query_vector dimension 3 does not match .* 1024"):
        semantic_query_plan(workspace_id="ws", query_vector=[0.1, 0.2, 0.3], limit=5)

    with pytest.raises(ValueError, match="query_vector dimension 2 does not match .* 1024"):
        hybrid_query_plan(workspace_id="ws", query="x", query_vector=[0.1, 0.2], limit=5)


def test_store_rejects_unsupported_profile_dimensions_clearly() -> None:
    with pytest.raises(ValueError, match=r"unsupported embedding profile .*vector\(1024\)"):
        PostgresVectorChordSearchStore(RecordingConnection(), embedding_dimension=512)

    with pytest.raises(ValueError, match=r"unsupported embedding profile .*vector\(1024\)"):
        validate_supported_embedding_profile(
            backend="postgres_vectorchord",
            embedding_model="voyage-4-lite",
            embedding_dimension=2048,
        )
