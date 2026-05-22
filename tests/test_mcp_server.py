"""Tests for the local MCP server adapters.

Two kinds of coverage:

* In-process tests against an isolated fixture corpus (always run) — verify the
  pure-Python tool/resource handlers wire to the catalog and return shapes that
  match the query verb contract.
* Live-corpus tests against `data/indexes/catalog.sqlite` if it exists
  (skipped on a fresh checkout) — confirm tools still work against the real
  indexed Leo and Longevity data.
"""
from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from yutome.config import default_config
from yutome.db import bootstrap_catalog, connect_catalog
from yutome.paths import ProjectPaths
from yutome.store import rebuild_fts
import yutome.mcp_server as mcp_server


def _fixture_project(tmp_path: Path) -> ProjectPaths:
    """Build a tiny indexed corpus on disk so MCP handlers have data to query."""
    config = default_config()
    paths = ProjectPaths.from_config(config, project_root=tmp_path)
    paths.ensure_base_dirs()
    bootstrap_catalog(paths.catalog_db)

    transcript_dir = paths.transcript_dir("vid123", "tx123")
    transcript_dir.mkdir(parents=True, exist_ok=True)
    normalized_path = transcript_dir / "normalized.jsonl"
    segments = [
        {"segment_id": "s1", "sequence": 0, "start_ms": 0, "end_ms": 4000, "text": "Crohn probiotics intro"},
        {"segment_id": "s2", "sequence": 1, "start_ms": 4000, "end_ms": 8000, "text": "lentils and salads context"},
    ]
    normalized_path.write_text(
        "\n".join(json.dumps(segment) for segment in segments) + "\n",
        encoding="utf-8",
    )
    (transcript_dir / "transcript.txt").write_text(
        "Crohn probiotics intro\nlentils and salads context\n",
        encoding="utf-8",
    )

    with connect_catalog(paths.catalog_db) as connection:
        connection.execute(
            """
            INSERT INTO channels(channel_id, handle, source_url, title)
            VALUES ('chan1', '@example', 'https://www.youtube.com/@example', 'Example Channel')
            """
        )
        connection.execute(
            """
            INSERT INTO videos(video_id, channel_id, title, duration_seconds, ingest_status)
            VALUES ('vid123', 'chan1', 'Crohn talk', 600, 'indexed')
            """
        )
        connection.execute(
            """
            INSERT INTO transcript_versions(
                transcript_version_id, video_id, source, language, is_generated,
                raw_path, normalized_path, text_hash, segment_count, active
            )
            VALUES ('tx123', 'vid123', 'youtube-transcript-api', 'en', 1,
                    ?, ?, 'hash123', 2, 1)
            """,
            (str(normalized_path), str(normalized_path)),
        )
        connection.execute(
            """
            INSERT INTO library_channels(library_channel_id, source, source_url, channel_id, handle, title, selected)
            VALUES ('lib123', 'chan1', 'https://www.youtube.com/@example', 'chan1', '@example', 'Example Channel', 1)
            """
        )
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
                    "chunk-a",
                    "tx123",
                    "vid123",
                    "chan1",
                    0,
                    0,
                    4000,
                    "Crohn probiotics intro paragraph " * 12,
                    150,
                    "hash-a",
                    "timestamp-aware-v2",
                ),
                (
                    "chunk-b",
                    "tx123",
                    "vid123",
                    "chan1",
                    1,
                    4000,
                    8000,
                    "lentils and salads context paragraph " * 12,
                    150,
                    "hash-b",
                    "timestamp-aware-v2",
                ),
            ],
        )
        rebuild_fts(connection)
        connection.commit()

    return paths


