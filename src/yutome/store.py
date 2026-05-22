from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from yutome.chunking import CHUNKER_VERSION, Chunk
from yutome.channels import channel_from_input
from yutome.db import connect_catalog
from yutome.hashing import sha256_json
from yutome.youtube import DiscoveredVideo


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def list_catalog_videos(connection: sqlite3.Connection, *, channel_selector: str | None = None) -> list[DiscoveredVideo]:
    clauses: list[str] = []
    params: list[str] = []
    if channel_selector and channel_selector != "catalog":
        candidates = _channel_selector_candidates(channel_selector)
        placeholders = ",".join("?" for _ in candidates)
        clauses.append(
            f"""
            (
                v.video_id IN ({placeholders})
                OR
                v.channel_id IN ({placeholders})
                OR c.channel_id IN ({placeholders})
                OR c.handle IN ({placeholders})
                OR ('@' || c.handle) IN ({placeholders})
                OR c.source_url IN ({placeholders})
                OR c.title IN ({placeholders})
            )
            """
        )
        params.extend(candidates * 7)
    rows = connection.execute(
        f"""
        SELECT
            v.video_id,
            v.title,
            v.channel_id,
            v.duration_seconds,
            c.title AS channel_title,
            c.handle AS channel_handle
        FROM videos v
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        {'WHERE ' + ' AND '.join(clauses) if clauses else ''}
        ORDER BY v.rowid
        """,
        params,
    ).fetchall()
    return [
        DiscoveredVideo(
            video_id=row["video_id"],
            title=row["title"],
            url=f"https://www.youtube.com/watch?v={row['video_id']}",
            channel_id=row["channel_id"],
            channel_title=row["channel_title"],
            channel_handle=row["channel_handle"],
            duration_seconds=row["duration_seconds"],
            playlist_tab="catalog",
            raw={},
        )
        for row in rows
    ]


def _channel_selector_candidates(selector: str) -> list[str]:
    candidates = {selector.strip()}
    channel = channel_from_input(selector)
    if channel is not None:
        candidates.add(channel.source_url)
        if channel.channel_id:
            candidates.add(channel.channel_id)
        if channel.handle:
            candidates.add(channel.handle)
            candidates.add(f"@{channel.handle}")
    return sorted(candidate for candidate in candidates if candidate)


def upsert_channel_from_discovery(
    connection: sqlite3.Connection,
    video: DiscoveredVideo,
    *,
    source_url: str,
) -> str:
    channel_id = video.channel_id or "unknown-channel"
    connection.execute(
        """
        INSERT INTO channels(channel_id, handle, source_url, uploads_url, title, last_synced_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(channel_id) DO UPDATE SET
            handle = COALESCE(excluded.handle, channels.handle),
            source_url = COALESCE(excluded.source_url, channels.source_url),
            uploads_url = COALESCE(excluded.uploads_url, channels.uploads_url),
            title = COALESCE(excluded.title, channels.title),
            last_synced_at = datetime('now')
        """,
        (channel_id, video.channel_handle, source_url, source_url.rstrip("/") + "/videos", video.channel_title),
    )
    return channel_id


def upsert_discovered_video(
    connection: sqlite3.Connection,
    video: DiscoveredVideo,
    *,
    channel_id: str,
) -> None:
    thumbnail_url = None
    thumbnails = video.raw.get("thumbnails") or []
    if thumbnails:
        thumbnail_url = thumbnails[-1].get("url")
    published_at = _published_at_from_flat_discovery(video.raw)
    connection.execute(
        """
        INSERT INTO videos(
            video_id, channel_id, title, duration_seconds, published_at, thumbnail_url, metadata_hash, ingest_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'discovered')
        ON CONFLICT(video_id) DO UPDATE SET
            channel_id = COALESCE(excluded.channel_id, videos.channel_id),
            title = COALESCE(excluded.title, videos.title),
            duration_seconds = COALESCE(excluded.duration_seconds, videos.duration_seconds),
            published_at = COALESCE(excluded.published_at, videos.published_at),
            thumbnail_url = COALESCE(excluded.thumbnail_url, videos.thumbnail_url),
            metadata_hash = excluded.metadata_hash,
            updated_at = datetime('now')
        """,
        (
            video.video_id,
            channel_id,
            video.title,
            video.duration_seconds,
            published_at,
            thumbnail_url,
            sha256_json(video.raw),
        ),
    )


def _published_at_from_flat_discovery(raw: dict[str, Any]) -> str | None:
    for key in ("upload_date", "release_date", "modified_date"):
        value = raw.get(key)
        if value:
            return str(value)
    for key in ("timestamp", "release_timestamp"):
        value = raw.get(key)
        if value is None:
            continue
        try:
            return datetime.fromtimestamp(int(value), tz=UTC).strftime("%Y%m%d")
        except (TypeError, ValueError, OSError):
            continue
    return None


