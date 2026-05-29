from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from yutome.config import AppConfig
from yutome.embeddings import _embed_voyage_query
from yutome.hosted.resources import HostedResourceNotFound
from yutome.hosted.runtime import connect_postgres
from yutome.hosted.search_store import PostgresVectorChordSearchStore, SearchFilters
from yutome.paths import ProjectPaths
from yutome.query import (
    BoolPredicate,
    Filter,
    OrderBy,
    QueryRequest,
    QueryResult,
    StringPredicate,
)


@dataclass(frozen=True)
class ContextRequest:
    chunk_id: str | None = None
    video_id: str | None = None
    time_seconds: int | None = None
    youtube_url: str | None = None


_ORDER_BY_ALIASES: dict[str, list[OrderBy]] = {
    "newest": [OrderBy(field="published_at", direction="desc")],
    "oldest": [OrderBy(field="published_at", direction="asc")],
    "longest": [OrderBy(field="duration_seconds", direction="desc")],
    "shortest": [OrderBy(field="duration_seconds", direction="asc")],
    "title": [OrderBy(field="title", direction="asc")],
    "relevance": [OrderBy(field="score", direction="desc")],
}


def _order(value: str | None) -> list[OrderBy]:
    if not value:
        return []
    normalized = value.strip().lower()
    if normalized in _ORDER_BY_ALIASES:
        return _ORDER_BY_ALIASES[normalized]
    field, _, direction = normalized.partition(":")
    return [OrderBy(field=field, direction=direction or "desc")]  # type: ignore[arg-type]


def q(*, config: AppConfig, paths: ProjectPaths, request: QueryRequest | dict[str, Any]) -> QueryResult:
    del paths
    query_request = request if isinstance(request, QueryRequest) else QueryRequest.model_validate(request)
    connection = _connect(config)
    store = PostgresVectorChordSearchStore(connection)
    workspace_id = _workspace_id(config)
    if query_request.project == "status_breakdown":
        return QueryResult(rows=[store.list_status(workspace_id=workspace_id)])
    if query_request.entity == "video":
        return QueryResult(
            rows=store.list_videos(
                workspace_id=workspace_id,
                limit=query_request.limit,
                offset=query_request.offset,
                channel=_string_eq(query_request.filter.channel_id) or _string_eq(query_request.filter.channel_handle),
                video_id=_string_eq(query_request.filter.video_id),
                since=query_request.filter.published_at.gte if query_request.filter.published_at else None,
                until=query_request.filter.published_at.lte if query_request.filter.published_at else None,
                status=_string_eq(query_request.filter.ingest_status),
                source=_string_eq(query_request.filter.transcript_source),
                language=_string_eq(query_request.filter.language),
                order_by=_order_alias(query_request.order_by),
            )
        )
    if query_request.entity == "channel":
        return QueryResult(
            rows=store.list_channels(
                workspace_id=workspace_id,
                limit=query_request.limit,
                offset=query_request.offset,
                channel=_string_eq(query_request.filter.channel_id) or _string_eq(query_request.filter.channel_handle),
                since=query_request.filter.published_at.gte if query_request.filter.published_at else None,
                until=query_request.filter.published_at.lte if query_request.filter.published_at else None,
                status=_string_eq(query_request.filter.ingest_status),
                source=_string_eq(query_request.filter.transcript_source),
                language=_string_eq(query_request.filter.language),
                selected=_bool_eq(query_request.filter.channel_selected),
            )
        )
    search = query_request.search
    if search is None or search.mode == "none":
        raise ValueError("chunk q requires lexical, semantic, or hybrid search in the Postgres search store")
    if search.over != "chunk_text":
        raise ValueError("Postgres search store chunk queries search chunk_text only")
    return _find_chunks(
        config=config,
        store=store,
        workspace_id=workspace_id,
        text=search.text,
        mode=search.mode,
        limit=query_request.limit,
        project=query_request.project,
        offset=query_request.offset,
        filters=_filters_from_query_filter(query_request.filter),
        group_by=query_request.group_by,
        per_group_limit=query_request.per_group_limit,
    )


