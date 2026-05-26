from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from yutome.hosted.repositories import SqlStatement


TRANSCRIPT_TEXT_CAP = 200_000


class HostedResourceNotFound(LookupError):
    def __init__(self, *, kind: str, id_: str) -> None:
        self.kind = kind
        self.id = id_
        super().__init__(f"{kind} not found: {id_}")


@dataclass(frozen=True)
class HostedResourceQueries:
    connection: Any

    def chunk(self, *, workspace_id: str, chunk_id: str) -> dict[str, Any]:
        statement = chunk_resource_sql(workspace_id=workspace_id, chunk_id=chunk_id)
        row = _one(self.connection.execute(statement.sql, statement.params))
        if row is None:
            raise HostedResourceNotFound(kind="chunk", id_=chunk_id)
        return format_chunk_resource(row)

    def video(self, *, workspace_id: str, video_id: str) -> dict[str, Any]:
        statement = video_resource_sql(workspace_id=workspace_id, video_id=video_id)
        row = _one(self.connection.execute(statement.sql, statement.params))
        if row is None:
            raise HostedResourceNotFound(kind="video", id_=video_id)
        return format_video_resource(row)

    def channel(self, *, workspace_id: str, channel_id: str) -> dict[str, Any]:
        statement = channel_resource_sql(workspace_id=workspace_id, channel_id=channel_id)
        row = _one(self.connection.execute(statement.sql, statement.params))
        if row is None:
            raise HostedResourceNotFound(kind="channel", id_=channel_id)
        return format_channel_resource(row)

    def transcript(self, *, workspace_id: str, transcript_version_id: str, offset: int = 0, limit: int | None = None) -> dict[str, Any]:
        statement = transcript_resource_sql(
            workspace_id=workspace_id,
            transcript_version_id=transcript_version_id,
            offset=offset,
            limit=limit,
        )
        rows = _rows(self.connection.execute(statement.sql, statement.params))
        if not rows:
            raise HostedResourceNotFound(kind="transcript", id_=transcript_version_id)
        return format_transcript_resource(rows, offset=offset, limit=limit)

    def source(self, *, workspace_id: str, source_id: str) -> dict[str, Any]:
        statement = source_resource_sql(workspace_id=workspace_id, source_id=source_id)
        row = _one(self.connection.execute(statement.sql, statement.params))
        if row is None:
            raise HostedResourceNotFound(kind="source", id_=source_id)
        return format_source_resource(row)


def chunk_resource_sql(*, workspace_id: str, chunk_id: str) -> SqlStatement:
    return SqlStatement(
        sql="""
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
    v.source_id,
    tv.source AS transcript_source,
    tv.language_code AS language,
    tv.metadata_json AS transcript_metadata
FROM chunks c
JOIN videos v ON v.id = c.video_id AND v.workspace_id = c.workspace_id
JOIN transcript_versions tv ON tv.id = c.transcript_version_id AND tv.workspace_id = c.workspace_id
WHERE c.workspace_id = %(workspace_id)s
  AND c.id = %(chunk_id)s
LIMIT 1;
""".strip(),
        params={"workspace_id": workspace_id, "chunk_id": chunk_id},
    )


def video_resource_sql(*, workspace_id: str, video_id: str) -> SqlStatement:
    return SqlStatement(
        sql="""
SELECT
    v.id AS video_id,
    v.youtube_video_id,
    v.source_id,
    v.active_transcript_version_id,
    v.channel_id,
    v.title,
    v.description,
    v.published_at,
    v.duration_seconds,
    v.metadata_json,
    s.display_name AS source_display_name,
    s.source_url,
    s.source_type,
    COUNT(c.id) FILTER (WHERE c.transcript_version_id = v.active_transcript_version_id) AS active_chunk_count
FROM videos v
LEFT JOIN sources s ON s.id = v.source_id AND s.workspace_id = v.workspace_id
LEFT JOIN chunks c ON c.video_id = v.id AND c.workspace_id = v.workspace_id
WHERE v.workspace_id = %(workspace_id)s
  AND (v.id = %(video_id)s OR v.youtube_video_id = %(video_id)s)
GROUP BY v.id, s.id
LIMIT 1;
""".strip(),
        params={"workspace_id": workspace_id, "video_id": video_id},
    )


