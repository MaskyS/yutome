from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from yutome.db import connect_catalog
from yutome.paths import ProjectPaths, resolve_under
from yutome.transcripts import TranscriptSegment, format_timestamp, read_normalized_segments

ExportMode = Literal["portable-md", "obsidian"]


@dataclass(frozen=True)
class ExportStats:
    exported: int
    output_dir: Path


@dataclass(frozen=True)
class ExportChunk:
    chunk_id: str
    sequence: int
    start_ms: int
    end_ms: int
    text: str


def export_markdown(*, paths: ProjectPaths, mode: ExportMode) -> ExportStats:
    output_dir = paths.portable_export_dir if mode == "portable-md" else paths.obsidian_export_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    with connect_catalog(paths.catalog_db) as connection:
        rows = connection.execute(
            """
            SELECT
                v.video_id,
                v.title,
                v.description,
                v.duration_seconds,
                v.published_at,
                v.thumbnail_url,
                c.title AS channel_title,
                c.handle AS channel_handle,
                tv.transcript_version_id,
                tv.source AS transcript_source,
                tv.language,
                tv.is_generated,
                tv.normalized_path
            FROM videos v
            JOIN transcript_versions tv
                ON tv.video_id = v.video_id
                AND tv.active = 1
            LEFT JOIN channels c ON c.channel_id = v.channel_id
            WHERE v.ingest_status = 'indexed'
            ORDER BY COALESCE(v.published_at, ''), v.video_id
            """
        ).fetchall()
        exported = 0
        used_names: set[str] = set()
        for row in rows:
            segments = read_normalized_segments(resolve_under(paths.root, Path(row["normalized_path"])))
            if not segments:
                continue
            if mode == "obsidian":
                chunks = _load_chunks(connection, row["video_id"])
                filename = _obsidian_filename(row["title"] or row["video_id"], row["video_id"], used_names)
                body = _render_obsidian_markdown(dict(row), segments, chunks)
            else:
                filename = _portable_filename(row["title"] or row["video_id"], row["video_id"])
                body = _render_portable_markdown(dict(row), segments)
            (output_dir / filename).write_text(body, encoding="utf-8")
            exported += 1
    return ExportStats(exported=exported, output_dir=output_dir)


def _load_chunks(connection: sqlite3.Connection, video_id: str) -> list[ExportChunk]:
    rows = connection.execute(
        """
        SELECT chunk_id, sequence, start_ms, end_ms, text
        FROM chunks
        WHERE video_id = ?
        ORDER BY sequence
        """,
        (video_id,),
    ).fetchall()
    return [
        ExportChunk(
            chunk_id=row["chunk_id"],
            sequence=row["sequence"],
            start_ms=row["start_ms"],
            end_ms=row["end_ms"],
            text=row["text"],
        )
        for row in rows
    ]


def _frontmatter(row: dict, youtube_url: str) -> dict:
    return {
        "title": row.get("title") or row["video_id"],
        "source": youtube_url,
        "video_id": row["video_id"],
        "channel": row.get("channel_title") or row.get("channel_handle"),
        "published": row.get("published_at"),
        "duration_seconds": row.get("duration_seconds"),
        "transcript_source": row.get("transcript_source"),
        "transcript_version_id": row.get("transcript_version_id"),
        "language": row.get("language"),
        "is_generated": bool(row.get("is_generated")),
        "tags": ["youtube", "yutome"],
    }


def _render_portable_markdown(row: dict, segments: list[TranscriptSegment]) -> str:
    video_id = row["video_id"]
    youtube_url = f"https://youtube.com/watch?v={video_id}"
    title = row.get("title") or video_id
    lines = [_yaml_frontmatter(_frontmatter(row, youtube_url)), f"# {title}", "", f"[Watch on YouTube]({youtube_url})", ""]
    if row.get("description"):
        lines.extend(["## Description", "", _truncate_description(row["description"]), ""])
    lines.extend(["## Transcript", ""])
    for segment in segments:
        seconds = segment.start_ms // 1000
        timestamp = format_timestamp(segment.start_ms)[:8]
        lines.append(f"- [{timestamp}](https://youtu.be/{video_id}?t={seconds}) {segment.text}")
    return "\n".join(lines).rstrip() + "\n"


def _render_obsidian_markdown(
    row: dict, segments: list[TranscriptSegment], chunks: list[ExportChunk]
) -> str:
    video_id = row["video_id"]
    youtube_url = f"https://youtube.com/watch?v={video_id}"
    title = row.get("title") or video_id
    lines = [
        _yaml_frontmatter(_frontmatter(row, youtube_url)),
        f"# {title}",
        "",
        f"[Watch on YouTube]({youtube_url})",
        "",
        f"![](https://www.youtube.com/watch?v={video_id})",
        "",
    ]
    if row.get("description"):
        lines.extend(["## Description", "", _truncate_description(row["description"]), ""])

    lines.extend(["## Transcript", ""])
    if chunks:
        for chunk in chunks:
            short_id = chunk.chunk_id[:8]
            lines.append(f"{chunk.text} ^chunk-{short_id}")
            lines.append("")
    else:
        # Fallback for older catalogs missing chunk rows: collapse segments into one paragraph.
        lines.append(" ".join(segment.text for segment in segments))
        lines.append("")

    if chunks:
        lines.extend(["## Timestamps", ""])
        for chunk in chunks:
            seconds = chunk.start_ms // 1000
            timestamp = format_timestamp(chunk.start_ms)[:8]
            preview = _preview_text(chunk.text, max_chars=80)
            lines.append(
                f"- [{timestamp}](https://youtu.be/{video_id}?t={seconds}) — {preview} [[#^chunk-{chunk.chunk_id[:8]}|→]]"
            )

    return "\n".join(lines).rstrip() + "\n"


def _preview_text(text: str, *, max_chars: int) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 1].rstrip() + "…"


def _yaml_frontmatter(values: dict) -> str:
    lines = ["---"]
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, int):
            lines.append(f"{key}: {value}")
        elif isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {_yaml_string(str(item))}")
        else:
            lines.append(f"{key}: {_yaml_string(str(value))}")
    lines.append("---")
    return "\n".join(lines)


def _yaml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _portable_filename(title: str, video_id: str) -> str:
    slug = _safe_slug(title)
    return f"{slug}-{video_id}.md"


def _obsidian_filename(title: str, video_id: str, used: set[str]) -> str:
    """Title-preserving filename for Obsidian. Only OS- and Obsidian-illegal chars are stripped."""
    base = _safe_obsidian_name(title) or f"video-{video_id}"
    candidate = f"{base}.md"
    if candidate not in used:
        used.add(candidate)
        return candidate
    # Collision: add a numeric suffix, then fall back to video_id.
    for n in range(2, 100):
        candidate = f"{base} ({n}).md"
        if candidate not in used:
            used.add(candidate)
            return candidate
    candidate = f"{base} {video_id}.md"
    used.add(candidate)
    return candidate


# Disallowed: OS-illegal on macOS/Windows plus Obsidian wikilink-reserved (#, ^, |, [, ]).
_OBSIDIAN_BAD_CHARS = re.compile(r'[<>:"/\\|?*#\^\[\]\x00-\x1f]')


def _safe_obsidian_name(value: str) -> str:
    cleaned = _OBSIDIAN_BAD_CHARS.sub("-", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().strip(".-")
    return cleaned[:120]


def _safe_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9 -]+", "", normalized)
    slug = re.sub(r"\s+", "-", slug.strip().lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return (slug or "video")[:90]


def _truncate_description(description: str, *, max_chars: int = 4000) -> str:
    compact = description.strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."
