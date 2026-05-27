from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from yutome.config import AppConfig
from yutome.db import connect_catalog
from yutome.embeddings import (
    LANCEDB_CHUNKS_TABLE,
    _embed_voyage_query,
    _lancedb_has_table,
    _lancedb_missing_chunk_columns,
)
from yutome.paths import ProjectPaths
from yutome.retrieval import _format_hit, _snippet

EntityName = Literal["chunk", "video", "channel"]
SearchOver = Literal["chunk_text", "video_title", "video_description"]
SearchMode = Literal["lexical", "semantic", "hybrid", "none"]
GroupKey = Literal["video", "channel", "transcript_source"]
SortDirection = Literal["asc", "desc"]
ProjectionName = Literal[
    "thin",
    "chunk",
    "metadata",
    "video_card",
    "video_attention",
    "channel_card",
    "group_video",
    "status_breakdown",
]
PlanKind = Literal[
    "sql_chunk",
    "lexical_chunk",
    "lance_chunk",
    "two_stage",
    "sql_video",
    "lexical_video",
    "sql_channel",
    "status_breakdown",
]


class StringPredicate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    eq: str | None = None
    in_: list[str] | None = Field(default=None, alias="in")
    not_in: list[str] | None = None
    starts_with: str | None = None
    starts_with_any: list[str] | None = None


class IntPredicate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    eq: int | None = None
    gte: int | None = None
    lte: int | None = None
    in_: list[int] | None = Field(default=None, alias="in")


class DateRange(BaseModel):
    gte: str | None = None
    lte: str | None = None


class BoolPredicate(BaseModel):
    eq: bool | None = None


class Filter(BaseModel):
    video_id: StringPredicate | None = None
    channel_id: StringPredicate | None = None
    channel_handle: StringPredicate | None = None
    published_at: DateRange | None = None
    duration_seconds: IntPredicate | None = None
    ingest_status: StringPredicate | None = None
    live_status: StringPredicate | None = None
    transcript_source: StringPredicate | None = None
    language: StringPredicate | None = None
    is_generated: BoolPredicate | None = None
    transcript_active: BoolPredicate | None = None
    chunk_id: StringPredicate | None = None
    sequence: IntPredicate | None = None
    start_ms: IntPredicate | None = None
    token_count: IntPredicate | None = None
    last_attempt_status: StringPredicate | None = None
    last_attempt_tool: StringPredicate | None = None
    last_attempt_error_class: StringPredicate | None = None
    last_attempt_retryable: BoolPredicate | None = None
    last_attempt_created_at: DateRange | None = None
    channel_selected: BoolPredicate | None = None
    channel_last_synced_at: DateRange | None = None


class Search(BaseModel):
    over: SearchOver = "chunk_text"
    mode: SearchMode = "hybrid"
    text: str = ""
    raw: bool = False
    """When False (default), lexical search wraps `text` as an FTS5 phrase
    so characters like `-`, `:`, `*`, `+` are treated literally. When True,
    the text is passed through unmodified so power users can write raw
    FTS5 query syntax (`AND`/`OR`/`NOT`, prefix `*`, column filters)."""


class OrderBy(BaseModel):
    field: Literal[
        "score",
        "published_at",
        "duration_seconds",
        "title",
        "ingest_status",
        "sequence",
        "start_ms",
        "last_attempt_created_at",
    ]
    direction: SortDirection = "desc"


