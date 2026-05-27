from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from yutome.chunking import CHUNKER_VERSION, Chunk
from yutome.channels import channel_from_input
from yutome.hashing import sha256_json
from yutome.hosted.ids import input_hash
from yutome.hosted.migrations import (
    HOSTED_DEFAULT_EMBEDDING_DIMENSION,
    HOSTED_DEFAULT_EMBEDDING_MODEL,
    HOSTED_DEFAULT_TOKENIZER,
    HOSTED_VECTOR_BACKEND,
)
from yutome.hosted.repositories import SqlStatement
from yutome.youtube import DiscoveredVideo


class SqlConnection(Protocol):
    def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> Any:
        ...


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def list_catalog_videos(connection: SqlConnection, *, workspace_id: str, channel_selector: str | None = None) -> list[DiscoveredVideo]:
    candidates = _channel_selector_candidates(channel_selector) if channel_selector and channel_selector != "catalog" else []
    statement = SqlStatement(
        sql="""
SELECT
    v.id AS hosted_video_id,
    v.youtube_video_id,
    v.title,
    v.channel_id,
    v.duration_seconds,
    v.metadata_json,
    s.display_name AS source_display_name
FROM videos v
LEFT JOIN sources s ON s.id = v.source_id AND s.workspace_id = v.workspace_id
WHERE v.workspace_id = %(workspace_id)s
  AND (
    cardinality(%(candidates)s::text[]) = 0
    OR v.id = ANY(%(candidates)s::text[])
    OR v.youtube_video_id = ANY(%(candidates)s::text[])
    OR v.channel_id = ANY(%(candidates)s::text[])
    OR v.metadata_json->>'channel_handle' = ANY(%(candidates)s::text[])
    OR ('@' || v.metadata_json->>'channel_handle') = ANY(%(candidates)s::text[])
    OR v.metadata_json->>'channel_title' = ANY(%(candidates)s::text[])
    OR s.canonical_channel_id = ANY(%(candidates)s::text[])
    OR s.source_url = ANY(%(candidates)s::text[])
    OR s.display_name = ANY(%(candidates)s::text[])
  )
ORDER BY v.published_at DESC NULLS LAST, v.created_at DESC, v.id;
""".strip(),
        params={"workspace_id": workspace_id, "candidates": candidates},
    )
    rows = _rows_from_result(connection.execute(statement.sql, statement.params))
    return [_discovered_video_from_row(row) for row in rows]


def upsert_channel_from_discovery(
    connection: SqlConnection,
    video: DiscoveredVideo,
    *,
    workspace_id: str,
    source_url: str,
) -> str:
    channel_id = video.channel_id or "unknown-channel"
    channel = channel_from_input(source_url, title=video.channel_title, import_source="yt_dlp")
    handle = video.channel_handle or (channel.handle if channel else None)
    source_id = _source_id(workspace_id, source_url)
    statement = SqlStatement(
        sql="""
INSERT INTO sources (
    id, workspace_id, source_type, source_url, canonical_channel_id,
    display_name, selected, auto_index_allowed, import_source, metadata_json, status,
    last_discovered_at
)
VALUES (
    %(id)s, %(workspace_id)s, %(source_type)s, %(source_url)s,
    %(canonical_channel_id)s, %(display_name)s, true, true, 'yt_dlp',
    %(metadata_json)s::jsonb, 'active', now()
)
ON CONFLICT (workspace_id, source_url) DO UPDATE
SET source_type = EXCLUDED.source_type,
    canonical_channel_id = COALESCE(EXCLUDED.canonical_channel_id, sources.canonical_channel_id),
    display_name = COALESCE(EXCLUDED.display_name, sources.display_name),
    metadata_json = sources.metadata_json || EXCLUDED.metadata_json,
    status = 'active',
    last_discovered_at = now(),
    updated_at = now()
RETURNING *;
""".strip(),
        params={
            "id": source_id,
            "workspace_id": workspace_id,
            "source_type": "channel" if video.channel_id else "handle" if handle else "url",
            "source_url": source_url,
            "canonical_channel_id": video.channel_id,
            "display_name": video.channel_title,
            "metadata_json": _json_param({"handle": handle}),
        },
    )
    connection.execute(statement.sql, statement.params)
    return channel_id


