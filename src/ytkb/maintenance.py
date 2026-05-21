from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ytkb.chunking import CHUNKER_VERSION, Chunk, build_chunks
from ytkb.db import connect_catalog
from ytkb.paths import ProjectPaths, resolve_under
from ytkb.store import rebuild_fts
from ytkb.transcripts import read_normalized_segments


@dataclass(frozen=True)
class RechunkStats:
    rebuilt_videos: int
    rebuilt_chunks: int
    skipped: int = 0


def rebuild_active_chunks(*, paths: ProjectPaths) -> RechunkStats:
    rebuilt_videos = 0
    rebuilt_chunks = 0
    skipped = 0
    with connect_catalog(paths.catalog_db) as connection:
        rows = connection.execute(
            """
            SELECT
                v.video_id,
                v.channel_id,
                tv.transcript_version_id,
                tv.normalized_path
            FROM transcript_versions tv
            JOIN videos v ON v.video_id = tv.video_id
            WHERE tv.active = 1
            ORDER BY v.video_id
            """
        ).fetchall()
        for row in rows:
            normalized_path = resolve_under(paths.root, Path(row["normalized_path"]))
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
            connection.execute("DELETE FROM chunks WHERE video_id = ?", (row["video_id"],))
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
                        row["transcript_version_id"],
                        row["video_id"],
                        row["channel_id"],
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
            rebuilt_videos += 1
            rebuilt_chunks += len(chunks)
        rebuild_fts(connection)
        connection.commit()
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