def find(
    *,
    config: AppConfig,
    paths: ProjectPaths,
    text: str,
    mode: Literal["lexical", "semantic", "hybrid", "none"] | None = None,
    channel: str | None = None,
    since: str | None = None,
    until: str | None = None,
    source: str | None = None,
    language: str | None = None,
    group_by: Literal["video", "channel", "transcript_source"] | None = None,
    limit: int = 10,
    offset: int = 0,
    project: str | None = None,
) -> QueryResult:
    del paths
    effective_mode = mode or config.find.default_mode
    if effective_mode == "none":
        raise ValueError("find requires lexical, semantic, or hybrid mode")
    connection = _connect(config)
    store = PostgresVectorChordSearchStore(connection)
    return _find_chunks(
        config=config,
        store=store,
        workspace_id=_workspace_id(config),
        text=text,
        mode=effective_mode,
        limit=limit,
        offset=offset,
        project=project or "thin",
        filters=SearchFilters(channel=channel, since=since, until=until, source=source, language=language),
        group_by=group_by,
        per_group_limit=3,
    )


def list_(
    *,
    config: AppConfig,
    paths: ProjectPaths,
    entity: Literal["video", "videos", "channel", "channels", "status"],
    channel: str | None = None,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    source: str | None = None,
    language: str | None = None,
    selected: bool | None = None,
    order_by: str | None = None,
    limit: int = 20,
    offset: int = 0,
    project: str | None = None,
) -> QueryResult:
    del paths, project
    store = PostgresVectorChordSearchStore(_connect(config))
    workspace_id = _workspace_id(config)
    if entity == "status":
        return QueryResult(rows=[store.list_status(workspace_id=workspace_id)])
    if entity in {"video", "videos"}:
        return QueryResult(
            rows=store.list_videos(
                workspace_id=workspace_id,
                limit=limit,
                offset=offset,
                channel=channel,
                since=since,
                until=until,
                status=status,
                source=source,
                language=language,
                order_by=order_by,
            )
        )
    if entity in {"channel", "channels"}:
        return QueryResult(
            rows=store.list_channels(
                workspace_id=workspace_id,
                limit=limit,
                offset=offset,
                channel=channel,
                since=since,
                until=until,
                status=status,
                source=source,
                language=language,
                selected=selected,
            )
        )
    raise ValueError("entity must be one of: videos, channels, status")


def show(
    *,
    config: AppConfig,
    paths: ProjectPaths,
    kind: Literal["chunk", "video", "channel", "transcript", "context", "source"],
    id_: str | None = None,
    token_budget: int = 3000,
    video_id: str | None = None,
    time_seconds: int | None = None,
    youtube_url: str | None = None,
    transcript_offset: int = 0,
    transcript_limit: int | None = None,
) -> dict[str, Any]:
    if kind == "context":
        return context_expand(
            config=config,
            paths=paths,
            request=_context_request(id_=id_, video_id=video_id, time_seconds=time_seconds, youtube_url=youtube_url),
            token_budget=token_budget,
        )
    connection = _connect(config)
    store = PostgresVectorChordSearchStore(connection)
    workspace_id = _workspace_id(config)
    try:
        if kind == "chunk":
            return store.resource_chunk(workspace_id=workspace_id, chunk_id=_required_id(id_, "chunk id"))
        if kind == "video":
            return store.resource_video(workspace_id=workspace_id, video_id=_required_id(id_, "video id"))
        if kind == "channel":
            return store.resource_channel(workspace_id=workspace_id, channel_id=_required_id(id_, "channel id or handle"))
        if kind == "transcript":
            return store.resource_transcript(
                workspace_id=workspace_id,
                transcript_version_id=_required_id(id_, "transcript id"),
                offset=transcript_offset,
                limit=transcript_limit,
            )
        if kind == "source":
            return store.resource_source(workspace_id=workspace_id, source_id=_required_id(id_, "source id"))
    except HostedResourceNotFound as exc:
        raise ValueError(f"{exc.kind} not found: {exc.id}") from exc
    raise ValueError(f"unsupported show kind: {kind}")