def channel_resource_sql(*, workspace_id: str, channel_id: str) -> SqlStatement:
    return SqlStatement(
        sql="""
SELECT
    COALESCE(v.channel_id, s.canonical_channel_id, %(channel_id)s) AS channel_id,
    max(COALESCE(v.metadata_json->>'channel_title', s.display_name)) AS title,
    max(v.metadata_json->>'channel_handle') AS channel_handle,
    count(DISTINCT v.id) AS video_count,
    max(v.published_at) AS latest_published_at,
    array_remove(array_agg(DISTINCT s.id), NULL) AS source_ids
FROM videos v
LEFT JOIN sources s ON s.id = v.source_id AND s.workspace_id = v.workspace_id
WHERE v.workspace_id = %(workspace_id)s
  AND (
    v.channel_id = %(channel_id)s
    OR v.metadata_json->>'channel_handle' = %(channel_id)s
    OR s.canonical_channel_id = %(channel_id)s
  )
GROUP BY COALESCE(v.channel_id, s.canonical_channel_id, %(channel_id)s)
LIMIT 1;
""".strip(),
        params={"workspace_id": workspace_id, "channel_id": channel_id},
    )


def transcript_resource_sql(
    *,
    workspace_id: str,
    transcript_version_id: str,
    offset: int = 0,
    limit: int | None = None,
) -> SqlStatement:
    bounded_offset = max(0, offset)
    bounded_limit = None if limit is None else max(1, min(limit, 5000))
    return SqlStatement(
        sql="""
WITH selected_transcript AS (
    SELECT
        tv.id AS transcript_version_id,
        tv.video_id,
        v.youtube_video_id,
        tv.source,
        tv.language_code,
        tv.content_hash,
        tv.metadata_json,
        tv.created_at,
        (v.active_transcript_version_id = tv.id) AS active,
        count(c.id) AS segment_count
    FROM transcript_versions tv
    JOIN videos v ON v.id = tv.video_id AND v.workspace_id = tv.workspace_id
    LEFT JOIN chunks c ON c.transcript_version_id = tv.id AND c.workspace_id = tv.workspace_id
    WHERE tv.workspace_id = %(workspace_id)s
      AND (
        tv.id = %(transcript_version_id)s
        OR v.id = %(transcript_version_id)s
        OR v.youtube_video_id = %(transcript_version_id)s
      )
    GROUP BY tv.id, v.id
    ORDER BY (tv.id = %(transcript_version_id)s) DESC, active DESC, tv.created_at DESC
    LIMIT 1
),
selected_chunks AS (
    SELECT c.*
    FROM chunks c
    JOIN selected_transcript st ON st.transcript_version_id = c.transcript_version_id
    WHERE c.workspace_id = %(workspace_id)s
    ORDER BY c.chunk_index
    OFFSET %(offset)s
    LIMIT COALESCE(%(limit)s::integer, 2147483647)
)
SELECT
    st.transcript_version_id,
    st.video_id,
    st.youtube_video_id,
    st.source,
    st.language_code,
    st.content_hash,
    st.metadata_json,
    st.created_at,
    st.active,
    st.segment_count,
    c.id AS chunk_id,
    c.chunk_index,
    c.start_seconds,
    c.end_seconds,
    c.text
FROM selected_transcript st
LEFT JOIN selected_chunks c ON true
ORDER BY c.chunk_index NULLS LAST;
""".strip(),
        params={
            "workspace_id": workspace_id,
            "transcript_version_id": transcript_version_id,
            "offset": bounded_offset,
            "limit": bounded_limit,
        },
    )


