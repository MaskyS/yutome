from __future__ import annotations

import re
import sqlite3
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

RetrieveDetail = Literal["thin", "chunk", "metadata"]

SNIPPET_CHARS = 360


def parse_youtube_location(url: str) -> tuple[str | None, int | None]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    video_id = None
    if parsed.netloc.endswith("youtu.be"):
        video_id = parsed.path.strip("/").split("/")[0] or None
    elif "v" in query:
        video_id = query["v"][0]
    time_value = query.get("t", [None])[0] or query.get("start", [None])[0]
    return video_id, _parse_time_seconds(time_value)


def _chunk_by_id(connection: sqlite3.Connection, chunk_id: str) -> dict[str, Any] | None:
    row = connection.execute(_CHUNK_SELECT + " WHERE c.chunk_id = ?", (chunk_id,)).fetchone()
    return None if row is None else _row_dict(row, match_type="context")


def _chunk_by_video_time(connection: sqlite3.Connection, *, video_id: str, time_ms: int) -> dict[str, Any] | None:
    row = connection.execute(
        _CHUNK_SELECT
        + """
        WHERE c.video_id = ?
          AND tv.active = 1
          AND c.start_ms <= ?
          AND c.end_ms >= ?
        ORDER BY ABS(c.start_ms - ?)
        LIMIT 1
        """,
        (video_id, time_ms, time_ms, time_ms),
    ).fetchone()
    if row is not None:
        return _row_dict(row, match_type="context")
    row = connection.execute(
        _CHUNK_SELECT
        + """
        WHERE c.video_id = ?
          AND tv.active = 1
        ORDER BY ABS(c.start_ms - ?)
        LIMIT 1
        """,
        (video_id, time_ms),
    ).fetchone()
    return None if row is None else _row_dict(row, match_type="context")


def _neighbor_chunks(
    connection: sqlite3.Connection,
    *,
    anchor: dict[str, Any],
    token_budget: int,
) -> list[dict[str, Any]]:
    rows = [
        _row_dict(row, match_type="context")
        for row in connection.execute(
            _CHUNK_SELECT
            + """
            WHERE c.transcript_version_id = ?
            ORDER BY c.sequence
            """,
            (anchor["transcript_version_id"],),
        ).fetchall()
    ]
    anchor_index = next(index for index, row in enumerate(rows) if row["chunk_id"] == anchor["chunk_id"])
    selected = {anchor_index}
    total = int(rows[anchor_index]["token_count"] or 0)
    left = anchor_index - 1
    right = anchor_index + 1
    while left >= 0 or right < len(rows):
        added = False
        for index in (left, right):
            if index < 0 or index >= len(rows) or index in selected:
                continue
            candidate_tokens = int(rows[index]["token_count"] or 0)
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


def _format_hit(
    row: dict[str, Any],
    *,
    detail: RetrieveDetail,
    include_description: bool,
) -> dict[str, Any]:
    hit = {
        "chunk_id": row["chunk_id"],
        "resource_uri": f"ytkb://chunk/{row['chunk_id']}",
        "video_id": row["video_id"],
        "title": row.get("title"),
        "youtube_url": _youtube_url(row["video_id"], row["start_ms"]),
        "start_ms": row["start_ms"],
        "end_ms": row["end_ms"],
        "snippet": row.get("snippet") or _snippet(row.get("text", "")),
        "transcript_version_id": row.get("transcript_version_id"),
        "transcript_source": row.get("transcript_source"),
        "language": row.get("language"),
        "is_generated": bool(row.get("is_generated")),
        "token_count": row.get("token_count"),
        "match_type": row.get("match_type"),
        "scores": {
            key: row.get(key)
            for key in ("lexical_score", "vector_score", "hybrid_score")
            if row.get(key) is not None
        },
    }
    if row.get("score") is not None:
        hit["score"] = row.get("score")
    if detail == "chunk":
        hit["text"] = row.get("text", "")
    if detail == "metadata":
        hit.update(
            {
                "published_at": row.get("published_at"),
                "duration_seconds": row.get("duration_seconds"),
                "channel_id": row.get("channel_id"),
                "sequence": row.get("sequence"),
                "chunker_version": row.get("chunker_version"),
                "text_hash": row.get("text_hash"),
                "thumbnail_url": row.get("thumbnail_url"),
                "live_status": row.get("live_status"),
                "metadata_hash": row.get("metadata_hash"),
                "ingest_status": row.get("ingest_status"),
            }
        )
    if include_description:
        hit["description"] = row.get("description")
    return hit


def _metadata_by_video(connection: sqlite3.Connection, video_ids: list[str]) -> dict[str, dict[str, Any]]:
    unique = sorted(set(video_ids))
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    rows = connection.execute(
        f"""
        SELECT video_id, title, description, duration_seconds, published_at
        FROM videos
        WHERE video_id IN ({placeholders})
        """,
        unique,
    ).fetchall()
    return {row["video_id"]: dict(row) for row in rows}


def _row_dict(row: sqlite3.Row, *, match_type: str) -> dict[str, Any]:
    data = dict(row)
    data["match_type"] = match_type
    data.setdefault("snippet", _snippet(data.get("text", "")))
    return data


def _snippet(text: str, *, max_chars: int = SNIPPET_CHARS) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def _youtube_url(video_id: str, start_ms: int) -> str:
    return f"https://youtube.com/watch?v={video_id}&t={int(start_ms // 1000)}s"


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


_CHUNK_SELECT = """
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
        tv.source AS transcript_source,
        tv.language,
        tv.is_generated,
        NULL AS snippet,
        NULL AS lexical_score
    FROM chunks c
    JOIN transcript_versions tv ON tv.transcript_version_id = c.transcript_version_id
    LEFT JOIN videos v ON v.video_id = c.video_id
"""