def context_expand(
    *,
    config: AppConfig,
    paths: ProjectPaths,
    request: ContextRequest,
    token_budget: int = 3000,
) -> dict[str, Any]:
    del paths
    connection = _connect(config)
    workspace_id = _workspace_id(config)
    anchor = _resolve_anchor(connection=connection, workspace_id=workspace_id, request=request)
    chunks = _neighbor_chunks(connection=connection, workspace_id=workspace_id, anchor=anchor, token_budget=token_budget)
    text = _merge_chunk_text([str(chunk["text"] or "") for chunk in chunks])
    estimated_tokens = sum(_chunk_token_count(chunk) for chunk in chunks)
    return {
        "anchor": _format_chunk_row(anchor, detail="chunk"),
        "token_budget": token_budget,
        "estimated_tokens": estimated_tokens,
        "text": text,
        "chunks": [_format_chunk_row(chunk, detail="chunk") for chunk in chunks],
        "citations": [
            {
                "chunk_id": chunk["chunk_id"],
                "video_id": chunk["video_id"],
                "youtube_video_id": chunk.get("youtube_video_id"),
                "title": chunk.get("title"),
                "youtube_url": _youtube_url(str(chunk.get("youtube_video_id") or chunk["video_id"]), _seconds_to_ms(chunk.get("start_seconds"))),
                "start_ms": _seconds_to_ms(chunk.get("start_seconds")),
                "end_ms": _seconds_to_ms(chunk.get("end_seconds")),
                "transcript_version_id": chunk["transcript_version_id"],
                "transcript_source": chunk.get("transcript_source"),
            }
            for chunk in chunks
        ],
    }


def source(*, config: AppConfig, paths: ProjectPaths, request: ContextRequest) -> dict[str, Any]:
    del paths
    connection = _connect(config)
    anchor = _resolve_anchor(connection=connection, workspace_id=_workspace_id(config), request=request)
    start_ms = _seconds_to_ms(anchor.get("start_seconds"))
    return {
        "chunk_id": anchor["chunk_id"],
        "video_id": anchor["video_id"],
        "youtube_video_id": anchor.get("youtube_video_id"),
        "title": anchor.get("title"),
        "youtube_url": _youtube_url(str(anchor.get("youtube_video_id") or anchor["video_id"]), start_ms),
        "start_ms": start_ms,
        "end_ms": _seconds_to_ms(anchor.get("end_seconds")),
        "transcript_version_id": anchor.get("transcript_version_id"),
        "transcript_source": anchor.get("transcript_source"),
        "language": anchor.get("language"),
        "is_generated": _metadata_bool(anchor.get("transcript_metadata"), "is_generated"),
    }


def resource_chunk(*, config: AppConfig, paths: ProjectPaths, chunk_id: str) -> dict[str, Any]:
    del paths
    return PostgresVectorChordSearchStore(_connect(config)).resource_chunk(workspace_id=_workspace_id(config), chunk_id=chunk_id)


def resource_video(*, config: AppConfig, paths: ProjectPaths, video_id: str) -> dict[str, Any]:
    del paths
    return PostgresVectorChordSearchStore(_connect(config)).resource_video(workspace_id=_workspace_id(config), video_id=video_id)


def resource_channel(*, config: AppConfig, paths: ProjectPaths, selector: str) -> dict[str, Any]:
    del paths
    return PostgresVectorChordSearchStore(_connect(config)).resource_channel(workspace_id=_workspace_id(config), channel_id=selector)