def source_resource_sql(*, workspace_id: str, source_id: str) -> SqlStatement:
    return SqlStatement(
        sql="""
SELECT
    id AS source_id,
    source_type,
    source_url,
    canonical_channel_id,
    canonical_playlist_id,
    canonical_video_id,
    display_name,
    selected,
    auto_index_allowed,
    import_source,
    auth_grant_id,
    metadata_json,
    status,
    last_discovered_at,
    last_indexed_at,
    created_at,
    updated_at
FROM sources
WHERE workspace_id = %(workspace_id)s
  AND id = %(source_id)s
LIMIT 1;
""".strip(),
        params={"workspace_id": workspace_id, "source_id": source_id},
    )


def format_chunk_resource(row: Mapping[str, Any]) -> dict[str, Any]:
    chunk_id = str(row["chunk_id"])
    video_id = str(row["video_id"])
    start_ms = _seconds_to_ms(row.get("start_seconds"))
    return _compact(
        {
            "chunk_id": chunk_id,
            "resource_uri": f"yutome://chunk/{chunk_id}",
            "video_id": video_id,
            "youtube_video_id": row.get("youtube_video_id"),
            "title": row.get("title"),
            "youtube_url": _youtube_url(str(row.get("youtube_video_id") or video_id), start_ms),
            "start_ms": start_ms,
            "end_ms": _seconds_to_ms(row.get("end_seconds")),
            "text": row.get("text") or "",
            "token_count": _metadata_value(row.get("chunk_metadata"), "token_count"),
            "sequence": row.get("chunk_index"),
            "transcript_version_id": row.get("transcript_version_id"),
            "transcript_source": row.get("transcript_source"),
            "language": row.get("language"),
            "is_generated": _metadata_bool(row.get("transcript_metadata"), "is_generated"),
            "chunker_version": _metadata_value(row.get("chunk_metadata"), "chunking_version"),
            "channel_id": row.get("channel_id"),
            "source_id": row.get("source_id"),
        }
    )


def format_video_resource(row: Mapping[str, Any]) -> dict[str, Any]:
    video_id = str(row["video_id"])
    return _compact(
        {
            "video_id": video_id,
            "resource_uri": f"yutome://video/{video_id}",
            "youtube_video_id": row.get("youtube_video_id"),
            "youtube_url": _youtube_url(str(row.get("youtube_video_id") or video_id), 0),
            "source_id": row.get("source_id"),
            "source_url": row.get("source_url"),
            "source_type": row.get("source_type"),
            "active_transcript_version_id": row.get("active_transcript_version_id"),
            "channel_id": row.get("channel_id"),
            "channel_title": _metadata_value(row.get("metadata_json"), "channel_title") or row.get("source_display_name"),
            "channel_handle": _metadata_value(row.get("metadata_json"), "channel_handle"),
            "title": row.get("title"),
            "description": row.get("description"),
            "published_at": _json_value(row.get("published_at")),
            "duration_seconds": row.get("duration_seconds"),
            "thumbnail_url": _metadata_value(row.get("metadata_json"), "thumbnail_url"),
            "active_chunk_count": row.get("active_chunk_count"),
        }
    )


def format_channel_resource(row: Mapping[str, Any]) -> dict[str, Any]:
    channel_id = str(row["channel_id"])
    return _compact(
        {
            "channel_id": channel_id,
            "resource_uri": f"yutome://channel/{channel_id}",
            "title": row.get("title"),
            "channel_handle": row.get("channel_handle"),
            "video_count": row.get("video_count"),
            "latest_published_at": _json_value(row.get("latest_published_at")),
            "source_ids": list(row.get("source_ids") or []),
        }
    )


