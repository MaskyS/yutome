from __future__ import annotations

import re
import urllib.parse
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

    def context(
        self,
        *,
        workspace_id: str,
        chunk_id: str | None = None,
        video_id: str | None = None,
        time_seconds: int | None = None,
        youtube_url: str | None = None,
        token_budget: int = 3000,
    ) -> dict[str, Any]:
        if youtube_url:
            parsed_video_id, parsed_time_seconds = parse_youtube_location(youtube_url)
            video_id = video_id or parsed_video_id
            time_seconds = time_seconds if time_seconds is not None else parsed_time_seconds
        statement = context_anchor_sql(
            workspace_id=workspace_id,
            chunk_id=chunk_id,
            video_id=video_id,
            time_seconds=time_seconds,
        )
        anchor = _one(self.connection.execute(statement.sql, statement.params))
        if anchor is None:
            raise HostedResourceNotFound(kind="context", id_=chunk_id or video_id or youtube_url or "")
        chunks_statement = context_chunks_sql(
            workspace_id=workspace_id,
            transcript_version_id=str(anchor["transcript_version_id"]),
        )
        transcript_chunks = _rows(self.connection.execute(chunks_statement.sql, chunks_statement.params))
        return format_context_resource(anchor, transcript_chunks, token_budget=token_budget)

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

    def list_status(self, *, workspace_id: str) -> dict[str, Any]:
        statement = list_status_sql(workspace_id=workspace_id)
        row = _one(self.connection.execute(statement.sql, statement.params))
        return format_status_row(row or {})

    def list_videos(
        self,
        *,
        workspace_id: str,
        limit: int,
        offset: int = 0,
        channel: str | None = None,
        video_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        status: str | None = None,
        source: str | None = None,
        language: str | None = None,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        statement = list_videos_sql(
            workspace_id=workspace_id,
            limit=limit,
            offset=offset,
            channel=channel,
            video_id=video_id,
            since=since,
            until=until,
            status=status,
            source=source,
            language=language,
            order_by=order_by,
        )
        return [format_video_list_row(row) for row in _rows(self.connection.execute(statement.sql, statement.params))]

    def list_channels(
        self,
        *,
        workspace_id: str,
        limit: int,
        offset: int = 0,
        channel: str | None = None,
        since: str | None = None,
        until: str | None = None,
        status: str | None = None,
        source: str | None = None,
        language: str | None = None,
        selected: bool | None = None,
    ) -> list[dict[str, Any]]:
        statement = list_channels_sql(
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
        return [format_channel_list_row(row) for row in _rows(self.connection.execute(statement.sql, statement.params))]


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


def context_anchor_sql(
    *,
    workspace_id: str,
    chunk_id: str | None = None,
    video_id: str | None = None,
    time_seconds: int | None = None,
) -> SqlStatement:
    if not chunk_id and not video_id:
        raise ValueError("context requires a chunk id, video id, or timestamped YouTube URL")
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
  AND (
    (%(chunk_id)s::text IS NOT NULL AND c.id = %(chunk_id)s::text)
    OR (
      %(chunk_id)s::text IS NULL
      AND %(video_id)s::text IS NOT NULL
      AND (v.id = %(video_id)s::text OR v.youtube_video_id = %(video_id)s::text)
      AND v.active_transcript_version_id = c.transcript_version_id
    )
  )
ORDER BY
    CASE
      WHEN %(time_seconds)s::integer IS NOT NULL
       AND c.start_seconds <= %(time_seconds)s::integer
       AND c.end_seconds >= %(time_seconds)s::integer THEN 0
      WHEN %(chunk_id)s::text IS NOT NULL AND c.id = %(chunk_id)s::text THEN 0
      ELSE 1
    END,
    abs(coalesce(c.start_seconds, 0) - coalesce(%(time_seconds)s::integer, 0)),
    c.chunk_index
LIMIT 1;
""".strip(),
        params={
            "workspace_id": workspace_id,
            "chunk_id": _blank_to_none(chunk_id),
            "video_id": _blank_to_none(video_id),
            "time_seconds": time_seconds,
        },
    )


def context_chunks_sql(*, workspace_id: str, transcript_version_id: str) -> SqlStatement:
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
  AND c.transcript_version_id = %(transcript_version_id)s
ORDER BY c.chunk_index;
""".strip(),
        params={"workspace_id": workspace_id, "transcript_version_id": transcript_version_id},
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


def list_status_sql(*, workspace_id: str) -> SqlStatement:
    return SqlStatement(
        sql="""
SELECT
    (SELECT count(*) FROM videos v WHERE v.workspace_id = %(workspace_id)s) AS videos,
    (SELECT count(*) FROM videos v WHERE v.workspace_id = %(workspace_id)s AND v.active_transcript_version_id IS NOT NULL) AS searchable_now,
    (SELECT count(*) FROM videos v WHERE v.workspace_id = %(workspace_id)s AND v.active_transcript_version_id IS NULL) AS still_indexing,
    0::integer AS needs_attention,
    (SELECT count(*) FROM chunks c WHERE c.workspace_id = %(workspace_id)s) AS chunks,
    (SELECT count(*) FROM transcript_versions tv WHERE tv.workspace_id = %(workspace_id)s) AS transcript_versions,
    (SELECT count(*) FROM sources s WHERE s.workspace_id = %(workspace_id)s AND s.source_type = 'channel') AS channels,
    (
        SELECT COALESCE(jsonb_object_agg(status, count), '{}'::jsonb)
        FROM (
            SELECT
                CASE WHEN v.active_transcript_version_id IS NULL THEN 'pending' ELSE 'indexed' END AS status,
                count(*) AS count
            FROM videos v
            WHERE v.workspace_id = %(workspace_id)s
            GROUP BY 1
        ) status_counts
    ) AS statuses;
""".strip(),
        params={"workspace_id": workspace_id},
    )


def list_videos_sql(
    *,
    workspace_id: str,
    limit: int,
    offset: int = 0,
    channel: str | None = None,
    video_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    source: str | None = None,
    language: str | None = None,
    order_by: str | None = None,
) -> SqlStatement:
    order_clause = {
        None: "v.published_at DESC NULLS LAST, v.created_at DESC, v.id",
        "newest": "v.published_at DESC NULLS LAST, v.created_at DESC, v.id",
        "oldest": "v.published_at ASC NULLS LAST, v.created_at ASC, v.id",
        "longest": "v.duration_seconds DESC NULLS LAST, v.published_at DESC NULLS LAST, v.id",
        "shortest": "v.duration_seconds ASC NULLS LAST, v.published_at DESC NULLS LAST, v.id",
        "title": "v.title ASC, v.published_at DESC NULLS LAST, v.id",
    }.get(order_by)
    if order_clause is None:
        order_clause = "v.published_at DESC NULLS LAST, v.created_at DESC, v.id"
    return SqlStatement(
        sql=f"""
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
    count(c.id) FILTER (WHERE c.transcript_version_id = v.active_transcript_version_id) AS active_chunk_count
FROM videos v
LEFT JOIN sources s ON s.id = v.source_id AND s.workspace_id = v.workspace_id
LEFT JOIN transcript_versions tv ON tv.id = v.active_transcript_version_id AND tv.workspace_id = v.workspace_id
LEFT JOIN chunks c ON c.video_id = v.id AND c.workspace_id = v.workspace_id
WHERE v.workspace_id = %(workspace_id)s
  AND (%(video_id)s::text IS NULL OR v.id = %(video_id)s::text OR v.youtube_video_id = %(video_id)s::text)
  AND (
    %(channel)s::text IS NULL
    OR v.channel_id = %(channel)s::text
    OR v.metadata_json->>'channel_handle' = %(channel)s::text
    OR v.metadata_json->>'channel_title' = %(channel)s::text
    OR s.canonical_channel_id = %(channel)s::text
    OR s.display_name = %(channel)s::text
  )
  AND (%(since)s::timestamptz IS NULL OR v.published_at >= %(since)s::timestamptz)
  AND (%(until)s::timestamptz IS NULL OR v.published_at <= %(until)s::timestamptz)
  AND (
    %(status)s::text IS NULL
    OR COALESCE(v.metadata_json->>'ingest_status', CASE WHEN v.active_transcript_version_id IS NULL THEN 'pending' ELSE 'indexed' END) = %(status)s::text
    OR (
      %(status_prefix)s::text IS NOT NULL
      AND COALESCE(v.metadata_json->>'ingest_status', CASE WHEN v.active_transcript_version_id IS NULL THEN 'pending' ELSE 'indexed' END) LIKE %(status_prefix)s::text
    )
  )
  AND (
    %(source)s::text IS NULL
    OR tv.source = %(source)s::text
    OR tv.source LIKE %(source_prefix)s::text
    OR s.id = %(source)s::text
    OR s.source_url = %(source)s::text
    OR s.import_source = %(source)s::text
  )
  AND (%(language)s::text IS NULL OR tv.language_code = %(language)s::text)
GROUP BY v.id, s.id
ORDER BY {order_clause}
LIMIT %(limit)s
OFFSET %(offset)s;
""".strip(),
        params={
            "workspace_id": workspace_id,
            "video_id": video_id,
            "channel": channel,
            "since": _blank_to_none(since),
            "until": _blank_to_none(until),
            "status": _status_exact(status),
            "status_prefix": _status_prefix(status),
            "source": _blank_to_none(source),
            "source_prefix": _prefix(source),
            "language": _blank_to_none(language),
            "limit": max(1, min(limit, 200)),
            "offset": max(0, offset),
        },
    )


def list_channels_sql(
    *,
    workspace_id: str,
    limit: int,
    offset: int = 0,
    channel: str | None = None,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    source: str | None = None,
    language: str | None = None,
    selected: bool | None = None,
) -> SqlStatement:
    return SqlStatement(
        sql="""
WITH channel_sources AS (
    SELECT
        COALESCE(s.canonical_channel_id, s.id) AS channel_id,
        max(s.display_name) AS title,
        bool_or(s.selected) AS selected,
        count(DISTINCT s.id) AS source_count,
        array_remove(array_agg(DISTINCT s.id), NULL) AS source_ids,
        max(s.last_discovered_at) AS last_discovered_at,
        max(s.last_indexed_at) AS last_indexed_at
    FROM sources s
    WHERE s.workspace_id = %(workspace_id)s
      AND s.source_type = 'channel'
      AND (%(selected)s::boolean IS NULL OR s.selected = %(selected)s::boolean)
    GROUP BY COALESCE(s.canonical_channel_id, s.id)
),
channel_videos AS (
    SELECT
        COALESCE(v.channel_id, s.canonical_channel_id) AS channel_id,
        max(COALESCE(v.metadata_json->>'channel_title', s.display_name)) AS title,
        max(v.metadata_json->>'channel_handle') AS channel_handle,
        count(DISTINCT v.id) AS video_count,
        max(v.published_at) AS latest_published_at
    FROM videos v
    LEFT JOIN sources s ON s.id = v.source_id AND s.workspace_id = v.workspace_id
    LEFT JOIN transcript_versions tv ON tv.id = v.active_transcript_version_id AND tv.workspace_id = v.workspace_id
    WHERE v.workspace_id = %(workspace_id)s
      AND (%(since)s::timestamptz IS NULL OR v.published_at >= %(since)s::timestamptz)
      AND (%(until)s::timestamptz IS NULL OR v.published_at <= %(until)s::timestamptz)
      AND (
        %(status)s::text IS NULL
        OR COALESCE(v.metadata_json->>'ingest_status', CASE WHEN v.active_transcript_version_id IS NULL THEN 'pending' ELSE 'indexed' END) = %(status)s::text
        OR (
          %(status_prefix)s::text IS NOT NULL
          AND COALESCE(v.metadata_json->>'ingest_status', CASE WHEN v.active_transcript_version_id IS NULL THEN 'pending' ELSE 'indexed' END) LIKE %(status_prefix)s::text
        )
      )
      AND (
        %(source)s::text IS NULL
        OR tv.source = %(source)s::text
        OR tv.source LIKE %(source_prefix)s::text
        OR s.id = %(source)s::text
        OR s.source_url = %(source)s::text
        OR s.import_source = %(source)s::text
      )
      AND (%(language)s::text IS NULL OR tv.language_code = %(language)s::text)
    GROUP BY COALESCE(v.channel_id, s.canonical_channel_id)
)
SELECT
    COALESCE(cs.channel_id, cv.channel_id) AS channel_id,
    COALESCE(cv.title, cs.title) AS title,
    cv.channel_handle,
    COALESCE(cv.video_count, 0) AS video_count,
    cv.latest_published_at,
    COALESCE(cs.selected, true) AS selected,
    COALESCE(cs.source_count, 0) AS source_count,
    COALESCE(cs.source_ids, ARRAY[]::text[]) AS source_ids,
    cs.last_discovered_at,
    cs.last_indexed_at
FROM channel_sources cs
FULL OUTER JOIN channel_videos cv USING (channel_id)
WHERE COALESCE(cs.channel_id, cv.channel_id) IS NOT NULL
  AND (
    %(channel)s::text IS NULL
    OR COALESCE(cs.channel_id, cv.channel_id) = %(channel)s::text
    OR cv.channel_handle = %(channel)s::text
    OR COALESCE(cv.title, cs.title) = %(channel)s::text
  )
ORDER BY cv.latest_published_at DESC NULLS LAST, COALESCE(cv.title, cs.title) ASC, COALESCE(cs.channel_id, cv.channel_id)
LIMIT %(limit)s
OFFSET %(offset)s;
""".strip(),
        params={
            "workspace_id": workspace_id,
            "channel": channel,
            "since": _blank_to_none(since),
            "until": _blank_to_none(until),
            "status": _status_exact(status),
            "status_prefix": _status_prefix(status),
            "source": _blank_to_none(source),
            "source_prefix": _prefix(source),
            "language": _blank_to_none(language),
            "selected": selected,
            "limit": max(1, min(limit, 200)),
            "offset": max(0, offset),
        },
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


def format_context_resource(anchor: Mapping[str, Any], chunks: list[Mapping[str, Any]], *, token_budget: int) -> dict[str, Any]:
    bounded_budget = max(200, min(int(token_budget), 8000))
    anchor_id = str(anchor["chunk_id"])
    anchor_index = next((index for index, row in enumerate(chunks) if str(row.get("chunk_id")) == anchor_id), None)
    if anchor_index is None:
        chunks = [anchor]
        anchor_index = 0
    selected_indexes = {anchor_index}
    total_tokens = _chunk_token_count(chunks[anchor_index])
    left = anchor_index - 1
    right = anchor_index + 1
    while left >= 0 or right < len(chunks):
        added = False
        for index in (left, right):
            if index < 0 or index >= len(chunks) or index in selected_indexes:
                continue
            candidate_tokens = _chunk_token_count(chunks[index])
            if total_tokens + candidate_tokens > bounded_budget and selected_indexes:
                continue
            selected_indexes.add(index)
            total_tokens += candidate_tokens
            added = True
        left -= 1
        right += 1
        if not added:
            break
    selected_chunks = [chunks[index] for index in sorted(selected_indexes)]
    formatted_chunks = [format_chunk_resource(chunk) for chunk in selected_chunks]
    text = _merge_chunk_text([str(chunk.get("text") or "") for chunk in selected_chunks])
    return {
        "anchor": format_chunk_resource(anchor),
        "token_budget": bounded_budget,
        "estimated_tokens": total_tokens,
        "text": text,
        "chunks": formatted_chunks,
        "citations": [
            {
                "chunk_id": chunk["chunk_id"],
                "video_id": chunk["video_id"],
                "youtube_video_id": chunk.get("youtube_video_id"),
                "title": chunk.get("title"),
                "youtube_url": chunk.get("youtube_url"),
                "start_ms": chunk.get("start_ms"),
                "end_ms": chunk.get("end_ms"),
                "transcript_version_id": chunk.get("transcript_version_id"),
                "transcript_source": chunk.get("transcript_source"),
            }
            for chunk in formatted_chunks
        ],
    }


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


def format_status_row(row: Mapping[str, Any]) -> dict[str, Any]:
    statuses = row.get("statuses") or {}
    if not isinstance(statuses, Mapping):
        statuses = {}
    return {
        "searchable_now": int(row.get("searchable_now") or 0),
        "still_indexing": int(row.get("still_indexing") or 0),
        "needs_attention": int(row.get("needs_attention") or 0),
        "channels": int(row.get("channels") or 0),
        "videos": int(row.get("videos") or 0),
        "chunks": int(row.get("chunks") or 0),
        "transcript_versions": int(row.get("transcript_versions") or 0),
        "statuses": dict(statuses),
    }


def format_video_list_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return format_video_resource(row)


def format_channel_list_row(row: Mapping[str, Any]) -> dict[str, Any]:
    channel_id = str(row["channel_id"])
    return _compact(
        {
            "channel_id": channel_id,
            "library_channel_id": channel_id,
            "resource_uri": f"yutome://channel/{channel_id}",
            "title": row.get("title"),
            "channel_handle": row.get("channel_handle"),
            "selected": row.get("selected"),
            "video_count": row.get("video_count"),
            "latest_published_at": _json_value(row.get("latest_published_at")),
            "source_count": row.get("source_count"),
            "source_ids": list(row.get("source_ids") or []),
            "last_discovered_at": _json_value(row.get("last_discovered_at")),
            "last_indexed_at": _json_value(row.get("last_indexed_at")),
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


def parse_youtube_location(url: str) -> tuple[str | None, int | None]:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    video_id = query.get("v", [None])[0]
    if parsed.hostname and "youtu.be" in parsed.hostname:
        path_id = parsed.path.strip("/")
        video_id = path_id or video_id
    time_seconds = _parse_time_seconds(query.get("t", [None])[0] or query.get("start", [None])[0])
    return video_id, time_seconds


def _parse_time_seconds(value: str | None) -> int | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized.isdigit():
        return int(normalized)
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?", normalized)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _chunk_token_count(row: Mapping[str, Any]) -> int:
    metadata_count = _metadata_value(row.get("chunk_metadata"), "token_count")
    if isinstance(metadata_count, int) and metadata_count > 0:
        return metadata_count
    return max(1, len(str(row.get("text") or "").split()))


def _merge_chunk_text(parts: list[str]) -> str:
    merged_words: list[str] = []
    for part in parts:
        words = part.split()
        if not words:
            continue
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


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _prefix(value: str | None) -> str | None:
    stripped = _blank_to_none(value)
    return f"{stripped}%" if stripped else None


def _status_exact(value: str | None) -> str | None:
    stripped = _blank_to_none(value)
    if not stripped or stripped.endswith("*"):
        return None
    return stripped


def _status_prefix(value: str | None) -> str | None:
    stripped = _blank_to_none(value)
    if not stripped or not stripped.endswith("*"):
        return None
    return f"{stripped[:-1]}%"


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


__all__ = [
    "HostedResourceNotFound",
    "HostedResourceQueries",
    "TRANSCRIPT_TEXT_CAP",
    "channel_resource_sql",
    "context_anchor_sql",
    "context_chunks_sql",
    "chunk_resource_sql",
    "format_context_resource",
    "format_channel_list_row",
    "format_status_row",
    "format_video_list_row",
    "list_channels_sql",
    "list_status_sql",
    "list_videos_sql",
    "source_resource_sql",
    "transcript_resource_sql",
    "video_resource_sql",
]