def resource_transcript(
    *,
    config: AppConfig,
    paths: ProjectPaths,
    transcript_version_id: str,
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    del paths
    return PostgresVectorChordSearchStore(_connect(config)).resource_transcript(
        workspace_id=_workspace_id(config),
        transcript_version_id=transcript_version_id,
        offset=offset,
        limit=limit,
    )


def _connect(config: AppConfig) -> Any:
    return connect_postgres(url_env=config.database.postgres_url_env)


def _workspace_id(config: AppConfig) -> str:
    workspace_id = (config.hosted.workspace_id or config.hosted.local_workspace_id).strip()
    if not workspace_id:
        raise ValueError("No workspace configured. Run: yutome setup.")
    return workspace_id


def _find_chunks(
    *,
    config: AppConfig,
    store: PostgresVectorChordSearchStore,
    workspace_id: str,
    text: str,
    mode: str,
    limit: int,
    project: str | None = None,
    offset: int = 0,
    filters: SearchFilters | None = None,
    group_by: str | None = None,
    per_group_limit: int = 3,
) -> QueryResult:
    search_limit = min(200, limit * max(1, per_group_limit) * 8) if group_by else limit
    search_offset = 0 if group_by else offset
    if mode == "lexical":
        rows, usage = store.lexical_search(
            workspace_id=workspace_id,
            query=text,
            limit=search_limit,
            offset=search_offset,
            filters=filters,
        )
    elif mode == "semantic":
        vector = _embed_voyage_query(query=text, model=config.embeddings.model, dimension=config.embeddings.dimension)
        rows, usage = store.semantic_search(
            workspace_id=workspace_id,
            query_vector=vector,
            limit=search_limit,
            offset=search_offset,
            filters=filters,
        )
    elif mode == "hybrid":
        vector = _embed_voyage_query(query=text, model=config.embeddings.model, dimension=config.embeddings.dimension)
        rows, usage = store.hybrid_search(
            workspace_id=workspace_id,
            query=text,
            query_vector=vector,
            limit=search_limit,
            offset=search_offset,
            filters=filters,
        )
    else:
        raise ValueError(f"unsupported search mode: {mode}")
    detail = "thin" if project in {None, "thin"} else project
    if group_by:
        hits = [_format_chunk_row(row, detail=detail) for row in rows]
        grouped_rows = _group_chunk_hits(hits, group_by=group_by, limit=limit, offset=offset, per_group_limit=per_group_limit)
        return QueryResult(
            rows=grouped_rows,
            notes=[f"search_store_backend={usage.backend}", f"group_by={group_by}"],
            total=len(grouped_rows),
        )
    return QueryResult(
        rows=[_format_chunk_row(row, detail=detail) for row in rows],
        notes=[f"search_store_backend={usage.backend}"],
        total=len(rows),
    )


def _group_chunk_hits(
    hits: list[dict[str, Any]],
    *,
    group_by: str,
    limit: int,
    offset: int,
    per_group_limit: int,
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for hit in hits:
        group_value = _chunk_group_value(hit, group_by)
        if group_value is None:
            continue
        key = str(group_value)
        if key not in groups:
            groups[key] = _new_chunk_group(hit, group_by=group_by, group_value=key)
            order.append(key)
        group_hits = groups[key]["hits"]
        if len(group_hits) < per_group_limit:
            group_hits.append(hit)
            groups[key]["hit_count"] += 1
    selected_keys = order[max(0, offset) : max(0, offset) + max(1, limit)]
    return [groups[key] for key in selected_keys]


def _chunk_group_value(hit: dict[str, Any], group_by: str) -> str | None:
    if group_by == "video":
        return str(hit.get("video_id") or hit.get("youtube_video_id") or "") or None
    if group_by == "channel":
        return str(hit.get("channel_id") or hit.get("channel_handle") or "") or None
    if group_by == "transcript_source":
        return str(hit.get("transcript_source") or "") or None
    raise ValueError("group_by must be one of: video, channel, transcript_source")


def _new_chunk_group(hit: dict[str, Any], *, group_by: str, group_value: str) -> dict[str, Any]:
    group: dict[str, Any] = {
        "group_by": group_by,
        "group_value": group_value,
        "hit_count": 0,
        "score": hit.get("score"),
        "hits": [],
    }
    for key in (
        "video_id",
        "youtube_video_id",
        "title",
        "youtube_url",
        "channel_id",
        "channel_title",
        "channel_handle",
        "transcript_source",
        "language",
    ):
        if key in hit:
            group[key] = hit[key]
    return {key: value for key, value in group.items() if value is not None}


def _resolve_anchor(*, connection: Any, workspace_id: str, request: ContextRequest) -> dict[str, Any]:
    chunk_id = request.chunk_id
    video_id = request.video_id
    time_seconds = request.time_seconds
    if request.youtube_url:
        parsed_video_id, parsed_time = parse_youtube_location(request.youtube_url)
        video_id = video_id or parsed_video_id
        time_seconds = time_seconds if time_seconds is not None else parsed_time
    if chunk_id:
        row = connection.execute(_CHUNK_SELECT + " AND c.id = %(chunk_id)s LIMIT 1;", {"workspace_id": workspace_id, "chunk_id": chunk_id}).fetchone()
    elif video_id and time_seconds is not None:
        row = connection.execute(
            _CHUNK_SELECT
            + """
              AND (v.id = %(video_id)s OR v.youtube_video_id = %(video_id)s)
            ORDER BY
              CASE
                WHEN c.start_seconds <= %(time_seconds)s AND c.end_seconds >= %(time_seconds)s THEN 0
                ELSE 1
              END,
              abs(coalesce(c.start_seconds, 0) - %(time_seconds)s),
              c.chunk_index
            LIMIT 1;
            """,
            {"workspace_id": workspace_id, "video_id": video_id, "time_seconds": time_seconds},
        ).fetchone()
    else:
        raise ValueError("Provide a chunk id, a timestamped YouTube URL, or video_id with time_seconds.")
    if row is None:
        raise ValueError("No matching chunk found.")
    return dict(row)


def _neighbor_chunks(*, connection: Any, workspace_id: str, anchor: dict[str, Any], token_budget: int) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in connection.execute(
            _CHUNK_SELECT
            + """
              AND c.transcript_version_id = %(transcript_version_id)s
            ORDER BY c.chunk_index;
            """,
            {"workspace_id": workspace_id, "transcript_version_id": anchor["transcript_version_id"]},
        ).fetchall()
    ]
    anchor_index = next(index for index, row in enumerate(rows) if row["chunk_id"] == anchor["chunk_id"])
    selected = {anchor_index}
    total = _chunk_token_count(rows[anchor_index])
    left = anchor_index - 1
    right = anchor_index + 1
    while left >= 0 or right < len(rows):
        added = False
        for index in (left, right):
            if index < 0 or index >= len(rows) or index in selected:
                continue
            candidate_tokens = _chunk_token_count(rows[index])
            if total + candidate_tokens > token_budget and selected:
                continue
            selected.add(index)
            total += candidate_tokens
            added = True
        left -= 1
        right += 1
        if not added:
            break
    return [rows[index] for index in sorted(selected)]


def parse_youtube_location(url: str) -> tuple[str | None, int | None]:
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    video_id = None
    if parsed.netloc.endswith("youtu.be"):
        video_id = parsed.path.strip("/").split("/")[0] or None
    elif "v" in query:
        video_id = query["v"][0]
    time_value = query.get("t", [None])[0] or query.get("start", [None])[0]
    return video_id, _parse_time_seconds(time_value)


def _format_chunk_row(row: dict[str, Any], *, detail: str | None) -> dict[str, Any]:
    chunk_id = str(row["chunk_id"])
    video_id = str(row["video_id"])
    youtube_video_id = str(row.get("youtube_video_id") or video_id)
    start_ms = _seconds_to_ms(row.get("start_seconds"))
    hit = {
        "chunk_id": chunk_id,
        "resource_uri": f"yutome://chunk/{chunk_id}",
        "video_id": video_id,
        "youtube_video_id": youtube_video_id,
        "title": row.get("title"),
        "channel_id": row.get("channel_id"),
        "channel_title": row.get("channel_title"),
        "channel_handle": row.get("channel_handle"),
        "youtube_url": _youtube_url(youtube_video_id, start_ms),
        "start_ms": start_ms,
        "end_ms": _seconds_to_ms(row.get("end_seconds")),
        "snippet": _snippet(str(row.get("text") or "")),
        "transcript_version_id": row.get("transcript_version_id"),
        "transcript_source": row.get("transcript_source"),
        "language": row.get("language"),
        "is_generated": _metadata_bool(row.get("transcript_metadata"), "is_generated"),
        "token_count": _chunk_token_count(row),
        "match_type": row.get("match_type"),
        "scores": {
            key: row.get(key)
            for key in ("lexical_score", "vector_distance", "score")
            if row.get(key) is not None
        },
    }
    if row.get("score") is not None:
        hit["score"] = row["score"]
    if detail == "chunk":
        hit["text"] = row.get("text", "")
    if detail == "metadata":
        hit.update(
            {
                "published_at": _json_value(row.get("published_at")),
                "duration_seconds": row.get("duration_seconds"),
                "sequence": row.get("chunk_index"),
                "chunker_version": _metadata_value(row.get("chunk_metadata"), "chunker_version")
                or _metadata_value(row.get("chunk_metadata"), "chunking_version"),
                "text_hash": _metadata_value(row.get("chunk_metadata"), "text_hash"),
                "thumbnail_url": row.get("thumbnail_url"),
            }
        )
    return {key: value for key, value in hit.items() if value is not None}


def _chunk_token_count(row: dict[str, Any]) -> int:
    value = _metadata_value(row.get("chunk_metadata"), "token_count")
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _metadata_value(metadata: Any, key: str) -> Any:
    return metadata.get(key) if isinstance(metadata, dict) else None


def _metadata_bool(metadata: Any, key: str) -> bool:
    return bool(_metadata_value(metadata, key))


def _json_value(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _seconds_to_ms(value: Any) -> int:
    return int(float(value or 0) * 1000)


def _snippet(text: str, *, max_chars: int = 360) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def _youtube_url(video_id: str, start_ms: int) -> str:
    return f"https://youtube.com/watch?v={video_id}&t={int(start_ms // 1000)}s"


def _merge_chunk_text(chunks: list[str]) -> str:
    merged_words: list[str] = []
    for text in chunks:
        words = text.split()
        if not merged_words:
            merged_words.extend(words)
            continue
        max_overlap = min(150, len(merged_words), len(words))
        overlap = 0
        for size in range(max_overlap, 0, -1):
            if merged_words[-size:] == words[:size]:
                overlap = size
                break
        merged_words.extend(words[overlap:])
    return " ".join(merged_words).strip()


def _parse_time_seconds(value: str | None) -> int | None:
    if not value:
        return None
    value = value.strip().lower()
    if value.isdigit():
        return int(value)
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?", value)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _context_request(
    *,
    id_: str | None,
    video_id: str | None,
    time_seconds: int | None,
    youtube_url: str | None,
) -> ContextRequest:
    if id_ and (id_.startswith("http://") or id_.startswith("https://")):
        youtube_url = youtube_url or id_
        id_ = None
    return ContextRequest(chunk_id=id_, video_id=video_id, time_seconds=time_seconds, youtube_url=youtube_url)


def _required_id(value: str | None, label: str) -> str:
    if not value:
        raise ValueError(f"{label} is required")
    return value


def _filters_from_query_filter(filter_: Filter) -> SearchFilters:
    return SearchFilters(
        channel=_string_eq(filter_.channel_id) or _string_eq(filter_.channel_handle),
        since=filter_.published_at.gte if filter_.published_at else None,
        until=filter_.published_at.lte if filter_.published_at else None,
        source=_string_eq(filter_.transcript_source),
        language=_string_eq(filter_.language),
    )


def _string_eq(predicate: StringPredicate | None) -> str | None:
    return predicate.eq if predicate and predicate.eq is not None else None


def _bool_eq(predicate: BoolPredicate | None) -> bool | None:
    return predicate.eq if predicate and predicate.eq is not None else None


def _order_alias(order_by: list[OrderBy]) -> str | None:
    if not order_by:
        return None
    first = order_by[0]
    if first.field == "published_at":
        return "oldest" if first.direction == "asc" else "newest"
    if first.field == "duration_seconds":
        return "shortest" if first.direction == "asc" else "longest"
    if first.field == "title":
        return "title"
    return None


_CHUNK_SELECT = """
SELECT
    c.id AS chunk_id,
    c.video_id,
    v.youtube_video_id,
    c.transcript_version_id,
    c.chunk_index,
    c.start_seconds,
    c.end_seconds,
    c.text,
    c.metadata_json AS chunk_metadata,
    v.title,
    v.channel_id,
    v.published_at,
    v.duration_seconds,
    v.metadata_json->>'thumbnail_url' AS thumbnail_url,
    tv.source AS transcript_source,
    tv.language_code AS language,
    tv.metadata_json AS transcript_metadata,
    'context' AS match_type
FROM chunks c
JOIN videos v ON v.id = c.video_id AND v.workspace_id = c.workspace_id
JOIN transcript_versions tv ON tv.id = c.transcript_version_id AND tv.workspace_id = c.workspace_id
WHERE c.workspace_id = %(workspace_id)s
  AND v.active_transcript_version_id = c.transcript_version_id
"""


__all__ = [
    "ContextRequest",
    "context_expand",
    "find",
    "list_",
    "parse_youtube_location",
    "q",
    "resource_channel",
    "resource_chunk",
    "resource_transcript",
    "resource_video",
    "show",
    "source",
]
