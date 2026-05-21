"""Tests for the local HTTP API.

Reuses the fixture corpus from `test_mcp_server.py` to avoid duplicating setup.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from test_mcp_server import _fixture_project
import ytkb.mcp_server as mcp_server


@pytest.fixture
def http_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _fixture_project(tmp_path)
    config_path = tmp_path / "ytkb.toml"
    config_path.write_text("[storage]\ndata_dir = 'data'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    mcp_server._RUNTIME = None
    mcp_server.configure(Path("ytkb.toml"))

    from fastapi.testclient import TestClient
    from ytkb.http_server import build_app

    monkeypatch.delenv("YTKB_HTTP_TOKEN", raising=False)
    app = build_app()
    yield TestClient(app)
    mcp_server._RUNTIME = None


def test_healthz(http_client) -> None:  # noqa: ANN001
    response = http_client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["auth_required"] is False


def test_status(http_client) -> None:  # noqa: ANN001
    response = http_client.post("/list", json={"entity": "status"})
    assert response.status_code == 200
    body = response.json()["rows"][0]
    assert body["searchable_now"] == 1
    assert body["chunks"] >= 2


def test_search(http_client) -> None:  # noqa: ANN001
    response = http_client.post(
        "/find",
        json={"text": "Crohn", "mode": "lexical", "limit": 5},
    )
    assert response.status_code == 200
    hits = response.json()["rows"]
    assert hits, "expected at least one hit"
    first = hits[0]
    assert "chunk_id" in first
    assert first["resource_uri"].startswith("ytkb://chunk/")
    assert first["youtube_url"].startswith("https://youtube.com/watch?v=")
    assert "text" not in first


def test_context_round_trip(http_client) -> None:  # noqa: ANN001
    hits = http_client.post(
        "/find", json={"text": "Crohn", "mode": "lexical", "limit": 1}
    ).json()["rows"]
    chunk_id = hits[0]["chunk_id"]
    response = http_client.post(
        "/show", json={"kind": "context", "id": chunk_id, "token_budget": 1000}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["anchor"]["chunk_id"] == chunk_id
    assert body["text"]


def test_context_validation_error(http_client) -> None:  # noqa: ANN001
    response = http_client.post("/show", json={"kind": "context"})
    assert response.status_code == 400
    assert "Provide" in response.json()["detail"]


def test_channels_endpoint(http_client) -> None:  # noqa: ANN001
    response = http_client.post("/list", json={"entity": "channels"})
    assert response.status_code == 200
    body = response.json()["rows"]
    assert any(c["library_channel_id"] == "lib123" for c in body)


def test_chunk_resource(http_client) -> None:  # noqa: ANN001
    hits = http_client.post(
        "/find", json={"text": "Crohn", "mode": "lexical", "limit": 1}
    ).json()["rows"]
    chunk_id = hits[0]["chunk_id"]
    response = http_client.get(f"/chunks/{chunk_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["chunk_id"] == chunk_id
    assert body["text"]


def test_chunk_resource_missing(http_client) -> None:  # noqa: ANN001
    response = http_client.get("/chunks/does-not-exist")
    assert response.status_code == 404


def test_video_resource(http_client) -> None:  # noqa: ANN001
    response = http_client.get("/videos/vid123")
    assert response.status_code == 200
    body = response.json()
    assert body["active_transcript"]["transcript_version_id"] == "tx123"


def test_transcript_resource(http_client) -> None:  # noqa: ANN001
    response = http_client.get("/transcripts/tx123")
    assert response.status_code == 200
    body = response.json()
    assert "Crohn" in body["text"]


def test_open_source_by_video_time(http_client) -> None:  # noqa: ANN001
    response = http_client.post(
        "/show", json={"kind": "source", "video_id": "vid123", "time_seconds": 1}
    )
    assert response.status_code == 200
    assert response.json()["video_id"] == "vid123"


def test_open_source_missing(http_client) -> None:  # noqa: ANN001
    response = http_client.post("/show", json={"kind": "source", "id": "nope"})
    assert response.status_code == 404


@pytest.fixture
def http_client_with_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _fixture_project(tmp_path)
    config_path = tmp_path / "ytkb.toml"
    config_path.write_text("[storage]\ndata_dir = 'data'\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    mcp_server._RUNTIME = None
    mcp_server.configure(Path("ytkb.toml"))

    from fastapi.testclient import TestClient
    from ytkb.http_server import build_app

    monkeypatch.setenv("YTKB_HTTP_TOKEN", "secret-abc123")
    app = build_app()
    yield TestClient(app), "secret-abc123"
    mcp_server._RUNTIME = None


def test_auth_rejects_missing_token(http_client_with_token) -> None:  # noqa: ANN001
    client, _ = http_client_with_token
    response = client.post("/list", json={"entity": "status"})
    assert response.status_code == 401


def test_auth_rejects_wrong_token(http_client_with_token) -> None:  # noqa: ANN001
    client, _ = http_client_with_token
    response = client.post("/list", json={"entity": "status"}, headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_auth_accepts_correct_token(http_client_with_token) -> None:  # noqa: ANN001
    client, token = http_client_with_token
    response = client.post("/list", json={"entity": "status"}, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_healthz_skips_auth(http_client_with_token) -> None:  # noqa: ANN001
    client, _ = http_client_with_token
    response = client.get("/healthz")
    # /healthz is intentionally not gated so liveness checks work without a token.
    assert response.status_code == 200
    assert response.json()["auth_required"] is True
