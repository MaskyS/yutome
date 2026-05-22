from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from yutome.config import AppConfig
from yutome.db import catalog_is_initialized, connect_catalog
from yutome.paths import ProjectPaths, resolve_under
from yutome.query import (
    BoolPredicate,
    CompiledQuery,
    DateRange,
    Filter,
    IntPredicate,
    OrderBy,
    QueryRequest,
    QueryResult,
    Search,
    StringPredicate,
    compile_query,
    execute_query,
)
from yutome.retrieval import (
    _chunk_by_id,
    _chunk_by_video_time,
    _format_hit,
    _merge_chunk_text,
    _neighbor_chunks,
    _youtube_url,
    parse_youtube_location,
)
from yutome.transcripts import format_timestamp, read_normalized_segments


@dataclass(frozen=True)
class ContextRequest:
    chunk_id: str | None = None
    video_id: str | None = None
    time_seconds: int | None = None
    youtube_url: str | None = None


def q(*, config: AppConfig, paths: ProjectPaths, request: QueryRequest | dict[str, Any]) -> QueryResult:
    query_request = request if isinstance(request, QueryRequest) else QueryRequest.model_validate(request)
    return execute_query(compile_query(query_request), config, paths)


def find(
    *,
    config: AppConfig,
    paths: ProjectPaths,
    text: str,
    in_: Literal["chunks", "titles", "descriptions"] = "chunks",
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
    filters = _common_filter(channel=channel, since=since, until=until, source=source, language=language)
    if in_ == "chunks":
        search = Search(over="chunk_text", mode=mode or config.find.default_mode, text=text)
        query_request = QueryRequest(
            entity="chunk",
            search=search,
            filter=filters,
            group_by=group_by,
            project=project or ("group_video" if group_by == "video" else "thin"),
            limit=limit,
            offset=offset,
        )
    else:
        if mode in {"semantic", "hybrid"}:
            raise ValueError("title and description search only support lexical mode")
        search = Search(
            over="video_title" if in_ == "titles" else "video_description",
            mode=mode or "lexical",
            text=text,
        )
        query_request = QueryRequest(
            entity="video",
            search=search,
            filter=filters,
            project=project or "video_card",
            limit=limit,
            offset=offset,
        )
    result = q(config=config, paths=paths, request=query_request)
    result = _annotate_empty_corpus(result, paths)
    result = _annotate_no_match(result, paths)
    return result


def list_(
    *,
    config: AppConfig,
    paths: ProjectPaths,
    entity: Literal["video", "videos", "channel", "channels", "attention", "status"],
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
    filters = _common_filter(channel=channel, since=since, until=until, source=source, language=language)
    if selected is not None:
        filters.channel_selected = BoolPredicate(eq=selected)
    if status:
        filters.ingest_status = _status_predicate(status)
    if entity in {"video", "videos"}:
        query_request = QueryRequest(
            entity="video",
            filter=filters,
            project=project or "video_card",
            order_by=_order(order_by),
            limit=limit,
            offset=offset,
        )
    elif entity == "attention":
        if filters.ingest_status is None:
            filters.ingest_status = StringPredicate(starts_with_any=["failed:", "deferred:"])
        query_request = QueryRequest(
            entity="video",
            filter=filters,
            project=project or "video_attention",
            order_by=_order(order_by) or [OrderBy(field="last_attempt_created_at", direction="desc")],
            limit=limit,
            offset=offset,
        )
    elif entity in {"channel", "channels"}:
        query_request = QueryRequest(
            entity="channel",
            filter=filters,
            project=project or "channel_card",
            limit=limit,
            offset=offset,
        )
    elif entity == "status":
        query_request = QueryRequest(project="status_breakdown")
    else:
        raise ValueError(f"unsupported list entity: {entity}")
    result = q(config=config, paths=paths, request=query_request)
    return _annotate_empty_corpus(result, paths)


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
    if kind == "chunk":
        if not id_:
            raise ValueError("chunk id is required")
        return resource_chunk(config=config, paths=paths, chunk_id=id_)
    if kind == "video":
        if not id_:
            raise ValueError("video id is required")
        return resource_video(config=config, paths=paths, video_id=id_)
    if kind == "channel":
        if not id_:
            raise ValueError("channel id or handle is required")
        return resource_channel(config=config, paths=paths, selector=id_)
    if kind == "transcript":
        if not id_:
            raise ValueError("transcript id is required")
        return resource_transcript(
            config=config,
            paths=paths,
            transcript_version_id=id_,
            offset=transcript_offset,
            limit=transcript_limit,
        )
    if kind == "context":
        return context_expand(
            paths=paths,
            request=_context_request(id_=id_, video_id=video_id, time_seconds=time_seconds, youtube_url=youtube_url),
            token_budget=token_budget,
        )
    if kind == "source":
        return source(
            paths=paths,
            request=_context_request(id_=id_, video_id=video_id, time_seconds=time_seconds, youtube_url=youtube_url),
        )
    raise ValueError(f"unsupported show kind: {kind}")


def context_expand(*, paths: ProjectPaths, request: ContextRequest, token_budget: int = 3000) -> dict[str, Any]:
    anchor = _resolve_anchor(paths=paths, request=request)
    with connect_catalog(paths.catalog_db) as connection:
        chunks = _neighbor_chunks(connection, anchor=anchor, token_budget=token_budget)
    text = _merge_chunk_text([chunk["text"] for chunk in chunks])
    estimated_tokens = sum(int(chunk["token_count"] or 0) for chunk in chunks)
    return {
        "anchor": _format_hit(anchor, detail="chunk", include_description=False),
        "token_budget": token_budget,
        "estimated_tokens": estimated_tokens,
        "text": text,
        "chunks": [_format_hit(chunk, detail="chunk", include_description=False) for chunk in chunks],
        "citations": [
            {
                "chunk_id": chunk["chunk_id"],
                "video_id": chunk["video_id"],
                "title": chunk["title"],
                "youtube_url": _youtube_url(chunk["video_id"], chunk["start_ms"]),
                "start_ms": chunk["start_ms"],
                "end_ms": chunk["end_ms"],
                "transcript_version_id": chunk["transcript_version_id"],
                "transcript_source": chunk["transcript_source"],
            }
            for chunk in chunks
        ],
    }


def source(*, paths: ProjectPaths, request: ContextRequest) -> dict[str, Any]:
    anchor = _resolve_anchor(paths=paths, request=request)
    return {
        "chunk_id": anchor["chunk_id"],
        "video_id": anchor["video_id"],
        "title": anchor.get("title"),
        "youtube_url": _youtube_url(anchor["video_id"], anchor["start_ms"]),
        "start_ms": anchor["start_ms"],
        "end_ms": anchor.get("end_ms"),
        "transcript_version_id": anchor.get("transcript_version_id"),
        "transcript_source": anchor.get("transcript_source"),
        "language": anchor.get("language"),
        "is_generated": bool(anchor.get("is_generated")),
    }


def resource_chunk(*, config: AppConfig, paths: ProjectPaths, chunk_id: str) -> dict[str, Any]:
    del config
    with connect_catalog(paths.catalog_db) as connection:
        anchor = _chunk_by_id(connection, chunk_id)
    if anchor is None:
        raise ValueError(f"chunk_id not found: {chunk_id}")
    return {
        "chunk_id": anchor["chunk_id"],
        "resource_uri": f"yutome://chunk/{anchor['chunk_id']}",
        "video_id": anchor["video_id"],
        "title": anchor.get("title"),
        "youtube_url": _youtube_url(anchor["video_id"], anchor["start_ms"]),
        "start_ms": anchor["start_ms"],
        "end_ms": anchor["end_ms"],
        "text": anchor.get("text", ""),
        "token_count": anchor.get("token_count"),
        "sequence": anchor.get("sequence"),
        "transcript_version_id": anchor.get("transcript_version_id"),
        "transcript_source": anchor.get("transcript_source"),
        "language": anchor.get("language"),
        "is_generated": bool(anchor.get("is_generated")),
        "chunker_version": anchor.get("chunker_version"),
    }


def resource_video(*, config: AppConfig, paths: ProjectPaths, video_id: str) -> dict[str, Any]:
    result = q(
        config=config,
        paths=paths,
        request=QueryRequest(
            entity="video",
            filter=Filter(video_id=StringPredicate(eq=video_id)),
            project="metadata",
            limit=1,
        ),
    )
    if not result.rows:
        raise ValueError(f"video_id not found: {video_id}")
    return result.rows[0]


def resource_channel(*, config: AppConfig, paths: ProjectPaths, selector: str) -> dict[str, Any]:
    filters = Filter()
    if selector.startswith("@") or not selector.startswith("UC"):
        filters.channel_handle = StringPredicate(eq=selector)
    else:
        filters.channel_id = StringPredicate(eq=selector)
    result = q(
        config=config,
        paths=paths,
        request=QueryRequest(entity="channel", filter=filters, project="channel_card", limit=1),
    )
    if not result.rows:
        raise ValueError(f"channel not found: {selector}")
    return result.rows[0]


_TRANSCRIPT_TEXT_CAP = 200_000


def resource_transcript(
    *,
    config: AppConfig,
    paths: ProjectPaths,
    transcript_version_id: str,
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    del config
    offset = max(0, offset)
    if limit is not None:
        limit = max(1, min(limit, 5000))
    with connect_catalog(paths.catalog_db) as connection:
        row = connection.execute(
            """
            SELECT transcript_version_id, video_id, source, language, is_generated,
                   raw_path, normalized_path, segment_count, active, created_at
            FROM transcript_versions
            WHERE transcript_version_id = ?
               OR (video_id = ? AND active = 1)
            ORDER BY active DESC, created_at DESC
            LIMIT 1
            """,
            (transcript_version_id, transcript_version_id),
        ).fetchone()
    if row is None:
        raise ValueError(f"transcript or active video transcript not found: {transcript_version_id}")

    normalized_path = resolve_under(paths.root, Path(row["normalized_path"])) if row["normalized_path"] else None
    text = ""
    text_truncated = False
    text_path: str | None = None
    if normalized_path is not None:
        text_file = normalized_path.with_name("transcript.txt")
        text_path = str(text_file)
        if text_file.exists():
            raw = text_file.read_text(encoding="utf-8", errors="replace")
            if len(raw) > _TRANSCRIPT_TEXT_CAP:
                text = raw[:_TRANSCRIPT_TEXT_CAP]
                text_truncated = True
            else:
                text = raw
        if limit is not None or offset:
            segments = read_normalized_segments(normalized_path)
            selected = segments[offset : offset + limit if limit is not None else None]
            text = "\n".join(
                f"[{format_timestamp(segment.start_ms)}] {segment.text}" for segment in selected
            )
            text_truncated = offset > 0 or offset + len(selected) < len(segments)
        else:
            segments = []
            selected = []
    else:
        segments = []
        selected = []

    return {
        "resource_uri": f"yutome://transcript/{row['transcript_version_id']}",
        "transcript_version_id": row["transcript_version_id"],
        "video_id": row["video_id"],
        "source": row["source"],
        "language": row["language"],
        "is_generated": bool(row["is_generated"]),
        "segment_count": row["segment_count"],
        "active": bool(row["active"]),
        "created_at": row["created_at"],
        "normalized_path": str(normalized_path) if normalized_path else None,
        "text_path": text_path,
        "text_truncated": text_truncated,
        "text_char_limit": _TRANSCRIPT_TEXT_CAP,
        "offset": offset,
        "limit": limit,
        "returned_segments": len(selected),
        "next_offset": (
            offset + len(selected)
            if (limit is not None or offset) and offset + len(selected) < int(row["segment_count"])
            else None
        ),
        "text": text,
    }


def _resolve_anchor(*, paths: ProjectPaths, request: ContextRequest) -> dict[str, Any]:
    chunk_id = request.chunk_id
    video_id = request.video_id
    time_seconds = request.time_seconds
    if request.youtube_url:
        parsed_video_id, parsed_time = parse_youtube_location(request.youtube_url)
        video_id = video_id or parsed_video_id
        time_seconds = time_seconds if time_seconds is not None else parsed_time

    with connect_catalog(paths.catalog_db) as connection:
        if chunk_id:
            anchor = _chunk_by_id(connection, chunk_id)
        elif video_id and time_seconds is not None:
            anchor = _chunk_by_video_time(connection, video_id=video_id, time_ms=time_seconds * 1000)
        else:
            raise ValueError("Provide a chunk id, a timestamped YouTube URL, or video_id with time_seconds.")
    if anchor is None:
        raise ValueError("No matching chunk found.")
    return anchor


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


_EMPTY_CORPUS_NOTE = (
    "No videos indexed yet — run `yutome sync <channel-url>` to index a channel before searching."
)

_NO_MATCH_NOTE = (
    "No matches for this query. Try different phrasing, `--mode lexical` for exact terms, "
    "or `yutome list videos --limit 5` to see what's indexed."
)


def _video_count(paths: ProjectPaths) -> int | None:
    if not catalog_is_initialized(paths.catalog_db):
        return None
    try:
        with connect_catalog(paths.catalog_db) as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM videos").fetchone()
            return int(row["count"]) if row is not None else 0
    except Exception:
        return None


def _annotate_empty_corpus(result: QueryResult, paths: ProjectPaths) -> QueryResult:
    if result.rows:
        return result
    count = _video_count(paths)
    if (count is None or count == 0) and _EMPTY_CORPUS_NOTE not in result.notes:
        result.notes.append(_EMPTY_CORPUS_NOTE)
    return result


def _annotate_no_match(result: QueryResult, paths: ProjectPaths) -> QueryResult:
    if result.rows:
        return result
    count = _video_count(paths)
    if count and count > 0 and _NO_MATCH_NOTE not in result.notes:
        result.notes.append(_NO_MATCH_NOTE)
    return result


def _common_filter(
    *,
    channel: str | None,
    since: str | None,
    until: str | None,
    source: str | None,
    language: str | None,
) -> Filter:
    filters = Filter()
    if channel:
        if channel.startswith("UC"):
            filters.channel_id = StringPredicate(eq=channel)
        else:
            filters.channel_handle = StringPredicate(eq=channel)
    if since or until:
        filters.published_at = DateRange(gte=since, lte=until)
    if source:
        filters.transcript_source = StringPredicate(starts_with=source)
    if language:
        filters.language = StringPredicate(eq=language)
    return filters


def _status_predicate(status: str) -> StringPredicate:
    if status.endswith("*"):
        return StringPredicate(starts_with=status[:-1])
    return StringPredicate(eq=status)


_ORDER_BY_ALIASES: dict[str, tuple[str, str]] = {
    # Aliases the model is told to use in tool descriptions. Map to real
    # OrderBy fields so callers can keep using natural names.
    "newest": ("published_at", "desc"),
    "oldest": ("published_at", "asc"),
    "longest": ("duration_seconds", "desc"),
    "shortest": ("duration_seconds", "asc"),
    "title_asc": ("title", "asc"),
    "title_desc": ("title", "desc"),
    "title": ("title", "asc"),
    "updated": ("last_attempt_created_at", "desc"),
    "relevance": ("score", "desc"),
}


def _order(order_by: str | None) -> list[OrderBy]:
    if not order_by:
        return []
    if order_by in _ORDER_BY_ALIASES:
        field, direction = _ORDER_BY_ALIASES[order_by]
        return [OrderBy(field=field, direction=direction)]  # type: ignore[arg-type]
    if ":" in order_by:
        field, direction = order_by.split(":", 1)
        return [OrderBy(field=field, direction="asc" if direction == "asc" else "desc")]  # type: ignore[arg-type]
    return [OrderBy(field=order_by, direction="desc")]  # type: ignore[arg-type]
