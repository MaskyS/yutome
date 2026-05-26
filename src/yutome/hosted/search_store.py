from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from yutome.hosted.migrations import (
    HOSTED_DEFAULT_EMBEDDING_DIMENSION,
    HOSTED_DEFAULT_EMBEDDING_MODEL,
    HOSTED_VECTOR_BACKEND,
)
from yutome.hosted.repositories import SqlStatement
from yutome.hosted.resources import HostedResourceQueries


class SearchStoreUsage(BaseModel):
    operation: str
    backend: str
    index_profile_ref: str | None = None
    units: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


SearchQueryMode = Literal["lexical", "semantic", "hybrid"]
SearchQuerySyntax = Literal["websearch", "plain", "tsquery"]


class SqlConnection(Protocol):
    def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> Any:
        ...


@dataclass(frozen=True)
class SearchStoreQueryPlan:
    mode: SearchQueryMode
    statement: SqlStatement
    usage: SearchStoreUsage


class SearchStore(Protocol):
    """Narrow hosted search-store contract for hosted Postgres search work."""

    def replace_active_transcript(self, *, workspace_id: str, video_id: str, transcript: dict[str, Any]) -> SearchStoreUsage:
        ...

    def lexical_search(self, *, workspace_id: str, query: str, limit: int) -> tuple[list[dict[str, Any]], SearchStoreUsage]:
        ...

    def semantic_search(self, *, workspace_id: str, query_vector: list[float], limit: int) -> tuple[list[dict[str, Any]], SearchStoreUsage]:
        ...

    def hybrid_search(
        self,
        *,
        workspace_id: str,
        query: str,
        query_vector: list[float],
        limit: int,
    ) -> tuple[list[dict[str, Any]], SearchStoreUsage]:
        ...

    def resource_chunk(self, *, workspace_id: str, chunk_id: str) -> dict[str, Any]:
        ...

    def resource_video(self, *, workspace_id: str, video_id: str) -> dict[str, Any]:
        ...

    def resource_channel(self, *, workspace_id: str, channel_id: str) -> dict[str, Any]:
        ...

    def resource_transcript(
        self,
        *,
        workspace_id: str,
        transcript_version_id: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        ...

    def resource_source(self, *, workspace_id: str, source_id: str) -> dict[str, Any]:
        ...


class PostgresVectorChordSearchStore:
    """Hosted search adapter for the VectorChord-first Postgres substrate.

    Dense-vector SQL uses pgvector-compatible operators. Lexical and hybrid
    recall currently use the native Postgres FTS fallback; they are labeled as
    fallback usage until real VectorChord-BM25 SQL is implemented.
    """

    backend = HOSTED_VECTOR_BACKEND

    def __init__(
        self,
        connection: SqlConnection,
        *,
        index_profile_ref: str | None = None,
        embedding_model: str = HOSTED_DEFAULT_EMBEDDING_MODEL,
        embedding_dimension: int = HOSTED_DEFAULT_EMBEDDING_DIMENSION,
    ) -> None:
        validate_supported_embedding_profile(
            backend=self.backend,
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
        )
        self.connection = connection
        self.index_profile_ref = index_profile_ref
        self.embedding_model = embedding_model
        self.embedding_dimension = embedding_dimension
        self.resources = HostedResourceQueries(connection)

    def extension_check(self) -> dict[str, bool]:
        result = self.connection.execute(extension_check_sql().sql, extension_check_sql().params)
        rows = _rows_from_result(result)
        installed = {str(row.get("extname")) for row in rows}
        return {extension: extension in installed for extension in VECTORCHORD_REQUIRED_EXTENSIONS}

    def replace_active_transcript(self, *, workspace_id: str, video_id: str, transcript: dict[str, Any]) -> SearchStoreUsage:
        statement = replace_active_transcript_sql(
            workspace_id=workspace_id,
            video_id=video_id,
            transcript_version_id=str(transcript["transcript_version_id"]),
            source=str(transcript.get("source", "hosted")),
            language_code=transcript.get("language_code"),
            content_hash=str(transcript["content_hash"]),
            metadata=transcript.get("metadata_json", {}),
        )
        self.connection.execute(statement.sql, statement.params)
        return SearchStoreUsage(
            operation="replace_active_transcript",
            backend=self.backend,
            index_profile_ref=self.index_profile_ref,
            units={"transcript_versions": 1},
            metadata={"video_id": video_id},
        )

    def lexical_search(self, *, workspace_id: str, query: str, limit: int) -> tuple[list[dict[str, Any]], SearchStoreUsage]:
        plan = lexical_query_plan(
            workspace_id=workspace_id,
            query=query,
            limit=limit,
            index_profile_ref=self.index_profile_ref,
        )
        return _execute_plan(self.connection, plan)

    def semantic_search(
        self,
        *,
        workspace_id: str,
        query_vector: list[float],
        limit: int,
    ) -> tuple[list[dict[str, Any]], SearchStoreUsage]:
        plan = semantic_query_plan(
            workspace_id=workspace_id,
            query_vector=query_vector,
            limit=limit,
            index_profile_ref=self.index_profile_ref,
            expected_dimension=self.embedding_dimension,
        )
        return _execute_plan(self.connection, plan)

    def hybrid_search(
        self,
        *,
        workspace_id: str,
        query: str,
        query_vector: list[float],
        limit: int,
    ) -> tuple[list[dict[str, Any]], SearchStoreUsage]:
        plan = hybrid_query_plan(
            workspace_id=workspace_id,
            query=query,
            query_vector=query_vector,
            limit=limit,
            index_profile_ref=self.index_profile_ref,
            expected_dimension=self.embedding_dimension,
        )
        return _execute_plan(self.connection, plan)

    def resource_chunk(self, *, workspace_id: str, chunk_id: str) -> dict[str, Any]:
        return self.resources.chunk(workspace_id=workspace_id, chunk_id=chunk_id)

    def resource_video(self, *, workspace_id: str, video_id: str) -> dict[str, Any]:
        return self.resources.video(workspace_id=workspace_id, video_id=video_id)

    def resource_channel(self, *, workspace_id: str, channel_id: str) -> dict[str, Any]:
        return self.resources.channel(workspace_id=workspace_id, channel_id=channel_id)

    def resource_transcript(
        self,
        *,
        workspace_id: str,
        transcript_version_id: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return self.resources.transcript(
            workspace_id=workspace_id,
            transcript_version_id=transcript_version_id,
            offset=offset,
            limit=limit,
        )

    def resource_source(self, *, workspace_id: str, source_id: str) -> dict[str, Any]:
        return self.resources.source(workspace_id=workspace_id, source_id=source_id)


VECTORCHORD_REQUIRED_EXTENSIONS = ("vector", "vchord", "pg_tokenizer", "vchord_bm25")
POSTGRES_FTS_FALLBACK_LEXICAL_BACKEND = "postgres_fts_fallback"
PGVECTOR_COMPATIBLE_SEMANTIC_BACKEND = "pgvector_vector_distance"
POSTGRES_FTS_PGVECTOR_FALLBACK_BACKEND = "postgres_fts_pgvector_fallback"
SUPPORTED_EMBEDDING_PROFILES = frozenset(
    {
        (
            HOSTED_VECTOR_BACKEND,
            HOSTED_DEFAULT_EMBEDDING_MODEL,
            HOSTED_DEFAULT_EMBEDDING_DIMENSION,
        )
    }
)


def validate_supported_embedding_profile(*, backend: str, embedding_model: str, embedding_dimension: int) -> None:
    profile = (backend, embedding_model, embedding_dimension)
    if profile not in SUPPORTED_EMBEDDING_PROFILES:
        supported = ", ".join(
            f"{supported_backend}/{supported_model}/{supported_dimension}d"
            for supported_backend, supported_model, supported_dimension in sorted(SUPPORTED_EMBEDDING_PROFILES)
        )
        raise ValueError(
            "unsupported embedding profile "
            f"{backend}/{embedding_model}/{embedding_dimension}d; "
            f"hosted storage is currently vector({HOSTED_DEFAULT_EMBEDDING_DIMENSION}) "
            f"with supported profiles: {supported}"
        )


def extension_check_sql(extensions: Sequence[str] = VECTORCHORD_REQUIRED_EXTENSIONS) -> SqlStatement:
    return SqlStatement(
        sql="""
SELECT extname
FROM pg_extension
WHERE extname = ANY(%(extensions)s)
ORDER BY extname;
""".strip(),
        params={"extensions": list(extensions)},
    )


def lexical_query_plan(
    *,
    workspace_id: str,
    query: str,
    limit: int,
    index_profile_ref: str | None = None,
    syntax: SearchQuerySyntax = "websearch",
) -> SearchStoreQueryPlan:
    _validate_limit(limit)
    statement = _postgres_fts_fallback_lexical_sql(
        workspace_id=workspace_id,
        query=query,
        limit=limit,
        index_profile_ref=index_profile_ref,
        syntax=syntax,
    )
    return SearchStoreQueryPlan(
        mode="lexical",
        statement=statement,
        usage=_usage(
            "lexical_query",
            index_profile_ref,
            {"queries": 1, "candidate_limit": limit},
            backend=POSTGRES_FTS_FALLBACK_LEXICAL_BACKEND,
            metadata={
                "storage_backend": HOSTED_VECTOR_BACKEND,
                "lexical_sql_backend": POSTGRES_FTS_FALLBACK_LEXICAL_BACKEND,
                "vectorchord_bm25_sql": "not_implemented",
            },
        ),
    )


def semantic_query_plan(
    *,
    workspace_id: str,
    query_vector: Sequence[float],
    limit: int,
    index_profile_ref: str | None = None,
    expected_dimension: int = HOSTED_DEFAULT_EMBEDDING_DIMENSION,
) -> SearchStoreQueryPlan:
    _validate_limit(limit)
    _validate_query_vector_dimension(query_vector, expected_dimension)
    statement = _semantic_sql(
        workspace_id=workspace_id,
        query_vector=query_vector,
        limit=limit,
        index_profile_ref=index_profile_ref,
        embedding_dimension=expected_dimension,
    )
    return SearchStoreQueryPlan(
        mode="semantic",
        statement=statement,
        usage=_usage(
            "semantic_query",
            index_profile_ref,
            {"queries": 1, "candidate_limit": limit, "query_vector_dimensions": len(query_vector)},
            metadata={
                "storage_backend": HOSTED_VECTOR_BACKEND,
                "semantic_sql_backend": PGVECTOR_COMPATIBLE_SEMANTIC_BACKEND,
            },
        ),
    )


def hybrid_query_plan(
    *,
    workspace_id: str,
    query: str,
    query_vector: Sequence[float],
    limit: int,
    index_profile_ref: str | None = None,
    candidate_multiplier: int = 4,
    rrf_k: int = 60,
    syntax: SearchQuerySyntax = "websearch",
    expected_dimension: int = HOSTED_DEFAULT_EMBEDDING_DIMENSION,
) -> SearchStoreQueryPlan:
    _validate_limit(limit)
    _validate_positive("candidate_multiplier", candidate_multiplier)
    _validate_positive("rrf_k", rrf_k)
    _validate_query_vector_dimension(query_vector, expected_dimension)
    candidate_limit = limit * candidate_multiplier
    statement = _postgres_fts_fallback_hybrid_sql(
        workspace_id=workspace_id,
        query=query,
        query_vector=query_vector,
        limit=limit,
        candidate_limit=candidate_limit,
        index_profile_ref=index_profile_ref,
        rrf_k=rrf_k,
        syntax=syntax,
        embedding_dimension=expected_dimension,
    )
    return SearchStoreQueryPlan(
        mode="hybrid",
        statement=statement,
        usage=_usage(
            "hybrid_query",
            index_profile_ref,
            {
                "queries": 1,
                "candidate_limit": candidate_limit,
                "result_limit": limit,
                "query_vector_dimensions": len(query_vector),
            },
            backend=POSTGRES_FTS_PGVECTOR_FALLBACK_BACKEND,
            metadata={
                "storage_backend": HOSTED_VECTOR_BACKEND,
                "lexical_sql_backend": POSTGRES_FTS_FALLBACK_LEXICAL_BACKEND,
                "semantic_sql_backend": PGVECTOR_COMPATIBLE_SEMANTIC_BACKEND,
                "fusion": "rrf",
                "vectorchord_bm25_sql": "not_implemented",
            },
        ),
    )


def replace_active_transcript_sql(
    *,
    workspace_id: str,
    video_id: str,
    transcript_version_id: str,
    source: str,
    language_code: str | None,
    content_hash: str,
    metadata: Mapping[str, Any] | None = None,
) -> SqlStatement:
    return SqlStatement(
        sql="""
WITH upserted AS (
    INSERT INTO transcript_versions (
        id, workspace_id, video_id, source, language_code,
        content_hash, metadata_json
    )
    VALUES (
        %(transcript_version_id)s, %(workspace_id)s, %(video_id)s, %(source)s,
        %(language_code)s, %(content_hash)s, %(metadata_json)s::jsonb
    )
    ON CONFLICT (id) DO UPDATE
    SET source = EXCLUDED.source,
        language_code = EXCLUDED.language_code,
        content_hash = EXCLUDED.content_hash,
        metadata_json = EXCLUDED.metadata_json
    RETURNING *
),
activated AS (
    UPDATE videos
    SET active_transcript_version_id = upserted.id,
        updated_at = now()
    FROM upserted
    WHERE videos.id = upserted.video_id
      AND videos.workspace_id = upserted.workspace_id
    RETURNING upserted.*
)
SELECT * FROM activated;
""".strip(),
        params={
            "workspace_id": workspace_id,
            "video_id": video_id,
            "transcript_version_id": transcript_version_id,
            "source": source,
            "language_code": language_code,
            "content_hash": content_hash,
            "metadata_json": _json_param(metadata or {}),
        },
    )


def _postgres_fts_fallback_lexical_sql(
    *,
    workspace_id: str,
    query: str,
    limit: int,
    index_profile_ref: str | None,
    syntax: SearchQuerySyntax,
) -> SqlStatement:
    query_expression = _query_expression_sql(syntax)
    return SqlStatement(
        sql=f"""
WITH query AS (
    SELECT {query_expression} AS tsquery
)
SELECT
    c.id AS chunk_id,
    c.video_id,
    v.youtube_video_id,
    c.transcript_version_id,
    c.chunk_index,
    c.start_seconds,
    c.end_seconds,
    c.text,
    v.title,
    v.channel_id,
    v.published_at,
    v.duration_seconds,
    v.metadata_json->>'channel_title' AS channel_title,
    v.metadata_json->>'channel_handle' AS channel_handle,
    v.metadata_json->>'thumbnail_url' AS thumbnail_url,
    ts_rank_cd(c.fts_document, query.tsquery) AS lexical_score,
    NULL::double precision AS vector_distance,
    ts_rank_cd(c.fts_document, query.tsquery) AS score,
    'lexical' AS match_type
FROM chunks c
JOIN videos v ON v.id = c.video_id
    AND v.workspace_id = c.workspace_id
    AND v.active_transcript_version_id = c.transcript_version_id
JOIN search_index_profiles sip ON sip.id = c.index_profile_id
CROSS JOIN query
WHERE c.workspace_id = %(workspace_id)s
  AND c.fts_document @@ query.tsquery
  AND (%(index_profile_ref)s::text IS NULL OR sip.id = %(index_profile_ref)s::text)
ORDER BY lexical_score DESC, c.video_id, c.chunk_index
LIMIT %(limit)s;
""".strip(),
        params={
            "workspace_id": workspace_id,
            "query": query,
            "index_profile_ref": index_profile_ref,
            "limit": limit,
        },
    )


def _semantic_sql(
    *,
    workspace_id: str,
    query_vector: Sequence[float],
    limit: int,
    index_profile_ref: str | None,
    embedding_dimension: int,
) -> SqlStatement:
    return SqlStatement(
        sql=f"""
SELECT
    c.id AS chunk_id,
    c.video_id,
    v.youtube_video_id,
    c.transcript_version_id,
    c.chunk_index,
    c.start_seconds,
    c.end_seconds,
    c.text,
    v.title,
    v.channel_id,
    v.published_at,
    v.duration_seconds,
    v.metadata_json->>'channel_title' AS channel_title,
    v.metadata_json->>'channel_handle' AS channel_handle,
    v.metadata_json->>'thumbnail_url' AS thumbnail_url,
    NULL::double precision AS lexical_score,
    ce.embedding <-> %(query_vector)s::vector({embedding_dimension}) AS vector_distance,
    1.0 / (1.0 + (ce.embedding <-> %(query_vector)s::vector({embedding_dimension}))) AS score,
    'semantic' AS match_type
FROM chunk_embeddings ce
JOIN chunks c ON c.id = ce.chunk_id AND c.workspace_id = ce.workspace_id
JOIN videos v ON v.id = c.video_id
    AND v.workspace_id = c.workspace_id
    AND v.active_transcript_version_id = c.transcript_version_id
JOIN search_index_profiles sip ON sip.id = ce.index_profile_id
WHERE ce.workspace_id = %(workspace_id)s
  AND (%(index_profile_ref)s::text IS NULL OR sip.id = %(index_profile_ref)s::text)
  AND sip.embedding_dimension = %(embedding_dimension)s
ORDER BY vector_distance ASC, c.video_id, c.chunk_index
LIMIT %(limit)s;
""".strip(),
        params={
            "workspace_id": workspace_id,
            "query_vector": _vector_literal(query_vector),
            "embedding_dimension": embedding_dimension,
            "index_profile_ref": index_profile_ref,
            "limit": limit,
        },
    )


def _postgres_fts_fallback_hybrid_sql(
    *,
    workspace_id: str,
    query: str,
    query_vector: Sequence[float],
    limit: int,
    candidate_limit: int,
    index_profile_ref: str | None,
    rrf_k: int,
    syntax: SearchQuerySyntax,
    embedding_dimension: int,
) -> SqlStatement:
    query_expression = _query_expression_sql(syntax)
    return SqlStatement(
        sql=f"""
WITH query AS (
    SELECT {query_expression} AS tsquery
),
lexical AS (
    SELECT
        c.id AS chunk_id,
        ts_rank_cd(c.fts_document, query.tsquery) AS lexical_score,
        row_number() OVER (ORDER BY ts_rank_cd(c.fts_document, query.tsquery) DESC, c.id) AS lexical_rank
    FROM chunks c
    JOIN videos v ON v.id = c.video_id
        AND v.workspace_id = c.workspace_id
        AND v.active_transcript_version_id = c.transcript_version_id
    JOIN search_index_profiles sip ON sip.id = c.index_profile_id
    CROSS JOIN query
    WHERE c.workspace_id = %(workspace_id)s
      AND c.fts_document @@ query.tsquery
      AND (%(index_profile_ref)s::text IS NULL OR sip.id = %(index_profile_ref)s::text)
    ORDER BY lexical_score DESC, c.id
    LIMIT %(candidate_limit)s
),
semantic AS (
    SELECT
        c.id AS chunk_id,
        ce.embedding <-> %(query_vector)s::vector({embedding_dimension}) AS vector_distance,
        row_number() OVER (ORDER BY ce.embedding <-> %(query_vector)s::vector({embedding_dimension}) ASC, c.id) AS semantic_rank
    FROM chunk_embeddings ce
    JOIN chunks c ON c.id = ce.chunk_id AND c.workspace_id = ce.workspace_id
    JOIN videos v ON v.id = c.video_id
        AND v.workspace_id = c.workspace_id
        AND v.active_transcript_version_id = c.transcript_version_id
    JOIN search_index_profiles sip ON sip.id = ce.index_profile_id
    WHERE ce.workspace_id = %(workspace_id)s
      AND (%(index_profile_ref)s::text IS NULL OR sip.id = %(index_profile_ref)s::text)
      AND sip.embedding_dimension = %(embedding_dimension)s
    ORDER BY vector_distance ASC, c.id
    LIMIT %(candidate_limit)s
),
fused AS (
    SELECT
        COALESCE(lexical.chunk_id, semantic.chunk_id) AS chunk_id,
        lexical.lexical_score,
        semantic.vector_distance,
        COALESCE(1.0 / (%(rrf_k)s + lexical.lexical_rank), 0.0)
          + COALESCE(1.0 / (%(rrf_k)s + semantic.semantic_rank), 0.0) AS score
    FROM lexical
    FULL OUTER JOIN semantic USING (chunk_id)
)
SELECT
    c.id AS chunk_id,
    c.video_id,
    v.youtube_video_id,
    c.transcript_version_id,
    c.chunk_index,
    c.start_seconds,
    c.end_seconds,
    c.text,
    v.title,
    v.channel_id,
    v.published_at,
    v.duration_seconds,
    v.metadata_json->>'channel_title' AS channel_title,
    v.metadata_json->>'channel_handle' AS channel_handle,
    v.metadata_json->>'thumbnail_url' AS thumbnail_url,
    fused.lexical_score,
    fused.vector_distance,
    fused.score,
    'hybrid' AS match_type
FROM fused
JOIN chunks c ON c.id = fused.chunk_id
JOIN videos v ON v.id = c.video_id
    AND v.workspace_id = c.workspace_id
    AND v.active_transcript_version_id = c.transcript_version_id
WHERE c.workspace_id = %(workspace_id)s
ORDER BY fused.score DESC, c.video_id, c.chunk_index
LIMIT %(limit)s;
""".strip(),
        params={
            "workspace_id": workspace_id,
            "query": query,
            "query_vector": _vector_literal(query_vector),
            "embedding_dimension": embedding_dimension,
            "index_profile_ref": index_profile_ref,
            "candidate_limit": candidate_limit,
            "rrf_k": rrf_k,
            "limit": limit,
        },
    )


def _execute_plan(connection: SqlConnection, plan: SearchStoreQueryPlan) -> tuple[list[dict[str, Any]], SearchStoreUsage]:
    started = perf_counter()
    result = connection.execute(plan.statement.sql, plan.statement.params)
    rows = _rows_from_result(result)
    usage = plan.usage.model_copy(
        update={
            "units": {
                **plan.usage.units,
                "result_count": len(rows),
                "latency_ms": round((perf_counter() - started) * 1000, 3),
            }
        }
    )
    return rows, usage


def _rows_from_result(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if hasattr(result, "mappings"):
        return [dict(row) for row in result.mappings()]
    if hasattr(result, "fetchall"):
        rows = result.fetchall()
    elif isinstance(result, Iterable) and not isinstance(result, (str, bytes, Mapping)):
        rows = list(result)
    else:
        return []
    return [dict(row) for row in rows]


def _query_expression_sql(syntax: SearchQuerySyntax) -> str:
    if syntax == "plain":
        return "plainto_tsquery('english', %(query)s)"
    if syntax == "tsquery":
        return "to_tsquery('english', %(query)s)"
    return "websearch_to_tsquery('english', %(query)s)"


def _usage(
    operation: str,
    index_profile_ref: str | None,
    units: dict[str, float | int | str | bool | None],
    *,
    backend: str = PostgresVectorChordSearchStore.backend,
    metadata: dict[str, Any] | None = None,
) -> SearchStoreUsage:
    return SearchStoreUsage(
        operation=operation,
        backend=backend,
        index_profile_ref=index_profile_ref,
        units=units,
        metadata=metadata or {},
    )


def _json_param(value: Mapping[str, Any]) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(value):.12g}" for value in vector) + "]"


def _validate_limit(limit: int) -> None:
    _validate_positive("limit", limit)


def _validate_query_vector_dimension(query_vector: Sequence[float], expected_dimension: int) -> None:
    actual_dimension = len(query_vector)
    if actual_dimension != expected_dimension:
        raise ValueError(
            f"query_vector dimension {actual_dimension} does not match "
            f"the hosted embedding profile dimension {expected_dimension}"
        )


def _validate_positive(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


__all__ = [
    "PostgresVectorChordSearchStore",
    "SearchQueryMode",
    "SearchQuerySyntax",
    "SearchStore",
    "SearchStoreQueryPlan",
    "SearchStoreUsage",
    "PGVECTOR_COMPATIBLE_SEMANTIC_BACKEND",
    "POSTGRES_FTS_PGVECTOR_FALLBACK_BACKEND",
    "POSTGRES_FTS_FALLBACK_LEXICAL_BACKEND",
    "SUPPORTED_EMBEDDING_PROFILES",
    "VECTORCHORD_REQUIRED_EXTENSIONS",
    "extension_check_sql",
    "hybrid_query_plan",
    "lexical_query_plan",
    "replace_active_transcript_sql",
    "semantic_query_plan",
    "validate_supported_embedding_profile",
]