def format_transcript_resource(rows: list[Mapping[str, Any]], *, offset: int = 0, limit: int | None = None) -> dict[str, Any]:
    first = rows[0]
    transcript_id = str(first["transcript_version_id"])
    chunks = [row for row in rows if row.get("chunk_id") is not None]
    text_parts = [_timestamped_text(row) for row in chunks]
    text = "\n".join(part for part in text_parts if part)
    text_truncated = len(text) > TRANSCRIPT_TEXT_CAP
    if text_truncated:
        text = text[:TRANSCRIPT_TEXT_CAP]
    returned_segments = len(chunks)
    segment_count = int(first.get("segment_count") or 0)
    next_offset = None
    if limit is not None and offset + returned_segments < segment_count:
        next_offset = offset + returned_segments
    return _compact(
        {
            "resource_uri": f"yutome://transcript/{transcript_id}",
            "transcript_version_id": transcript_id,
            "video_id": first.get("video_id"),
            "youtube_video_id": first.get("youtube_video_id"),
            "source": first.get("source"),
            "language": first.get("language_code"),
            "is_generated": _metadata_bool(first.get("metadata_json"), "is_generated"),
            "segment_count": segment_count,
            "active": bool(first.get("active")),
            "created_at": _json_value(first.get("created_at")),
            "content_hash": first.get("content_hash"),
            "text_truncated": text_truncated,
            "text_char_limit": TRANSCRIPT_TEXT_CAP,
            "offset": offset,
            "limit": limit,
            "returned_segments": returned_segments,
            "next_offset": next_offset,
            "text": text,
        }
    )


def format_source_resource(row: Mapping[str, Any]) -> dict[str, Any]:
    source_id = str(row["source_id"])
    return _compact(
        {
            "source_id": source_id,
            "resource_uri": f"yutome://source/{source_id}",
            "source_type": row.get("source_type"),
            "source_url": row.get("source_url"),
            "canonical_channel_id": row.get("canonical_channel_id"),
            "canonical_playlist_id": row.get("canonical_playlist_id"),
            "canonical_video_id": row.get("canonical_video_id"),
            "display_name": row.get("display_name"),
            "selected": row.get("selected"),
            "auto_index_allowed": row.get("auto_index_allowed"),
            "import_source": row.get("import_source"),
            "auth_grant_id": row.get("auth_grant_id"),
            "status": row.get("status"),
            "last_discovered_at": _json_value(row.get("last_discovered_at")),
            "last_indexed_at": _json_value(row.get("last_indexed_at")),
            "created_at": _json_value(row.get("created_at")),
            "updated_at": _json_value(row.get("updated_at")),
        }
    )


def _one(result: Any) -> dict[str, Any] | None:
    rows = _rows(result)
    return rows[0] if rows else None


def _rows(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if hasattr(result, "mappings"):
        return [dict(row) for row in result.mappings()]
    if hasattr(result, "fetchall"):
        rows = result.fetchall()
    elif isinstance(result, list):
        rows = result
    else:
        rows = list(result)
    return [dict(row) for row in rows]


def _seconds_to_ms(value: Any) -> int:
    if value is None:
        return 0
    return int(round(float(value) * 1000))


def _youtube_url(video_id: str, start_ms: int) -> str:
    if start_ms <= 0:
        return f"https://youtube.com/watch?v={video_id}"
    return f"https://youtube.com/watch?v={video_id}&t={start_ms // 1000}s"


def _timestamped_text(row: Mapping[str, Any]) -> str:
    start_ms = _seconds_to_ms(row.get("start_seconds"))
    text = str(row.get("text") or "").strip()
    if not text:
        return ""
    return f"[{_format_timestamp(start_ms)}] {text}"


def _format_timestamp(ms: int) -> str:
    total = ms // 1000
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _metadata_value(metadata: Any, key: str) -> Any:
    if isinstance(metadata, Mapping):
        return metadata.get(key)
    return None


def _metadata_bool(metadata: Any, key: str) -> bool:
    return bool(_metadata_value(metadata, key))


def _json_value(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


__all__ = [
    "HostedResourceNotFound",
    "HostedResourceQueries",
    "TRANSCRIPT_TEXT_CAP",
    "channel_resource_sql",
    "chunk_resource_sql",
    "source_resource_sql",
    "transcript_resource_sql",
    "video_resource_sql",
]