def upsert_video_metadata(
    connection: sqlite3.Connection,
    *,
    video_id: str,
    channel_id: str | None,
    metadata: dict[str, Any],
) -> str:
    metadata_hash = sha256_json(metadata)
    thumbnail_url = metadata.get("thumbnail")
    if not thumbnail_url and metadata.get("thumbnails"):
        thumbnail_url = metadata["thumbnails"][-1].get("url")
    published_at = metadata.get("upload_date") or metadata.get("release_date")
    connection.execute(
        """
        INSERT INTO videos(
            video_id, channel_id, title, description, duration_seconds, published_at,
            live_status, thumbnail_url, metadata_hash, ingest_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'metadata')
        ON CONFLICT(video_id) DO UPDATE SET
            channel_id = COALESCE(excluded.channel_id, videos.channel_id),
            title = COALESCE(excluded.title, videos.title),
            description = COALESCE(excluded.description, videos.description),
            duration_seconds = COALESCE(excluded.duration_seconds, videos.duration_seconds),
            published_at = COALESCE(excluded.published_at, videos.published_at),
            live_status = COALESCE(excluded.live_status, videos.live_status),
            thumbnail_url = COALESCE(excluded.thumbnail_url, videos.thumbnail_url),
            metadata_hash = excluded.metadata_hash,
            ingest_status = CASE
                WHEN videos.ingest_status = 'indexed' THEN 'indexed'
                ELSE 'metadata'
            END,
            updated_at = datetime('now')
        """,
        (
            video_id,
            channel_id,
            metadata.get("title"),
            metadata.get("description"),
            int(metadata["duration"]) if metadata.get("duration") is not None else None,
            published_at,
            metadata.get("live_status"),
            thumbnail_url,
            metadata_hash,
        ),
    )
    return metadata_hash


def transcript_exists(db_path: Path, video_id: str) -> bool:
    with connect_catalog(db_path) as connection:
        row = connection.execute(
            "SELECT 1 FROM transcript_versions WHERE video_id = ? AND active = 1 LIMIT 1",
            (video_id,),
        ).fetchone()
    return row is not None


def video_ingest_status(connection: sqlite3.Connection, *, video_id: str) -> str | None:
    row = connection.execute(
        "SELECT ingest_status FROM videos WHERE video_id = ?",
        (video_id,),
    ).fetchone()
    return None if row is None else str(row["ingest_status"])


def active_transcript_source(connection: sqlite3.Connection, *, video_id: str) -> str | None:
    row = connection.execute(
        """
        SELECT source
        FROM transcript_versions
        WHERE video_id = ? AND active = 1
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (video_id,),
    ).fetchone()
    return None if row is None else str(row["source"])


def upsert_transcript_and_chunks(
    connection: sqlite3.Connection,
    *,
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
    connection.execute("DELETE FROM chunks WHERE video_id = ?", (video_id,))
    connection.execute(
        "UPDATE transcript_versions SET active = 0 WHERE video_id = ?",
        (video_id,),
    )
    connection.execute(
        """
        INSERT INTO transcript_versions(
            transcript_version_id, video_id, source, language, is_generated,
            raw_path, normalized_path, text_hash, segment_count, active
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(transcript_version_id) DO UPDATE SET
            source = excluded.source,
            language = excluded.language,
            is_generated = excluded.is_generated,
            raw_path = excluded.raw_path,
            normalized_path = excluded.normalized_path,
            text_hash = excluded.text_hash,
            segment_count = excluded.segment_count,
            active = 1
        """,
        (
            transcript_version_id,
            video_id,
            source,
            language,
            1 if is_generated else 0,
            str(raw_path),
            str(normalized_path),
            text_hash,
            segment_count,
        ),
    )
    connection.execute("DELETE FROM chunks WHERE transcript_version_id = ?", (transcript_version_id,))
    connection.executemany(
        """
        INSERT INTO chunks(
            chunk_id, transcript_version_id, video_id, channel_id, sequence,
            start_ms, end_ms, text, token_count, text_hash, chunker_version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                chunk.chunk_id,
                transcript_version_id,
                video_id,
                channel_id,
                chunk.sequence,
                chunk.start_ms,
                chunk.end_ms,
                chunk.text,
                chunk.token_count,
                chunk.text_hash,
                CHUNKER_VERSION,
            )
            for chunk in chunks
        ],
    )
    connection.execute("UPDATE videos SET ingest_status = 'indexed', updated_at = datetime('now') WHERE video_id = ?", (video_id,))


def mark_video_failed(connection: sqlite3.Connection, *, video_id: str, error: str) -> None:
    connection.execute(
        """
        UPDATE videos
        SET ingest_status = ?, updated_at = datetime('now')
        WHERE video_id = ?
        """,
        (f"failed: {error[:200]}", video_id),
    )


def mark_video_deferred(connection: sqlite3.Connection, *, video_id: str, reason: str) -> None:
    connection.execute(
        """
        UPDATE videos
        SET ingest_status = ?, updated_at = datetime('now')
        WHERE video_id = ?
        """,
        (f"deferred: {reason[:200]}", video_id),
    )


def record_transcript_attempt(
    connection: sqlite3.Connection,
    *,
    video_id: str,
    tool: str,
    status: str,
    error_class: str | None = None,
    error: str | None = None,
    retryable: bool = False,
) -> None:
    connection.execute(
        """
        INSERT INTO transcript_attempts(video_id, tool, status, error_class, error, retryable)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            video_id,
            tool,
            status,
            error_class,
            error[:1000] if error else None,
            1 if retryable else 0,
        ),
    )


def rebuild_fts(connection: sqlite3.Connection) -> None:
    connection.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