class QueryRequest(BaseModel):
    entity: EntityName = "chunk"
    search: Search | None = None
    filter: Filter = Field(default_factory=Filter)
    group_by: GroupKey | None = None
    order_by: list[OrderBy] = Field(default_factory=list)
    project: ProjectionName = "thin"
    limit: int = Field(default=10, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
    per_group_limit: int = Field(default=3, ge=1, le=20)


class QueryResult(BaseModel):
    rows: list[dict[str, Any]]
    notes: list[str] = Field(default_factory=list)
    total: int | None = None


@dataclass(frozen=True)
class CompiledQuery:
    request: QueryRequest
    kind: PlanKind
    notes: list[str] = field(default_factory=list)


LANCE_NATIVE_FIELDS = {
    "video_id",
    "channel_id",
    "chunk_id",
    "transcript_source",
    "language",
    "is_generated",
    "sequence",
    "start_ms",
    "token_count",
}

SQLITE_REQUIRED_FOR_LANCE = {
    "channel_handle",
    "published_at",
    "duration_seconds",
    "ingest_status",
    "live_status",
    "channel_selected",
    "channel_last_synced_at",
}

LAST_ATTEMPT_FIELDS = {
    "last_attempt_status",
    "last_attempt_tool",
    "last_attempt_error_class",
    "last_attempt_retryable",
    "last_attempt_created_at",
}


def compile_query(request: QueryRequest) -> CompiledQuery:
    notes: list[str] = []
    search = request.search
    if search is not None and search.mode != "none" and not search.text.strip():
        search = search.model_copy(update={"mode": "none"})
        request = request.model_copy(update={"search": search})
        notes.append("empty_search_text_downgraded_to_none")

    if request.group_by is None and "per_group_limit" in request.model_fields_set:
        raise ValueError("per_group_limit requires group_by")

    fields = _present_filter_fields(request.filter)
    if request.entity != "video" and fields & LAST_ATTEMPT_FIELDS:
        raise ValueError("last_attempt_* filters require entity=video with project=video_attention")

    if request.project == "status_breakdown":
        return CompiledQuery(request=request, kind="status_breakdown", notes=notes)

    mode: SearchMode = "none" if search is None else search.mode
    if mode in {"semantic", "hybrid"} and (search is None or search.over != "chunk_text"):
        raise ValueError("semantic and hybrid search are only supported for chunk_text")

    if request.entity == "chunk":
        if mode == "none":
            return CompiledQuery(request=request, kind="sql_chunk", notes=notes)
        if mode == "lexical":
            return CompiledQuery(request=request, kind="lexical_chunk", notes=notes)
        if request.filter.transcript_active and request.filter.transcript_active.eq is False:
            raise ValueError("semantic and hybrid search only support active transcript chunks")
        kind: PlanKind = "two_stage" if fields & SQLITE_REQUIRED_FOR_LANCE else "lance_chunk"
        return CompiledQuery(request=request, kind=kind, notes=notes)

    if request.entity == "video":
        if mode == "none":
            return CompiledQuery(request=request, kind="sql_video", notes=notes)
        if mode == "lexical" and search and search.over in {"video_title", "video_description"}:
            return CompiledQuery(request=request, kind="lexical_video", notes=notes)
        raise ValueError("video search only supports lexical over video_title or video_description")

    if request.entity == "channel":
        if mode != "none":
            raise ValueError("channel queries do not support search")
        return CompiledQuery(request=request, kind="sql_channel", notes=notes)

    raise ValueError(f"unsupported query entity: {request.entity}")


def execute_query(compiled: CompiledQuery, config: AppConfig, paths: ProjectPaths) -> QueryResult:
    request = compiled.request
    with connect_catalog(paths.catalog_db) as connection:
        if compiled.kind == "status_breakdown":
            rows = [_status_breakdown(connection)]
        elif compiled.kind == "sql_chunk":
            rows = _execute_sql_chunk(connection, request=request, lexical=False)
        elif compiled.kind == "lexical_chunk":
            rows = _execute_sql_chunk(connection, request=request, lexical=True)
        elif compiled.kind == "sql_video":
            rows = _execute_sql_video(connection, request=request, lexical=False)
        elif compiled.kind == "lexical_video":
            rows = _execute_sql_video(connection, request=request, lexical=True)
        elif compiled.kind == "sql_channel":
            rows = _execute_sql_channel(connection, request=request)
        elif compiled.kind in {"lance_chunk", "two_stage"}:
            if not _vectors_available_for_chunks(config, paths):
                vector_message = (
                    "Vector search unavailable. Configure VOYAGE_API_KEY and run "
                    "`yutome corpus rebuild vectors` to enable hybrid/semantic recall."
                )
                if request.search is not None and request.search.mode == "semantic":
                    raise RuntimeError(vector_message)
                # Fall back to lexical FTS when Voyage credentials or vector
                # rows are missing, so default search still returns results.
                lexical_request = request.model_copy(deep=True)
                if lexical_request.search is not None:
                    lexical_request.search = lexical_request.search.model_copy(update={"mode": "lexical"})
                lexical_compiled = compile_query(lexical_request)
                fallback_result = execute_query(lexical_compiled, config, paths)
                fallback_result.notes = [
                    *compiled.notes,
                    f"{vector_message} Ran lexical search instead.",
                    *fallback_result.notes,
                ]
                return fallback_result
            rows, lance_notes = _execute_lance_chunk(
                connection,
                config=config,
                paths=paths,
                request=request,
                two_stage=compiled.kind == "two_stage",
            )
            return QueryResult(rows=rows, notes=[*compiled.notes, *lance_notes], total=len(rows))
        else:
            raise ValueError(f"unsupported compiled plan: {compiled.kind}")
    return QueryResult(rows=rows, notes=compiled.notes, total=len(rows))


def _fts5_phrase(text: str) -> str:
    """Wrap user input as an FTS5 phrase so characters like `-`, `:`, `*`,
    `+`, `(`, `)`, `^` are treated as literal tokenizer input instead of
    FTS5 operators. Per https://www.sqlite.org/fts5.html section 3.1,
    embedded `"` characters inside a phrase escape by doubling."""
    return '"' + text.replace('"', '""') + '"'


def _fts5_match_text(search: Search) -> str:
    return search.text if search.raw else _fts5_phrase(search.text)


def _present_filter_fields(filters: Filter) -> set[str]:
    return {name for name in type(filters).model_fields if getattr(filters, name) is not None}


def _execute_sql_chunk(connection: sqlite3.Connection, *, request: QueryRequest, lexical: bool) -> list[dict[str, Any]]:
    params: list[Any] = []
    if lexical:
        match_text = _fts5_match_text(request.search) if request.search else '""'
        params.append(match_text)
        from_sql = """
        FROM chunks_fts
        JOIN chunks c ON chunks_fts.rowid = c.rowid
        JOIN transcript_versions tv ON tv.transcript_version_id = c.transcript_version_id
        LEFT JOIN videos v ON v.video_id = c.video_id
        LEFT JOIN channels ch ON ch.channel_id = COALESCE(c.channel_id, v.channel_id)
        LEFT JOIN library_channels lc ON lc.channel_id = ch.channel_id
        WHERE chunks_fts MATCH ?
        """
        score_sql = "snippet(chunks_fts, 0, '[', ']', '...', 32) AS snippet, bm25(chunks_fts) AS lexical_score"
    else:
        from_sql = """
        FROM chunks c
        JOIN transcript_versions tv ON tv.transcript_version_id = c.transcript_version_id
        LEFT JOIN videos v ON v.video_id = c.video_id
        LEFT JOIN channels ch ON ch.channel_id = COALESCE(c.channel_id, v.channel_id)
        LEFT JOIN library_channels lc ON lc.channel_id = ch.channel_id
        WHERE 1 = 1
        """
        score_sql = "NULL AS snippet, NULL AS lexical_score"

    clauses, filter_params = _chunk_filter_sql(request.filter)
    params.extend(filter_params)
    sql = f"""
        SELECT
            c.chunk_id,
            c.transcript_version_id,
            c.video_id,
            c.channel_id,
            c.sequence,
            c.start_ms,
            c.end_ms,
            c.text,
            c.token_count,
            c.text_hash,
            c.chunker_version,
            v.title,
            v.description,
            v.duration_seconds,
            v.published_at,
            v.live_status,
            v.thumbnail_url,
            v.metadata_hash,
            v.ingest_status,
            tv.source AS transcript_source,
            tv.language,
            tv.is_generated,
            {score_sql}
        {from_sql}
        {' AND ' + ' AND '.join(clauses) if clauses else ''}
        {_order_sql(request, entity='chunk', lexical=lexical)}
        LIMIT ? OFFSET ?
    """
    params.extend([request.limit, request.offset])
    rows = [_row_dict(row, match_type="lexical" if lexical else "filter") for row in connection.execute(sql, params)]
    return _project_chunk_rows(rows, request=request)


def _execute_sql_video(connection: sqlite3.Connection, *, request: QueryRequest, lexical: bool) -> list[dict[str, Any]]:
    params: list[Any] = []
    if lexical:
        assert request.search is not None
        column = "title" if request.search.over == "video_title" else "description"
        # Wrap as an FTS5 phrase so special chars like `-` `:` `*` are literal,
        # but keep the `column:` prefix so search stays scoped to that column.
        # Power users pass --raw to inject their own FTS5 expression here.
        inner = request.search.text if request.search.raw else _fts5_phrase(request.search.text)
        params.append(f"{column}:({inner})")
        from_sql = """
        FROM videos_fts
        JOIN videos v ON videos_fts.rowid = v.rowid
        LEFT JOIN channels ch ON ch.channel_id = v.channel_id
        LEFT JOIN library_channels lc ON lc.channel_id = ch.channel_id
        LEFT JOIN transcript_versions atv ON atv.video_id = v.video_id AND atv.active = 1
        LEFT JOIN transcript_attempts la
          ON la.attempt_id = (
            SELECT MAX(attempt_id)
            FROM transcript_attempts
            WHERE video_id = v.video_id
          )
        WHERE videos_fts MATCH ?
        """
        score_sql = "bm25(videos_fts) AS lexical_score"
    else:
        from_sql = _VIDEO_FROM_SQL + " WHERE 1 = 1"
        score_sql = "NULL AS lexical_score"

    clauses, filter_params = _video_filter_sql(request.filter)
    params.extend(filter_params)
    sql = f"""
        SELECT
            {_VIDEO_SELECT_SQL},
            {score_sql}
        {from_sql}
        {' AND ' + ' AND '.join(clauses) if clauses else ''}
        {_order_sql(request, entity='video', lexical=lexical)}
        LIMIT ? OFFSET ?
    """
    params.extend([request.limit, request.offset])
    rows = [_row_dict(row, match_type="lexical" if lexical else "filter") for row in connection.execute(sql, params)]
    for row in rows:
        if row.get("lexical_score") is not None:
            row["score"] = -float(row["lexical_score"])
    return [_project_video_row(row, project=request.project) for row in rows]


def _execute_sql_channel(connection: sqlite3.Connection, *, request: QueryRequest) -> list[dict[str, Any]]:
    params: list[Any] = []
    clauses, filter_params = _channel_filter_sql(request.filter)
    params.extend(filter_params)
    sql = f"""
        SELECT
            COALESCE(ch.channel_id, lc.channel_id) AS channel_id,
            COALESCE(ch.handle, lc.handle) AS handle,
            COALESCE(ch.title, lc.title) AS title,
            ch.source_url,
            ch.uploads_url,
            ch.first_synced_at,
            ch.last_synced_at,
            lc.library_channel_id,
            lc.source AS library_source,
            lc.source_url AS library_source_url,
            lc.selected,
            (
                SELECT COUNT(*)
                FROM videos v
                WHERE v.channel_id = COALESCE(ch.channel_id, lc.channel_id)
            ) AS video_count,
            (
                SELECT COUNT(*)
                FROM videos v
                WHERE v.channel_id = COALESCE(ch.channel_id, lc.channel_id)
                  AND v.ingest_status = 'indexed'
            ) AS indexed_count
        FROM library_channels lc
        LEFT JOIN channels ch ON ch.channel_id = lc.channel_id
        WHERE 1 = 1
        {' AND ' + ' AND '.join(clauses) if clauses else ''}
        ORDER BY lc.selected DESC, COALESCE(ch.title, lc.title, ch.handle, lc.handle, lc.source_url)
        LIMIT ? OFFSET ?
    """
    params.extend([request.limit, request.offset])
    return [_project_channel_row(dict(row)) for row in connection.execute(sql, params)]


def _vectors_available_for_chunks(config: AppConfig, paths: ProjectPaths) -> bool:
    """Return True only when a vector search over the chunks table can succeed
    end-to-end. Used by `execute_query` to fall back to lexical FTS when the
    user hasn't set up embeddings yet, instead of raising a setup error."""
    if config.vectors.backend != "lancedb" or not config.vectors.enabled:
        return False
    if config.embeddings.provider != "voyage":
        return False
    if not _voyage_credentials_available():
        return False
    try:
        import lancedb
    except ImportError:
        return False
    try:
        db = lancedb.connect(paths.lancedb_dir)
    except Exception:
        return False
    if not _lancedb_has_table(db, LANCEDB_CHUNKS_TABLE):
        return False
    return True


def _voyage_credentials_available() -> bool:
    """Voyage's client raises at construction time when no key is configured.

    Treat a missing key like a missing vector backend so default hybrid search
    falls back to lexical, matching the setup copy and README. Invalid keys
    still surface from the provider when a user explicitly configured one.
    """
    if os.environ.get("VOYAGE_API_KEY") or os.environ.get("VOYAGE_API_KEY_PATH"):
        return True
    try:
        import voyageai
    except ImportError:
        return False
    return bool(getattr(voyageai, "api_key", None) or getattr(voyageai, "api_key_path", None))


def _execute_lance_chunk(
    connection: sqlite3.Connection,
    *,
    config: AppConfig,
    paths: ProjectPaths,
    request: QueryRequest,
    two_stage: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    if config.vectors.backend != "lancedb" or not config.vectors.enabled:
        raise RuntimeError("LanceDB vector backend disabled")
    if config.embeddings.provider != "voyage":
        raise RuntimeError(f"unsupported embedding provider: {config.embeddings.provider}")
    try:
        import lancedb
    except ImportError as exc:
        raise RuntimeError("lancedb is not installed") from exc

    assert request.search is not None
    db = lancedb.connect(paths.lancedb_dir)
    if not _lancedb_has_table(db, LANCEDB_CHUNKS_TABLE):
        raise RuntimeError("LanceDB chunks table is missing; run `yutome corpus rebuild vectors`")
    table = db.open_table(LANCEDB_CHUNKS_TABLE)
    missing = _lancedb_missing_chunk_columns(table)
    if missing:
        missing_text = ", ".join(sorted(missing))
        raise RuntimeError(f"LanceDB chunks table is stale; missing {missing_text}. Run `yutome corpus rebuild vectors`")

    vector = _embed_voyage_query(
        query=request.search.text,
        model=config.embeddings.model,
        dimension=config.embeddings.dimension,
    )

    video_ids: list[str] | None = None
    if two_stage:
        video_ids = _video_ids_for_sqlite_stage(connection, request.filter)
        if not video_ids:
            return [], []
        if len(video_ids) > 2000 and request.search.mode == "hybrid":
            raise ValueError("filter matches too many videos for hybrid; narrow the filter or use semantic")

    fetch_count = _lance_fetch_count(request)
    notes: list[str] = []
    if video_ids and len(video_ids) > 2000:
        lance_rows = []
        for batch in _batches(video_ids, 2000):
            where = _lance_where(request.filter, video_ids=batch)
            lance_rows.extend(_lance_search(table, vector, request.search.text, request.search.mode, where, fetch_count))
    else:
        where = _lance_where(request.filter, video_ids=video_ids)
        lance_rows = _lance_search(table, vector, request.search.text, request.search.mode, where, fetch_count)

    rows, stale_dropped = _validate_and_enrich_lance_rows(connection, lance_rows, mode=request.search.mode)
    if stale_dropped:
        notes.append("stale_lancedb_rows_dropped")
    rows = _dedupe_by_chunk_id(rows)
    rows.sort(key=lambda row: row.get("score") if row.get("score") is not None else float("-inf"), reverse=True)

    if request.group_by == "video":
        return _group_video_rows(rows, request=request), notes

    rows = rows[request.offset : request.offset + request.limit]
    return _project_chunk_rows(rows, request=request), notes


def _lance_fetch_count(request: QueryRequest) -> int:
    if request.group_by == "video":
        return min(request.limit * request.per_group_limit * 3, 500)
    return min(max(request.limit + request.offset, request.limit) * 3, 500)


def _lance_search(table: Any, vector: list[float], text: str, mode: SearchMode, where: str, limit: int) -> list[dict[str, Any]]:
    try:
        if mode == "hybrid":
            return (
                table.search(query_type="hybrid")
                .vector(vector)
                .text(text)
                .where(where, prefilter=True)
                .rerank()
                .limit(limit)
                .to_list()
            )
        return table.search(vector).where(where, prefilter=True).limit(limit).to_list()
    except Exception as exc:  # noqa: BLE001 - LanceDB exposes mixed exception types.
        if mode == "hybrid":
            raise RuntimeError(f"LanceDB hybrid search is not ready; run `yutome corpus rebuild vectors`: {exc}") from exc
        raise


def _validate_and_enrich_lance_rows(
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    mode: SearchMode,
) -> tuple[list[dict[str, Any]], int]:
    if not rows:
        return [], 0
    chunk_ids = [row["chunk_id"] for row in rows if row.get("chunk_id")]
    placeholders = ",".join("?" for _ in chunk_ids)
    active_rows = connection.execute(
        f"""
        SELECT
            c.chunk_id,
            c.transcript_version_id,
            c.video_id,
            c.channel_id,
            c.sequence,
            c.start_ms,
            c.end_ms,
            c.text,
            c.token_count,
            c.text_hash,
            c.chunker_version,
            v.title,
            v.description,
            v.duration_seconds,
            v.published_at,
            v.live_status,
            v.thumbnail_url,
            v.metadata_hash,
            v.ingest_status,
            tv.source AS transcript_source,
            tv.language,
            tv.is_generated,
            NULL AS snippet,
            NULL AS lexical_score
        FROM chunks c
        JOIN transcript_versions tv
          ON tv.transcript_version_id = c.transcript_version_id
         AND tv.active = 1
        LEFT JOIN videos v ON v.video_id = c.video_id
        WHERE c.chunk_id IN ({placeholders})
        """,
        chunk_ids,
    ).fetchall()
    by_chunk = {row["chunk_id"]: dict(row) for row in active_rows}
    enriched: list[dict[str, Any]] = []
    stale = 0
    for lance_row in rows:
        base = by_chunk.get(lance_row.get("chunk_id"))
        if base is None:
            stale += 1
            continue
        base["match_type"] = mode
        base["vector_score"] = lance_row.get("_distance")
        base["hybrid_score"] = lance_row.get("_relevance_score", lance_row.get("_score"))
        if mode == "hybrid" and base["hybrid_score"] is not None:
            base["score"] = float(base["hybrid_score"])
        elif base["vector_score"] is not None:
            base["score"] = -float(base["vector_score"])
        base["snippet"] = _snippet(base.get("text", ""))
        enriched.append(base)
    return enriched, stale


def _dedupe_by_chunk_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in rows:
        chunk_id = row["chunk_id"]
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        deduped.append(row)
    return deduped


def _group_video_rows(rows: list[dict[str, Any]], *, request: QueryRequest) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["video_id"], []).append(row)
    ranked = sorted(
        groups.items(),
        key=lambda item: max(row.get("score") or float("-inf") for row in item[1]),
        reverse=True,
    )
    result = []
    for video_id, hits in ranked[request.offset : request.offset + request.limit]:
        hits = sorted(hits, key=lambda row: row.get("score") or float("-inf"), reverse=True)[: request.per_group_limit]
        top = hits[0]
        result.append(
            {
                "video_id": video_id,
                "title": top.get("title"),
                "channel_id": top.get("channel_id"),
                "published_at": top.get("published_at"),
                "duration_seconds": top.get("duration_seconds"),
                "score": top.get("score"),
                "hits": _project_chunk_rows(hits, request=request.model_copy(update={"project": "thin"})),
            }
        )
    return result


def _video_ids_for_sqlite_stage(connection: sqlite3.Connection, filters: Filter) -> list[str]:
    clauses, params = _video_filter_sql(filters, for_lance_stage=True)
    sql = f"""
        SELECT DISTINCT v.video_id
        {_VIDEO_FROM_SQL}
        WHERE 1 = 1
        {' AND ' + ' AND '.join(clauses) if clauses else ''}
        ORDER BY v.video_id
    """
    return [row["video_id"] for row in connection.execute(sql, params)]


def _chunk_filter_sql(filters: Filter) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    active = filters.transcript_active.eq if filters.transcript_active and filters.transcript_active.eq is not None else True
    clauses.append("tv.active = ?")
    params.append(1 if active else 0)
    _add_string_clause(clauses, params, "c.video_id", filters.video_id)
    _add_string_clause(clauses, params, "COALESCE(c.channel_id, v.channel_id)", filters.channel_id)
    _add_handle_clause(clauses, params, filters.channel_handle)
    _add_date_clause(clauses, params, "v.published_at", filters.published_at)
    _add_int_clause(clauses, params, "v.duration_seconds", filters.duration_seconds)
    _add_string_clause(clauses, params, "v.ingest_status", filters.ingest_status)
    _add_string_clause(clauses, params, "v.live_status", filters.live_status)
    _add_string_clause(clauses, params, "tv.source", filters.transcript_source)
    _add_string_clause(clauses, params, "tv.language", filters.language)
    _add_bool_clause(clauses, params, "tv.is_generated", filters.is_generated)
    _add_string_clause(clauses, params, "c.chunk_id", filters.chunk_id)
    _add_int_clause(clauses, params, "c.sequence", filters.sequence)
    _add_int_clause(clauses, params, "c.start_ms", filters.start_ms)
    _add_int_clause(clauses, params, "c.token_count", filters.token_count)
    _add_bool_clause(clauses, params, "lc.selected", filters.channel_selected)
    _add_date_clause(clauses, params, "ch.last_synced_at", filters.channel_last_synced_at)
    return clauses, params


def _video_filter_sql(filters: Filter, *, for_lance_stage: bool = False) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    _add_string_clause(clauses, params, "v.video_id", filters.video_id)
    _add_string_clause(clauses, params, "v.channel_id", filters.channel_id)
    _add_handle_clause(clauses, params, filters.channel_handle)
    _add_date_clause(clauses, params, "v.published_at", filters.published_at)
    _add_int_clause(clauses, params, "v.duration_seconds", filters.duration_seconds)
    _add_string_clause(clauses, params, "v.ingest_status", filters.ingest_status)
    _add_string_clause(clauses, params, "v.live_status", filters.live_status)
    _add_string_clause(clauses, params, "atv.source", filters.transcript_source)
    _add_string_clause(clauses, params, "atv.language", filters.language)
    _add_bool_clause(clauses, params, "atv.is_generated", filters.is_generated)
    _add_string_clause(clauses, params, "la.status", filters.last_attempt_status)
    _add_string_clause(clauses, params, "la.tool", filters.last_attempt_tool)
    _add_string_clause(clauses, params, "la.error_class", filters.last_attempt_error_class)
    _add_bool_clause(clauses, params, "la.retryable", filters.last_attempt_retryable)
    _add_date_clause(clauses, params, "la.created_at", filters.last_attempt_created_at)
    _add_bool_clause(clauses, params, "lc.selected", filters.channel_selected)
    _add_date_clause(clauses, params, "ch.last_synced_at", filters.channel_last_synced_at)
    if not for_lance_stage and filters.transcript_active and filters.transcript_active.eq is not None:
        clauses.append("COALESCE(atv.active, 0) = ?")
        params.append(1 if filters.transcript_active.eq else 0)
    return clauses, params


def _channel_filter_sql(filters: Filter) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    _add_string_clause(clauses, params, "COALESCE(ch.channel_id, lc.channel_id)", filters.channel_id)
    _add_handle_clause(clauses, params, filters.channel_handle)
    _add_bool_clause(clauses, params, "lc.selected", filters.channel_selected)
    _add_date_clause(clauses, params, "ch.last_synced_at", filters.channel_last_synced_at)
    return clauses, params


def _add_string_clause(clauses: list[str], params: list[Any], column: str, predicate: StringPredicate | None) -> None:
    if predicate is None:
        return
    if predicate.eq is not None:
        clauses.append(f"{column} = ?")
        params.append(predicate.eq)
    if predicate.in_:
        clauses.append(f"{column} IN ({','.join('?' for _ in predicate.in_)})")
        params.extend(predicate.in_)
    if predicate.not_in:
        clauses.append(f"{column} NOT IN ({','.join('?' for _ in predicate.not_in)})")
        params.extend(predicate.not_in)
    if predicate.starts_with is not None:
        clauses.append(f"{column} LIKE ?")
        params.append(f"{predicate.starts_with}%")
    if predicate.starts_with_any:
        clauses.append("(" + " OR ".join(f"{column} LIKE ?" for _ in predicate.starts_with_any) + ")")
        params.extend(f"{value}%" for value in predicate.starts_with_any)


def _add_handle_clause(clauses: list[str], params: list[Any], predicate: StringPredicate | None) -> None:
    if predicate is None:
        return
    normalized = _normalize_handle_values(predicate)
    column = "LOWER(LTRIM(COALESCE(ch.handle, lc.handle), '@'))"
    if normalized.eq is not None:
        clauses.append(f"{column} = ?")
        params.append(normalized.eq)
    if normalized.in_:
        clauses.append(f"{column} IN ({','.join('?' for _ in normalized.in_)})")
        params.extend(normalized.in_)
    if normalized.not_in:
        clauses.append(f"{column} NOT IN ({','.join('?' for _ in normalized.not_in)})")
        params.extend(normalized.not_in)
    if normalized.starts_with is not None:
        clauses.append(f"{column} LIKE ?")
        params.append(f"{normalized.starts_with}%")


def _normalize_handle_values(predicate: StringPredicate) -> StringPredicate:
    def norm(value: str) -> str:
        return value.strip().lower().lstrip("@")

    return StringPredicate(
        eq=norm(predicate.eq) if predicate.eq is not None else None,
        in_=[norm(value) for value in predicate.in_] if predicate.in_ else None,
        not_in=[norm(value) for value in predicate.not_in] if predicate.not_in else None,
        starts_with=norm(predicate.starts_with) if predicate.starts_with is not None else None,
        starts_with_any=[norm(value) for value in predicate.starts_with_any] if predicate.starts_with_any else None,
    )


def _add_int_clause(clauses: list[str], params: list[Any], column: str, predicate: IntPredicate | None) -> None:
    if predicate is None:
        return
    if predicate.eq is not None:
        clauses.append(f"{column} = ?")
        params.append(predicate.eq)
    if predicate.gte is not None:
        clauses.append(f"{column} >= ?")
        params.append(predicate.gte)
    if predicate.lte is not None:
        clauses.append(f"{column} <= ?")
        params.append(predicate.lte)
    if predicate.in_:
        clauses.append(f"{column} IN ({','.join('?' for _ in predicate.in_)})")
        params.extend(predicate.in_)


def _add_date_clause(clauses: list[str], params: list[Any], column: str, predicate: DateRange | None) -> None:
    if predicate is None:
        return
    if predicate.gte is not None:
        clauses.append(f"{column} >= ?")
        params.append(predicate.gte)
    if predicate.lte is not None:
        clauses.append(f"{column} <= ?")
        params.append(predicate.lte)


def _add_bool_clause(clauses: list[str], params: list[Any], column: str, predicate: BoolPredicate | None) -> None:
    if predicate is None or predicate.eq is None:
        return
    clauses.append(f"{column} = ?")
    params.append(1 if predicate.eq else 0)


def _order_sql(request: QueryRequest, *, entity: EntityName, lexical: bool) -> str:
    if not request.order_by:
        if lexical:
            return "ORDER BY lexical_score ASC"
        if entity == "chunk":
            return "ORDER BY v.published_at DESC, c.video_id, c.sequence"
        if entity == "video":
            return "ORDER BY v.published_at DESC, v.video_id"
        return ""
    pieces = []
    for order in request.order_by:
        direction = "ASC" if order.direction == "asc" else "DESC"
        column = {
            "score": "score",
            "published_at": "v.published_at",
            "duration_seconds": "v.duration_seconds",
            "title": "v.title",
            "ingest_status": "v.ingest_status",
            "sequence": "c.sequence",
            "start_ms": "c.start_ms",
            "last_attempt_created_at": "la.created_at",
        }[order.field]
        if order.field == "score" and lexical:
            column = "-lexical_score"
        pieces.append(f"{column} {direction}")
    return "ORDER BY " + ", ".join(pieces)


def _row_dict(row: sqlite3.Row, *, match_type: str) -> dict[str, Any]:
    data = dict(row)
    data["match_type"] = match_type
    if data.get("lexical_score") is not None:
        data["score"] = -float(data["lexical_score"])
    return data


def _project_chunk_rows(rows: list[dict[str, Any]], *, request: QueryRequest) -> list[dict[str, Any]]:
    detail: Literal["thin", "chunk", "metadata"] = "thin"
    if request.project == "chunk":
        detail = "chunk"
    elif request.project == "metadata":
        detail = "metadata"
    projected = []
    for row in rows:
        hit = _format_hit(row, detail=detail, include_description=request.project == "metadata")
        if row.get("score") is not None:
            hit["score"] = row["score"]
        if request.project == "metadata":
            hit["thumbnail_url"] = row.get("thumbnail_url")
            hit["live_status"] = row.get("live_status")
            hit["metadata_hash"] = row.get("metadata_hash")
            hit["ingest_status"] = row.get("ingest_status")
        projected.append(hit)
    return projected


def _project_video_row(row: dict[str, Any], *, project: ProjectionName) -> dict[str, Any]:
    card = {
        "video_id": row["video_id"],
        "resource_uri": f"yutome://video/{row['video_id']}",
        "title": row.get("title"),
        "description": row.get("description") if project == "metadata" else None,
        "youtube_url": f"https://youtube.com/watch?v={row['video_id']}",
        "channel_id": row.get("channel_id"),
        "channel_handle": row.get("channel_handle"),
        "channel_title": row.get("channel_title"),
        "published_at": row.get("published_at"),
        "duration_seconds": row.get("duration_seconds"),
        "live_status": row.get("live_status"),
        "thumbnail_url": row.get("thumbnail_url"),
        "ingest_status": row.get("ingest_status"),
        "active_transcript_source": row.get("active_transcript_source"),
        "active_transcript": (
            None
            if row.get("active_transcript_id") is None
            else {
                "transcript_version_id": row.get("active_transcript_id"),
                "source": row.get("active_transcript_source"),
                "language": row.get("active_transcript_language"),
                "is_generated": bool(row.get("active_transcript_is_generated")),
                "segment_count": row.get("active_transcript_segment_count"),
                "resource_uri": f"yutome://transcript/{row['active_transcript_id']}",
            }
        ),
    }
    if row.get("score") is not None:
        card["score"] = row["score"]
        card["scores"] = {"lexical_score": row.get("lexical_score")}
    if project == "video_attention":
        card["last_attempt"] = (
            None
            if row.get("last_attempt_id") is None
            else {
                "attempt_id": row.get("last_attempt_id"),
                "tool": row.get("last_attempt_tool"),
                "status": row.get("last_attempt_status"),
                "error_class": row.get("last_attempt_error_class"),
                "error": row.get("last_attempt_error"),
                "retryable": bool(row.get("last_attempt_retryable")),
                "created_at": row.get("last_attempt_created_at"),
            }
        )
    return {key: value for key, value in card.items() if value is not None}


def _project_channel_row(row: dict[str, Any]) -> dict[str, Any]:
    channel_id = row.get("channel_id")
    return {
        "channel_id": channel_id,
        "resource_uri": f"yutome://channel/{channel_id}" if channel_id else None,
        "handle": row.get("handle"),
        "title": row.get("title"),
        "source_url": row.get("source_url") or row.get("library_source_url"),
        "uploads_url": row.get("uploads_url"),
        "first_synced_at": row.get("first_synced_at"),
        "last_synced_at": row.get("last_synced_at"),
        "library_channel_id": row.get("library_channel_id"),
        "selected": bool(row.get("selected")),
        "video_count": row.get("video_count"),
        "indexed_count": row.get("indexed_count"),
    }


def _status_breakdown(connection: sqlite3.Connection) -> dict[str, Any]:
    counts = {
        name: connection.execute(f"SELECT COUNT(*) AS count FROM {name}").fetchone()["count"]
        for name in ("channels", "videos", "transcript_versions", "chunks", "embeddings", "transcript_attempts", "jobs")
    }
    status_rows = connection.execute(
        """
        SELECT ingest_status, COUNT(*) AS count
        FROM videos
        GROUP BY ingest_status
        ORDER BY count DESC, ingest_status
        """
    ).fetchall()
    statuses = {row["ingest_status"]: int(row["count"]) for row in status_rows}
    indexed = statuses.get("indexed", 0)
    needs_attention = sum(
        count for status, count in statuses.items() if status.startswith("failed:") or status.startswith("deferred:")
    )
    total_videos = counts.get("videos", 0)
    jobs_by_status = {
        row["status"]: row["count"]
        for row in connection.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status")
    }
    jobs_by_kind = {
        row["job_kind"]: row["count"]
        for row in connection.execute("SELECT job_kind, COUNT(*) AS count FROM jobs GROUP BY job_kind")
    }
    return {
        "searchable_now": indexed,
        "still_indexing": max(0, total_videos - indexed - needs_attention),
        "needs_attention": needs_attention,
        "channels": counts.get("channels", 0),
        "videos": total_videos,
        "transcript_versions": counts.get("transcript_versions", 0),
        "chunks": counts.get("chunks", 0),
        "embeddings": counts.get("embeddings", 0),
        "indexed_percent": round((indexed / total_videos) * 100, 1) if total_videos else 0.0,
        "statuses": statuses,
        "jobs": {"total": counts.get("jobs", 0), "by_status": jobs_by_status, "by_kind": jobs_by_kind},
    }


def _lance_where(filters: Filter, *, video_ids: list[str] | None) -> str:
    clauses = ["active = true"]
    if video_ids is not None:
        clauses.append(f"video_id IN ({_lance_str_list(video_ids)})")
    _add_lance_string_clause(clauses, "video_id", filters.video_id)
    _add_lance_string_clause(clauses, "channel_id", filters.channel_id)
    _add_lance_string_clause(clauses, "chunk_id", filters.chunk_id)
    _add_lance_string_clause(clauses, "source", filters.transcript_source)
    _add_lance_string_clause(clauses, "language", filters.language)
    _add_lance_bool_clause(clauses, "is_generated", filters.is_generated)
    _add_lance_int_clause(clauses, "sequence", filters.sequence)
    _add_lance_int_clause(clauses, "start_ms", filters.start_ms)
    _add_lance_int_clause(clauses, "token_count", filters.token_count)
    return " AND ".join(clauses)


def _add_lance_string_clause(clauses: list[str], column: str, predicate: StringPredicate | None) -> None:
    if predicate is None:
        return
    if predicate.eq is not None:
        clauses.append(f"{column} = {_lance_quote(predicate.eq)}")
    if predicate.in_:
        clauses.append(f"{column} IN ({_lance_str_list(predicate.in_)})")
    if predicate.not_in:
        clauses.append(f"{column} NOT IN ({_lance_str_list(predicate.not_in)})")
    if predicate.starts_with is not None:
        clauses.append(f"{column} LIKE {_lance_quote(predicate.starts_with + '%')}")
    if predicate.starts_with_any:
        pieces = [f"{column} LIKE {_lance_quote(value + '%')}" for value in predicate.starts_with_any]
        clauses.append("(" + " OR ".join(pieces) + ")")


def _add_lance_int_clause(clauses: list[str], column: str, predicate: IntPredicate | None) -> None:
    if predicate is None:
        return
    if predicate.eq is not None:
        clauses.append(f"{column} = {int(predicate.eq)}")
    if predicate.gte is not None:
        clauses.append(f"{column} >= {int(predicate.gte)}")
    if predicate.lte is not None:
        clauses.append(f"{column} <= {int(predicate.lte)}")
    if predicate.in_:
        clauses.append(f"{column} IN ({','.join(str(int(value)) for value in predicate.in_)})")


def _add_lance_bool_clause(clauses: list[str], column: str, predicate: BoolPredicate | None) -> None:
    if predicate is None or predicate.eq is None:
        return
    clauses.append(f"{column} = {'true' if predicate.eq else 'false'}")


def _lance_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _lance_str_list(values: list[str]) -> str:
    return ",".join(_lance_quote(value) for value in values)


def _batches(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


_VIDEO_SELECT_SQL = """
    v.video_id,
    v.channel_id,
    v.title,
    v.description,
    v.duration_seconds,
    v.published_at,
    v.live_status,
    v.thumbnail_url,
    v.metadata_hash,
    v.ingest_status,
    ch.title AS channel_title,
    ch.handle AS channel_handle,
    atv.transcript_version_id AS active_transcript_id,
    atv.source AS active_transcript_source,
    atv.language AS active_transcript_language,
    atv.is_generated AS active_transcript_is_generated,
    atv.segment_count AS active_transcript_segment_count,
    la.attempt_id AS last_attempt_id,
    la.tool AS last_attempt_tool,
    la.status AS last_attempt_status,
    la.error_class AS last_attempt_error_class,
    la.error AS last_attempt_error,
    la.retryable AS last_attempt_retryable,
    la.created_at AS last_attempt_created_at
"""

_VIDEO_FROM_SQL = """
    FROM videos v
    LEFT JOIN channels ch ON ch.channel_id = v.channel_id
    LEFT JOIN library_channels lc ON lc.channel_id = ch.channel_id
    LEFT JOIN transcript_versions atv ON atv.video_id = v.video_id AND atv.active = 1
    LEFT JOIN transcript_attempts la
      ON la.attempt_id = (
        SELECT MAX(attempt_id)
        FROM transcript_attempts
        WHERE video_id = v.video_id
      )
"""