def upsert_discovered_video(
    connection: SqlConnection,
    video: DiscoveredVideo,
    *,
    workspace_id: str,
    channel_id: str,
    source_id: str | None = None,
) -> None:
    thumbnail_url = None
    thumbnails = video.raw.get("thumbnails") or []
    if thumbnails:
        thumbnail_url = thumbnails[-1].get("url")
    metadata = {
        "source": "yt_dlp.discovery",
        "metadata_hash": sha256_json(video.raw),
        "channel_title": video.channel_title,
        "channel_handle": video.channel_handle,
        "thumbnail_url": thumbnail_url,
        "playlist_tab": video.playlist_tab,
        "ingest_status": "discovered",
    }
    statement = SqlStatement(
        sql="""
INSERT INTO videos (
    id, workspace_id, source_id, youtube_video_id, channel_id, title,
    published_at, duration_seconds, metadata_json
)
VALUES (
    %(id)s, %(workspace_id)s, %(source_id)s, %(youtube_video_id)s,
    %(channel_id)s, %(title)s, %(published_at)s, %(duration_seconds)s,
    %(metadata_json)s::jsonb
)
ON CONFLICT (workspace_id, youtube_video_id) DO UPDATE
SET source_id = COALESCE(EXCLUDED.source_id, videos.source_id),
    channel_id = COALESCE(EXCLUDED.channel_id, videos.channel_id),
    title = COALESCE(NULLIF(EXCLUDED.title, ''), videos.title),
    duration_seconds = COALESCE(EXCLUDED.duration_seconds, videos.duration_seconds),
    published_at = COALESCE(EXCLUDED.published_at, videos.published_at),
    metadata_json = videos.metadata_json || EXCLUDED.metadata_json,
    updated_at = now()
RETURNING *;
""".strip(),
        params={
            "id": _hosted_video_id(workspace_id, video.video_id),
            "workspace_id": workspace_id,
            "source_id": source_id,
            "youtube_video_id": video.video_id,
            "channel_id": None if channel_id == "unknown-channel" else channel_id,
            "title": video.title or "",
            "published_at": _published_at_from_flat_discovery(video.raw),
            "duration_seconds": video.duration_seconds,
            "metadata_json": _json_param(metadata),
        },
    )
    connection.execute(statement.sql, statement.params)


def upsert_video_metadata(
    connection: SqlConnection,
    *,
    workspace_id: str,
    video_id: str,
    channel_id: str | None,
    metadata: dict[str, Any],
    source_id: str | None = None,
) -> str:
    metadata_hash = sha256_json(metadata)
    thumbnail_url = metadata.get("thumbnail")
    if not thumbnail_url and metadata.get("thumbnails"):
        thumbnail_url = metadata["thumbnails"][-1].get("url")
    metadata_json = {
        "source": "yt_dlp.metadata",
        "metadata_hash": metadata_hash,
        "live_status": metadata.get("live_status"),
        "thumbnail_url": thumbnail_url,
        "channel_title": metadata.get("channel") or metadata.get("uploader"),
        "channel_handle": metadata.get("channel_handle") or metadata.get("uploader_id"),
        "ingest_status": "metadata",
    }
    statement = SqlStatement(
        sql="""
INSERT INTO videos (
    id, workspace_id, source_id, youtube_video_id, channel_id, title,
    description, published_at, duration_seconds, metadata_json
)
VALUES (
    %(id)s, %(workspace_id)s, %(source_id)s, %(youtube_video_id)s,
    %(channel_id)s, %(title)s, %(description)s, %(published_at)s,
    %(duration_seconds)s, %(metadata_json)s::jsonb
)
ON CONFLICT (workspace_id, youtube_video_id) DO UPDATE
SET source_id = COALESCE(EXCLUDED.source_id, videos.source_id),
    channel_id = COALESCE(EXCLUDED.channel_id, videos.channel_id),
    title = COALESCE(NULLIF(EXCLUDED.title, ''), videos.title),
    description = COALESCE(NULLIF(EXCLUDED.description, ''), videos.description),
    duration_seconds = COALESCE(EXCLUDED.duration_seconds, videos.duration_seconds),
    published_at = COALESCE(EXCLUDED.published_at, videos.published_at),
    metadata_json = videos.metadata_json || EXCLUDED.metadata_json,
    updated_at = now()
RETURNING *;
""".strip(),
        params={
            "id": _hosted_video_id(workspace_id, video_id),
            "workspace_id": workspace_id,
            "source_id": source_id,
            "youtube_video_id": video_id,
            "channel_id": channel_id,
            "title": metadata.get("title") or "",
            "description": metadata.get("description") or "",
            "published_at": _published_at_from_flat_discovery(metadata),
            "duration_seconds": int(metadata["duration"]) if metadata.get("duration") is not None else None,
            "metadata_json": _json_param(metadata_json),
        },
    )
    connection.execute(statement.sql, statement.params)
    return metadata_hash


