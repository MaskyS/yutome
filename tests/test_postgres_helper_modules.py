from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from yutome.channels import channel_from_input, upsert_library_channel_sql
from yutome.chunking import Chunk
from yutome.exports import export_markdown
from yutome.paths import ProjectPaths
from yutome.sources import list_library_sources, source_from_input, upsert_library_source_sql
from yutome.store import upsert_transcript_and_chunks


@dataclass
class _Cursor:
    rows: list[dict[str, Any]]
    rowcount: int = 0

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _RecordingConnection:
    def __init__(self, results: list[list[dict[str, Any]]] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[tuple[str, Mapping[str, Any] | None]] = []
        self.commits = 0

    def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> _Cursor:
        self.calls.append((statement, params))
        rows = self.results.pop(0) if self.results else []
        return _Cursor(rows=rows, rowcount=len(rows))

    def commit(self) -> None:
        self.commits += 1


def test_channel_source_upsert_targets_hosted_sources_table() -> None:
    channel = channel_from_input("UCabc12345678901234567890", title="Example", import_source="manual")

    assert channel is not None
    statement = upsert_library_channel_sql(channel, workspace_id="ws_alice")

    assert "INSERT INTO sources" in statement.sql
    assert "library_channels" not in statement.sql
    assert "?" not in statement.sql
    assert statement.params["workspace_id"] == "ws_alice"
    assert statement.params["source_type"] == "channel"
    assert statement.params["canonical_channel_id"] == "UCabc12345678901234567890"


def test_library_sources_round_trip_postgres_rows() -> None:
    connection = _RecordingConnection(
        results=[
            [
                {
                    "source_id": "src_1",
                    "source_type": "video",
                    "source_url": "https://www.youtube.com/watch?v=UTuuTTnjxMQ",
                    "channel_id": None,
                    "playlist_id": None,
                    "video_id": "UTuuTTnjxMQ",
                    "title": "Video",
                    "selected": True,
                    "import_source": "manual",
                    "metadata_json": {"source": "youtube:video:UTuuTTnjxMQ"},
                }
            ]
        ]
    )

    sources = list_library_sources(connection, workspace_id="ws_alice")

    assert sources[0].source_type == "video"
    assert sources[0].video_id == "UTuuTTnjxMQ"
    assert connection.calls[0][1] == {"workspace_id": "ws_alice", "selected_only": False}


def test_source_upsert_uses_hosted_source_types() -> None:
    source = source_from_input("https://www.youtube.com/playlist?list=PLabc1234567890", title="Playlist")

    assert source is not None
    statement = upsert_library_source_sql(source, workspace_id="ws_alice")

    assert statement.params["source_type"] == "playlist"
    assert statement.params["canonical_playlist_id"] == "PLabc1234567890"
    assert "INSERT INTO sources" in statement.sql
    assert "library_sources" not in statement.sql


def test_transcript_chunk_write_targets_postgres_vectorchord_tables(tmp_path: Path) -> None:
    connection = _RecordingConnection()
    chunk = Chunk(
        chunk_id="old-local-chunk",
        sequence=0,
        start_ms=0,
        end_ms=1200,
        text="Postgres chunks should carry BM25 documents.",
        token_count=7,
        text_hash="hash",
        segment_ids=["seg_1"],
    )

    upsert_transcript_and_chunks(
        connection,
        workspace_id="ws_alice",
        transcript_version_id="tx_1",
        video_id="UTuuTTnjxMQ",
        channel_id="UCabc12345678901234567890",
        source="manual",
        language="en",
        is_generated=False,
        raw_path=tmp_path / "raw.json",
        normalized_path=tmp_path / "normalized.jsonl",
        text_hash="content_hash",
        segment_count=1,
        chunks=[chunk],
    )

    sql = "\n".join(call[0] for call in connection.calls)
    assert "INSERT INTO transcript_versions" in sql
    assert "INSERT INTO search_index_profiles" in sql
    assert "INSERT INTO chunks" in sql
    assert "tokenize(%(text)s, %(tokenizer)s)::bm25vector" in sql
    assert "chunks_fts" not in sql
    assert "?" not in sql


def test_markdown_export_reads_postgres_chunks(tmp_path: Path) -> None:
    paths = ProjectPaths(
        root=tmp_path,
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "data" / "artifacts",
        portable_export_dir=tmp_path / "data" / "exports" / "portable-md",
        obsidian_export_dir=tmp_path / "data" / "exports" / "obsidian",
        logs_dir=tmp_path / "data" / "logs",
    )
    connection = _RecordingConnection(
        results=[
            [
                {
                    "hosted_video_id": "vid_1",
                    "youtube_video_id": "UTuuTTnjxMQ",
                    "title": "Example Video",
                    "description": "Description",
                    "duration_seconds": 12,
                    "published_at": None,
                    "video_metadata": {"channel_title": "Example Channel"},
                    "channel_id": "UCabc12345678901234567890",
                    "source_display_name": "Example Channel",
                    "transcript_version_id": "tx_1",
                    "transcript_source": "manual",
                    "language": "en",
                    "transcript_metadata": {"is_generated": False},
                }
            ],
            [
                {
                    "chunk_id": "chk_1",
                    "chunk_index": 0,
                    "start_seconds": 0,
                    "end_seconds": 12,
                    "text": "Exported from Postgres.",
                }
            ],
        ]
    )

    stats = export_markdown(connection=connection, workspace_id="ws_alice", paths=paths, mode="portable-md")

    assert stats.exported == 1
    exported = next(paths.portable_export_dir.glob("*.md"))
    assert "Exported from Postgres." in exported.read_text(encoding="utf-8")
