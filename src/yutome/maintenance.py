from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from yutome.chunking import CHUNKER_VERSION, Chunk, build_chunks
from yutome.paths import ProjectPaths, resolve_under
from yutome.store import upsert_transcript_and_chunks
from yutome.transcripts import read_normalized_segments


class SqlConnection(Protocol):
    def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> Any:
        ...


@dataclass(frozen=True)
class RechunkStats:
    rebuilt_videos: int
    rebuilt_chunks: int
    skipped: int = 0


def rebuild_active_chunks(*, connection: SqlConnection, workspace_id: str, paths: ProjectPaths) -> RechunkStats:
    rebuilt_videos = 0
    rebuilt_chunks = 0
    skipped = 0
    rows = _rows_from_result(
        connection.execute(
            """
SELECT
    v.youtube_video_id AS video_id,
    v.channel_id,
    tv.id AS transcript_version_id,
    tv.source,
    tv.language_code,
    tv.content_hash,
    tv.metadata_json
FROM videos v
JOIN transcript_versions tv
  ON tv.id = v.active_transcript_version_id
 AND tv.workspace_id = v.workspace_id
WHERE v.workspace_id = %(workspace_id)s
ORDER BY v.youtube_video_id;
""".strip(),
            {"workspace_id": workspace_id},
        )
    )
    for row in rows:
        metadata = _metadata(row.get("metadata_json"))
        normalized_path_value = metadata.get("normalized_path")
        if not normalized_path_value:
            skipped += 1
            continue
        normalized_path = resolve_under(paths.root, Path(str(normalized_path_value)))
        if not normalized_path.exists():
            skipped += 1
            continue
        segments = read_normalized_segments(normalized_path)
        chunks = build_chunks(
            video_id=row["video_id"],
            transcript_version_id=row["transcript_version_id"],
            segments=segments,
        )
        _write_chunk_artifact(paths=paths, video_id=row["video_id"], chunks=chunks)
        upsert_transcript_and_chunks(
            connection,
            workspace_id=workspace_id,
            transcript_version_id=str(row["transcript_version_id"]),
            video_id=str(row["video_id"]),
            channel_id=str(row["channel_id"]) if row.get("channel_id") else None,
            source=str(row["source"]),
            language=str(row["language_code"]) if row.get("language_code") else None,
            is_generated=bool(metadata.get("is_generated")),
            raw_path=Path(str(metadata.get("raw_path") or "")),
            normalized_path=Path(str(normalized_path_value)),
            text_hash=str(row["content_hash"]),
            segment_count=len(segments),
            chunks=chunks,
        )
        rebuilt_videos += 1
        rebuilt_chunks += len(chunks)
    _commit_if_supported(connection)
    return RechunkStats(rebuilt_videos=rebuilt_videos, rebuilt_chunks=rebuilt_chunks, skipped=skipped)


def _write_chunk_artifact(*, paths: ProjectPaths, video_id: str, chunks: list[Chunk]) -> None:
    chunks_path = paths.chunks_path(video_id, CHUNKER_VERSION)
    chunks_path.parent.mkdir(parents=True, exist_ok=True)
    with chunks_path.open("w", encoding="utf-8") as chunks_file:
        for chunk in chunks:
            chunks_file.write(
                json.dumps(
                    {
                        "chunk_id": chunk.chunk_id,
                        "sequence": chunk.sequence,
                        "start_ms": chunk.start_ms,
                        "end_ms": chunk.end_ms,
                        "text": chunk.text,
                        "token_count": chunk.token_count,
                        "text_hash": chunk.text_hash,
                        "segment_ids": chunk.segment_ids,
                        "forced_split": chunk.forced_split,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )


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


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        return json.loads(value)
    return {}


def _commit_if_supported(connection: SqlConnection) -> None:
    commit = getattr(connection, "commit", None)
    if callable(commit):
        commit()