def transcript_exists(connection: SqlConnection, *, workspace_id: str, video_id: str) -> bool:
    row = _one_from_result(
        connection.execute(
            """
SELECT 1
FROM videos v
JOIN transcript_versions tv
  ON tv.id = v.active_transcript_version_id
 AND tv.workspace_id = v.workspace_id
WHERE v.workspace_id = %(workspace_id)s
  AND (v.id = %(video_id)s OR v.youtube_video_id = %(video_id)s)
LIMIT 1;
""".strip(),
            {"workspace_id": workspace_id, "video_id": video_id},
        )
    )
    return row is not None


def video_ingest_status(connection: SqlConnection, *, workspace_id: str, video_id: str) -> str | None:
    row = _one_from_result(
        connection.execute(
            """
SELECT active_transcript_version_id, metadata_json->>'ingest_status' AS ingest_status
FROM videos
WHERE workspace_id = %(workspace_id)s
  AND (id = %(video_id)s OR youtube_video_id = %(video_id)s)
LIMIT 1;
""".strip(),
            {"workspace_id": workspace_id, "video_id": video_id},
        )
    )
    if row is None:
        return None
    if row.get("ingest_status"):
        return str(row["ingest_status"])
    return "indexed" if row.get("active_transcript_version_id") else "metadata"


def active_transcript_source(connection: SqlConnection, *, workspace_id: str, video_id: str) -> str | None:
    row = _one_from_result(
        connection.execute(
            """
SELECT tv.source
FROM videos v
JOIN transcript_versions tv
  ON tv.id = v.active_transcript_version_id
 AND tv.workspace_id = v.workspace_id
WHERE v.workspace_id = %(workspace_id)s
  AND (v.id = %(video_id)s OR v.youtube_video_id = %(video_id)s)
LIMIT 1;
""".strip(),
            {"workspace_id": workspace_id, "video_id": video_id},
        )
    )
    return None if row is None else str(row["source"])


def upsert_transcript_and_chunks(
    connection: SqlConnection,
    *,
    workspace_id: str,
    transcript_version_id: str,
    video_id: str,
    channel_id: str | None,
    source: str,
    language: str | None,
    is_generated: bool,
    raw_path: Path,
    normalized_path: Path,
    text_hash: str,
    segment_count: int,
    chunks: list[Chunk],
) -> None:
    hosted_video_id = _hosted_video_id(workspace_id, video_id)
    index_profile_id = _default_index_profile_id(workspace_id)
    _execute_statement(connection, _ensure_video_sql(workspace_id=workspace_id, hosted_video_id=hosted_video_id, youtube_video_id=video_id, channel_id=channel_id))
    _execute_statement(connection, _ensure_index_profile_sql(workspace_id=workspace_id, index_profile_id=index_profile_id))
    _execute_statement(
        connection,
        _upsert_transcript_version_sql(
            workspace_id=workspace_id,
            hosted_video_id=hosted_video_id,
            transcript_version_id=transcript_version_id,
            source=source,
            language=language,
            is_generated=is_generated,
            raw_path=raw_path,
            normalized_path=normalized_path,
            text_hash=text_hash,
            segment_count=segment_count,
        ),
    )
    chunk_ids = [_hosted_chunk_id(workspace_id, transcript_version_id, chunk.sequence) for chunk in chunks]
    _execute_statement(
        connection,
        SqlStatement(
            sql="""
DELETE FROM chunk_embeddings ce
USING chunks c
WHERE ce.chunk_id = c.id
  AND ce.workspace_id = c.workspace_id
  AND c.workspace_id = %(workspace_id)s
  AND c.transcript_version_id = %(transcript_version_id)s
  AND c.index_profile_id = %(index_profile_id)s
  AND NOT (c.id = ANY(%(chunk_ids)s::text[]));
""".strip(),
            params={
                "workspace_id": workspace_id,
                "transcript_version_id": transcript_version_id,
                "index_profile_id": index_profile_id,
                "chunk_ids": chunk_ids,
            },
        ),
    )
    _execute_statement(
        connection,
        SqlStatement(
            sql="""
DELETE FROM chunks
WHERE workspace_id = %(workspace_id)s
  AND transcript_version_id = %(transcript_version_id)s
  AND index_profile_id = %(index_profile_id)s
  AND NOT (id = ANY(%(chunk_ids)s::text[]));
""".strip(),
            params={
                "workspace_id": workspace_id,
                "transcript_version_id": transcript_version_id,
                "index_profile_id": index_profile_id,
                "chunk_ids": chunk_ids,
            },
        ),
    )
    for chunk, chunk_id in zip(chunks, chunk_ids, strict=True):
        _execute_statement(
            connection,
            _upsert_chunk_sql(
                workspace_id=workspace_id,
                hosted_video_id=hosted_video_id,
                transcript_version_id=transcript_version_id,
                index_profile_id=index_profile_id,
                chunk=chunk,
                chunk_id=chunk_id,
            ),
        )
    _execute_statement(
        connection,
        SqlStatement(
            sql="""
UPDATE videos
SET active_transcript_version_id = %(transcript_version_id)s,
    metadata_json = metadata_json || %(metadata_json)s::jsonb,
    updated_at = now()
WHERE workspace_id = %(workspace_id)s
  AND id = %(hosted_video_id)s;
""".strip(),
            params={
                "workspace_id": workspace_id,
                "hosted_video_id": hosted_video_id,
                "transcript_version_id": transcript_version_id,
                "metadata_json": _json_param({"ingest_status": "indexed"}),
            },
        ),
    )