@pytest.fixture
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectPaths:
    paths = _fixture_project(tmp_path)
    config_path = tmp_path / "yutome.toml"
    config_path.write_text("[storage]\ndata_dir = 'data'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    mcp_server._RUNTIME = None
    mcp_server.configure(Path("yutome.toml"))
    yield paths
    mcp_server._RUNTIME = None


def test_search_lexical_returns_thin_hits(configured: ProjectPaths) -> None:
    result = mcp_server.tool_find(text="Crohn", mode="lexical", limit=5)
    hits = result["rows"]
    assert hits, "lexical search should find the Crohn chunk"
    hit = hits[0]
    for field in ("chunk_id", "resource_uri", "video_id", "youtube_url", "start_ms", "snippet"):
        assert field in hit
    assert hit["resource_uri"].startswith("yutome://chunk/")
    assert hit["youtube_url"].startswith("https://youtube.com/watch?v=")
    assert "text" not in hit, "thin hits must not include full chunk text"


def test_context_expands_anchor(configured: ProjectPaths) -> None:
    hits = mcp_server.tool_find(text="Crohn", mode="lexical", limit=5)["rows"]
    chunk_id = hits[0]["chunk_id"]
    result = mcp_server.tool_show(kind="context", id_=chunk_id, token_budget=1000)
    assert result["anchor"]["chunk_id"] == chunk_id
    assert result["estimated_tokens"] >= 0
    assert result["text"], "merged context text must be present"
    assert result["citations"], "citations must be present"
    assert all("youtube_url" in c for c in result["citations"])


def test_context_rejects_empty_anchor(configured: ProjectPaths) -> None:
    with pytest.raises(ValueError):
        mcp_server.tool_show(kind="context")


def test_list_channels_returns_library(configured: ProjectPaths) -> None:
    channels = mcp_server.tool_list(entity="channels")["rows"]
    assert any(c["library_channel_id"] == "lib123" for c in channels)
    selected = mcp_server.tool_list(entity="channels", selected=True)["rows"]
    assert all(c["selected"] is True for c in selected)


def test_corpus_status_shape(configured: ProjectPaths) -> None:
    status = mcp_server.tool_list(entity="status")["rows"][0]
    for field in (
        "searchable_now",
        "still_indexing",
        "needs_attention",
        "channels",
        "videos",
        "chunks",
        "transcript_versions",
        "statuses",
    ):
        assert field in status
    assert status["searchable_now"] == 1  # one indexed fixture video
    assert status["videos"] == 1


def test_open_source_by_chunk_id(configured: ProjectPaths) -> None:
    hits = mcp_server.tool_find(text="Crohn", mode="lexical", limit=1)["rows"]
    resolved = mcp_server.tool_show(kind="source", id_=hits[0]["chunk_id"])
    assert resolved["video_id"] == "vid123"
    assert resolved["youtube_url"].startswith("https://youtube.com/watch?v=vid123&t=")


def test_open_source_by_video_time(configured: ProjectPaths) -> None:
    resolved = mcp_server.tool_show(kind="source", video_id="vid123", time_seconds=1)
    assert resolved["video_id"] == "vid123"


def test_open_source_requires_anchor(configured: ProjectPaths) -> None:
    with pytest.raises(ValueError):
        mcp_server.tool_show(kind="source")


def test_chunk_resource_returns_full_text(configured: ProjectPaths) -> None:
    hits = mcp_server.tool_find(text="Crohn", mode="lexical", limit=1)["rows"]
    chunk_id = hits[0]["chunk_id"]
    payload = mcp_server.resource_chunk(chunk_id)
    assert payload["chunk_id"] == chunk_id
    assert payload["text"], "chunk resource must include full text"
    assert payload["resource_uri"] == f"yutome://chunk/{chunk_id}"


def test_video_resource_includes_active_transcript(configured: ProjectPaths) -> None:
    payload = mcp_server.resource_video("vid123")
    assert payload["video_id"] == "vid123"
    assert payload["active_transcript"]
    assert payload["active_transcript"]["transcript_version_id"] == "tx123"


def test_transcript_resource_returns_text(configured: ProjectPaths) -> None:
    payload = mcp_server.resource_transcript("tx123")
    assert payload["transcript_version_id"] == "tx123"
    assert "Crohn" in payload["text"]
    assert payload["text_truncated"] is False


def test_show_transcript_accepts_video_id_and_pages_segments(configured: ProjectPaths) -> None:
    payload = mcp_server.tool_show(
        kind="transcript",
        id_="vid123",
        transcript_offset=1,
        transcript_limit=1,
    )
    assert payload["transcript_version_id"] == "tx123"
    assert payload["video_id"] == "vid123"
    assert payload["returned_segments"] == 1
    assert payload["next_offset"] is None
    assert "lentils and salads context" in payload["text"]
    assert "Crohn probiotics intro" not in payload["text"]


def test_build_server_registers_tools_and_resources(configured: ProjectPaths) -> None:
    server = mcp_server.build_server()
    # FastMCP exposes registered tools/resources via these methods (sync wrappers
    # call into the manager). We tolerate either iterable-of-objects or a dict.
    tools = server._tool_manager.list_tools()
    tool_names = {t.name for t in tools}
    assert {"find", "list", "show", "q"} <= tool_names

    templates = server._resource_manager.list_templates()
    template_uris = {t.uri_template for t in templates}
    assert {
        "yutome://chunk/{chunk_id}",
        "yutome://video/{video_id}",
        "yutome://channel/{channel_id}",
        "yutome://transcript/{transcript_version_id}",
    } <= template_uris


def test_build_server_can_enable_remote_bearer_auth(configured: ProjectPaths) -> None:
    server = mcp_server.build_server(
        host="127.0.0.1",
        port=8766,
        auth_token="secret-abc123",
        auth_base_url="http://127.0.0.1:8766",
    )

    assert server.settings.auth is not None
    assert server.settings.streamable_http_path == "/mcp"
    access = anyio.run(server._token_verifier.verify_token, "secret-abc123")
    rejected = anyio.run(server._token_verifier.verify_token, "wrong")
    assert access is not None
    assert access.scopes == ["yutome.search.read"]
    assert rejected is None


def test_remote_mcp_refuses_non_loopback_without_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixture_project(tmp_path)
    config_path = tmp_path / "yutome.toml"
    config_path.write_text("[storage]\ndata_dir = 'data'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("YUTOME_HTTP_TOKEN", raising=False)
    mcp_server._RUNTIME = None

    with pytest.raises(RuntimeError, match="YUTOME_HTTP_TOKEN is required"):
        mcp_server.run_streamable_http_server(
            config_path=Path("yutome.toml"),
            host="0.0.0.0",
            port=8766,
        )

    mcp_server._RUNTIME = None


# ---------------------------------------------------------------------------
# Optional smoke against the real Leo corpus, if present.
# ---------------------------------------------------------------------------


_LIVE_CATALOG = Path("data/indexes/catalog.sqlite")


@pytest.mark.skipif(not _LIVE_CATALOG.exists(), reason="live corpus not present")
def test_live_corpus_search_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)
    mcp_server._RUNTIME = None
    try:
        mcp_server.configure(Path("yutome.toml"))
        hits = mcp_server.tool_find(text="Crohn probiotics", mode="lexical", limit=3)["rows"]
        assert hits, "live lexical search returned no hits"
        for hit in hits:
            assert hit["chunk_id"]
            assert hit["youtube_url"].startswith("https://youtube.com/watch?v=")
        status = mcp_server.tool_list(entity="status")["rows"][0]
        assert status["searchable_now"] >= 100
    finally:
        mcp_server._RUNTIME = None
