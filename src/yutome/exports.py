from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol

from yutome.paths import ProjectPaths
from yutome.transcripts import format_timestamp

ExportMode = Literal["portable-md", "obsidian"]


class SqlConnection(Protocol):
    def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> Any:
        ...


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


def export_markdown(*, connection: SqlConnection, workspace_id: str, paths: ProjectPaths, mode: ExportMode) -> ExportStats:
    output_dir = paths.portable_export_dir if mode == "portable-md" else paths.obsidian_export_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = _rows_from_result(
        connection.execute(
            """
SELECT
    v.id AS hosted_video_id,
    v.youtube_video_id,
    v.title,
    v.description,
    v.duration_seconds,
    v.published_at,
    v.metadata_json AS video_metadata,
    v.channel_id,
    s.display_name AS source_display_name,
    tv.id AS transcript_version_id,
    tv.source AS transcript_source,
    tv.language_code AS language,
    tv.metadata_json AS transcript_metadata
FROM videos v
JOIN transcript_versions tv
  ON tv.id = v.active_transcript_version_id
 AND tv.workspace_id = v.workspace_id
LEFT JOIN sources s ON s.id = v.source_id AND s.workspace_id = v.workspace_id
WHERE v.workspace_id = %(workspace_id)s
  AND v.active_transcript_version_id IS NOT NULL
ORDER BY v.published_at ASC NULLS LAST, v.youtube_video_id;
""".strip(),
            {"workspace_id": workspace_id},
        )
    )
    exported = 0
    used_names: set[str] = set()
    for row in rows:
        chunks = _load_chunks(connection, workspace_id=workspace_id, transcript_version_id=str(row["transcript_version_id"]))
        if not chunks:
            continue
        export_row = _export_row(row)
        if mode == "obsidian":
            filename = _obsidian_filename(export_row["title"] or export_row["video_id"], export_row["video_id"], used_names)
            body = _render_obsidian_markdown(export_row, chunks)
        else:
            filename = _portable_filename(export_row["title"] or export_row["video_id"], export_row["video_id"])
            body = _render_portable_markdown(export_row, chunks)
        (output_dir / filename).write_text(body, encoding="utf-8")
        exported += 1
    return ExportStats(exported=exported, output_dir=output_dir)


def _load_chunks(connection: SqlConnection, *, workspace_id: str, transcript_version_id: str) -> list[ExportChunk]:
    rows = _rows_from_result(
        connection.execute(
            """
SELECT id AS chunk_id, chunk_index, start_seconds, end_seconds, text
FROM chunks
WHERE workspace_id = %(workspace_id)s
  AND transcript_version_id = %(transcript_version_id)s
ORDER BY chunk_index;
""".strip(),
            {"workspace_id": workspace_id, "transcript_version_id": transcript_version_id},
        )
    )
    return [
        ExportChunk(
            chunk_id=row["chunk_id"],
            sequence=int(row["chunk_index"]),
            start_ms=_seconds_to_ms(row.get("start_seconds")),
            end_ms=_seconds_to_ms(row.get("end_seconds")),
            text=row["text"],
        )
        for row in rows
    ]


def _export_row(row: Mapping[str, Any]) -> dict[str, Any]:
    video_metadata = _json_value(row.get("video_metadata"))
    transcript_metadata = _json_value(row.get("transcript_metadata"))
    return {
        "video_id": row.get("youtube_video_id") or row["hosted_video_id"],
        "hosted_video_id": row["hosted_video_id"],
        "title": row.get("title"),
        "description": row.get("description"),
        "duration_seconds": row.get("duration_seconds"),
        "published_at": _json_scalar(row.get("published_at")),
        "thumbnail_url": video_metadata.get("thumbnail_url"),
        "channel_title": video_metadata.get("channel_title") or row.get("source_display_name"),
        "channel_handle": video_metadata.get("channel_handle"),
        "channel_id": row.get("channel_id"),
        "transcript_version_id": row.get("transcript_version_id"),
        "transcript_source": row.get("transcript_source"),
        "language": row.get("language"),
        "is_generated": bool(transcript_metadata.get("is_generated")),
    }


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


def _render_portable_markdown(row: dict, chunks: list[ExportChunk]) -> str:
    video_id = row["video_id"]
    youtube_url = f"https://youtube.com/watch?v={video_id}"
    title = row.get("title") or video_id
    lines = [_yaml_frontmatter(_frontmatter(row, youtube_url)), f"# {title}", "", f"[Watch on YouTube]({youtube_url})", ""]
    if row.get("description"):
        lines.extend(["## Description", "", _truncate_description(row["description"]), ""])
    lines.extend(["## Transcript", ""])
    for chunk in chunks:
        seconds = chunk.start_ms // 1000
        timestamp = format_timestamp(chunk.start_ms)[:8]
        lines.append(f"- [{timestamp}](https://youtu.be/{video_id}?t={seconds}) {chunk.text}")
    return "\n".join(lines).rstrip() + "\n"


def _render_obsidian_markdown(row: dict, chunks: list[ExportChunk]) -> str:
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
    for chunk in chunks:
        short_id = chunk.chunk_id[:8]
        lines.append(f"{chunk.text} ^chunk-{short_id}")
        lines.append("")

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


def _seconds_to_ms(value: Any) -> int:
    if value is None:
        return 0
    return int(round(float(value) * 1000))


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


def _json_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        import json

        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_scalar(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