def mark_video_failed(connection: SqlConnection, *, workspace_id: str, video_id: str, error: str) -> None:
    _set_video_ingest_status(connection, workspace_id=workspace_id, video_id=video_id, status=f"failed: {error[:200]}")


def mark_video_deferred(connection: SqlConnection, *, workspace_id: str, video_id: str, reason: str) -> None:
    _set_video_ingest_status(connection, workspace_id=workspace_id, video_id=video_id, status=f"deferred: {reason[:200]}")


def record_transcript_attempt(
    connection: SqlConnection,
    *,
    workspace_id: str,
    video_id: str,
    tool: str,
    status: str,
    error_class: str | None = None,
    error: str | None = None,
    retryable: bool = False,
) -> None:
    attempt = {
        "tool": tool,
        "status": status,
        "error_class": error_class,
        "error": error[:1000] if error else None,
        "retryable": retryable,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    connection.execute(
        """
UPDATE videos
SET metadata_json = jsonb_set(
        metadata_json,
        '{transcript_attempts}',
        COALESCE(metadata_json->'transcript_attempts', '[]'::jsonb) || %(attempt_json)s::jsonb,
        true
    ),
    updated_at = now()
WHERE workspace_id = %(workspace_id)s
  AND (id = %(video_id)s OR youtube_video_id = %(video_id)s);
""".strip(),
        {
            "workspace_id": workspace_id,
            "video_id": video_id,
            "attempt_json": _json_param([_compact(attempt)]),
        },
    )


def rebuild_fts(connection: SqlConnection, *, workspace_id: str | None = None) -> None:
    connection.execute(
        """
UPDATE chunks
SET bm25_document = tokenize(chunks.text, sip.tokenizer)::bm25vector
FROM search_index_profiles sip
WHERE chunks.index_profile_id = sip.id
  AND chunks.workspace_id = sip.workspace_id
  AND (%(workspace_id)s::text IS NULL OR chunks.workspace_id = %(workspace_id)s::text);
""".strip(),
        {"workspace_id": workspace_id},
    )


def _channel_selector_candidates(selector: str | None) -> list[str]:
    if not selector:
        return []
    candidates = {selector.strip()}
    channel = channel_from_input(selector)
    if channel is not None:
        candidates.add(channel.source_url)
        candidates.add(channel.source)
        if channel.channel_id:
            candidates.add(channel.channel_id)
        if channel.handle:
            candidates.add(channel.handle)
            candidates.add(f"@{channel.handle}")
    return sorted(candidate for candidate in candidates if candidate)


def _published_at_from_flat_discovery(raw: Mapping[str, Any]) -> datetime | None:
    for key in ("upload_date", "release_date", "modified_date"):
        value = raw.get(key)
        if parsed := _parse_yyyymmdd(value):
            return parsed
    for key in ("timestamp", "release_timestamp"):
        value = raw.get(key)
        if value is None:
            continue
        try:
            return datetime.fromtimestamp(int(value), tz=UTC)
        except (TypeError, ValueError, OSError):
            continue
    return None


def _parse_yyyymmdd(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _discovered_video_from_row(row: Mapping[str, Any]) -> DiscoveredVideo:
    metadata = _json_value(row.get("metadata_json"))
    youtube_video_id = str(row.get("youtube_video_id") or row["hosted_video_id"])
    return DiscoveredVideo(
        video_id=youtube_video_id,
        title=str(row.get("title") or youtube_video_id),
        url=f"https://www.youtube.com/watch?v={youtube_video_id}",
        channel_id=_optional_str(row.get("channel_id")),
        channel_title=_optional_str(metadata.get("channel_title")) or _optional_str(row.get("source_display_name")),
        channel_handle=_optional_str(metadata.get("channel_handle")),
        duration_seconds=row.get("duration_seconds"),
        playlist_tab=_optional_str(metadata.get("playlist_tab")) or "postgres",
        raw=metadata,
    )


def _set_video_ingest_status(connection: SqlConnection, *, workspace_id: str, video_id: str, status: str) -> None:
    connection.execute(
        """
UPDATE videos
SET metadata_json = metadata_json || %(metadata_json)s::jsonb,
    updated_at = now()
WHERE workspace_id = %(workspace_id)s
  AND (id = %(video_id)s OR youtube_video_id = %(video_id)s);
""".strip(),
        {"workspace_id": workspace_id, "video_id": video_id, "metadata_json": _json_param({"ingest_status": status})},
    )


def _ensure_video_sql(*, workspace_id: str, hosted_video_id: str, youtube_video_id: str, channel_id: str | None) -> SqlStatement:
    return SqlStatement(
        sql="""
INSERT INTO videos (id, workspace_id, youtube_video_id, channel_id, title)
VALUES (%(id)s, %(workspace_id)s, %(youtube_video_id)s, %(channel_id)s, '')
ON CONFLICT (workspace_id, youtube_video_id) DO UPDATE
SET channel_id = COALESCE(EXCLUDED.channel_id, videos.channel_id),
    updated_at = now()
RETURNING *;
""".strip(),
        params={
            "id": hosted_video_id,
            "workspace_id": workspace_id,
            "youtube_video_id": youtube_video_id,
            "channel_id": channel_id,
        },
    )


def _ensure_index_profile_sql(*, workspace_id: str, index_profile_id: str) -> SqlStatement:
    return SqlStatement(
        sql="""
INSERT INTO search_index_profiles (
    id, workspace_id, backend, embedding_model, embedding_dimension,
    chunking_version, tokenizer, metadata_json
)
VALUES (
    %(id)s, %(workspace_id)s, %(backend)s, %(embedding_model)s,
    %(embedding_dimension)s, %(chunking_version)s, %(tokenizer)s,
    %(metadata_json)s::jsonb
)
ON CONFLICT (id) DO UPDATE
SET backend = EXCLUDED.backend,
    embedding_model = EXCLUDED.embedding_model,
    embedding_dimension = EXCLUDED.embedding_dimension,
    chunking_version = EXCLUDED.chunking_version,
    tokenizer = EXCLUDED.tokenizer,
    metadata_json = EXCLUDED.metadata_json
RETURNING *;
""".strip(),
        params={
            "id": index_profile_id,
            "workspace_id": workspace_id,
            "backend": HOSTED_VECTOR_BACKEND,
            "embedding_model": HOSTED_DEFAULT_EMBEDDING_MODEL,
            "embedding_dimension": HOSTED_DEFAULT_EMBEDDING_DIMENSION,
            "chunking_version": CHUNKER_VERSION,
            "tokenizer": HOSTED_DEFAULT_TOKENIZER,
            "metadata_json": _json_param({"owner": "yutome.store"}),
        },
    )


def _upsert_transcript_version_sql(
    *,
    workspace_id: str,
    hosted_video_id: str,
    transcript_version_id: str,
    source: str,
    language: str | None,
    is_generated: bool,
    raw_path: Path,
    normalized_path: Path,
    text_hash: str,
    segment_count: int,
) -> SqlStatement:
    metadata = {
        "is_generated": is_generated,
        "raw_path": str(raw_path),
        "normalized_path": str(normalized_path),
        "segment_count": segment_count,
    }
    return SqlStatement(
        sql="""
INSERT INTO transcript_versions (
    id, workspace_id, video_id, source, language_code, content_hash, metadata_json
)
VALUES (
    %(id)s, %(workspace_id)s, %(video_id)s, %(source)s,
    %(language_code)s, %(content_hash)s, %(metadata_json)s::jsonb
)
ON CONFLICT (id) DO UPDATE
SET source = EXCLUDED.source,
    language_code = EXCLUDED.language_code,
    content_hash = EXCLUDED.content_hash,
    metadata_json = EXCLUDED.metadata_json
RETURNING *;
""".strip(),
        params={
            "id": transcript_version_id,
            "workspace_id": workspace_id,
            "video_id": hosted_video_id,
            "source": source,
            "language_code": language,
            "content_hash": text_hash,
            "metadata_json": _json_param(metadata),
        },
    )


def _upsert_chunk_sql(
    *,
    workspace_id: str,
    hosted_video_id: str,
    transcript_version_id: str,
    index_profile_id: str,
    chunk: Chunk,
    chunk_id: str,
) -> SqlStatement:
    metadata = {
        "token_count": chunk.token_count,
        "text_hash": chunk.text_hash,
        "segment_ids": chunk.segment_ids,
        "forced_split": chunk.forced_split,
        "chunker_version": CHUNKER_VERSION,
    }
    return SqlStatement(
        sql="""
INSERT INTO chunks (
    id, workspace_id, video_id, transcript_version_id, index_profile_id,
    chunk_index, start_seconds, end_seconds, text, bm25_document, metadata_json
)
VALUES (
    %(id)s, %(workspace_id)s, %(video_id)s, %(transcript_version_id)s,
    %(index_profile_id)s, %(chunk_index)s, %(start_seconds)s, %(end_seconds)s,
    %(text)s, tokenize(%(text)s, %(tokenizer)s)::bm25vector, %(metadata_json)s::jsonb
)
ON CONFLICT (workspace_id, transcript_version_id, index_profile_id, chunk_index) DO UPDATE
SET start_seconds = EXCLUDED.start_seconds,
    end_seconds = EXCLUDED.end_seconds,
    text = EXCLUDED.text,
    bm25_document = EXCLUDED.bm25_document,
    metadata_json = EXCLUDED.metadata_json
RETURNING *;
""".strip(),
        params={
            "id": chunk_id,
            "workspace_id": workspace_id,
            "video_id": hosted_video_id,
            "transcript_version_id": transcript_version_id,
            "index_profile_id": index_profile_id,
            "chunk_index": chunk.sequence,
            "start_seconds": chunk.start_ms / 1000,
            "end_seconds": chunk.end_ms / 1000,
            "text": chunk.text,
            "tokenizer": HOSTED_DEFAULT_TOKENIZER,
            "metadata_json": _json_param(metadata),
        },
    )


def _default_index_profile_id(workspace_id: str) -> str:
    return _stable_id(
        "sip",
        workspace_id,
        HOSTED_VECTOR_BACKEND,
        HOSTED_DEFAULT_EMBEDDING_MODEL,
        str(HOSTED_DEFAULT_EMBEDDING_DIMENSION),
        CHUNKER_VERSION,
        HOSTED_DEFAULT_TOKENIZER,
    )


def _hosted_video_id(workspace_id: str, youtube_video_id: str) -> str:
    return _stable_id("vid", workspace_id, youtube_video_id)


def _hosted_chunk_id(workspace_id: str, transcript_version_id: str, sequence: int) -> str:
    return _stable_id("chk", workspace_id, transcript_version_id, str(sequence))


def _source_id(workspace_id: str, source_url: str) -> str:
    return _stable_id("src", workspace_id, source_url.strip())


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{input_hash(list(parts), prefix='').lstrip('_')[:24]}"


def _execute_statement(connection: SqlConnection, statement: SqlStatement) -> Any:
    return connection.execute(statement.sql, statement.params)


def _one_from_result(result: Any) -> dict[str, Any] | None:
    rows = _rows_from_result(result)
    return rows[0] if rows else None


def _rows_from_result(result: Any) -> list[dict[str, Any]]:
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


def _json_param(value: Any) -> str:
    return json.dumps(_compact(value), sort_keys=True, separators=(",", ":"), default=str)


def _json_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _compact(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _compact(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_compact(item) for item in value]
    return value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
