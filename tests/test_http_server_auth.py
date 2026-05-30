from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from yutome import http_server
from yutome.cli import app as cli_app
from yutome.cli import serve as serve_cli


@pytest.fixture(autouse=True)
def clear_http_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(http_server.TOKEN_ENV_VAR, raising=False)
    monkeypatch.delenv(http_server.CORS_ENV_VAR, raising=False)


def _stub_tool_list(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_tool_list(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"rows": [{"searchable_now": 1, "needs_attention": 0, "videos": 2, "chunks": 3}]}

    monkeypatch.setattr(http_server.contract, "tool_list", fake_tool_list)
    return calls


def test_configured_token_allows_authorized_requests_and_rejects_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(http_server.TOKEN_ENV_VAR, "test-secret")
    calls = _stub_tool_list(monkeypatch)
    client = TestClient(http_server.build_app())

    missing = client.post("/list", json={"entity": "status"})
    assert missing.status_code == 401
    assert missing.json()["detail"] == "missing bearer token"

    invalid = client.post("/list", json={"entity": "status"}, headers={"Authorization": "Bearer wrong"})
    assert invalid.status_code == 401
    assert invalid.json()["detail"] == "invalid bearer token"

    authorized = client.post("/list", json={"entity": "status"}, headers={"Authorization": "Bearer test-secret"})
    assert authorized.status_code == 200
    assert authorized.json()["rows"][0]["videos"] == 2
    assert calls == [
        {
            "entity": "status",
            "channel": None,
            "since": None,
            "until": None,
            "status": None,
            "source": None,
            "language": None,
            "selected": None,
            "order_by": None,
            "limit": 20,
            "offset": 0,
            "project": None,
        }
    ]


def test_missing_token_default_denies_protected_routes_but_keeps_health_open(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = _stub_tool_list(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=http_server.__name__):
        client = TestClient(http_server.build_app())

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["auth_required"] is True

    protected = client.post("/list", json={"entity": "status"})
    assert protected.status_code == 401
    assert http_server.TOKEN_ENV_VAR in protected.json()["detail"]
    assert "--insecure/--allow-no-auth" in protected.json()["detail"]
    assert calls == []
    assert any(http_server.TOKEN_ENV_VAR in record.message for record in caplog.records)


def test_insecure_no_auth_mode_allows_requests_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = _stub_tool_list(monkeypatch)
    with caplog.at_level(logging.WARNING, logger=http_server.__name__):
        client = TestClient(http_server.build_app(allow_no_auth=True))

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["auth_required"] is False

    protected = client.post("/list", json={"entity": "status"})
    assert protected.status_code == 200
    assert protected.json()["rows"][0]["chunks"] == 3
    assert len(calls) == 1
    assert any("INSECURE" in record.message for record in caplog.records)


def test_serve_http_insecure_flag_wires_to_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_http_serve(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(serve_cli.actions, "http_serve", fake_http_serve)

    result = CliRunner().invoke(
        cli_app,
        ["--config", str(tmp_path / "yutome.toml"), "serve", "http", "--insecure"],
    )

    assert result.exit_code == 0, result.output
    assert captured["insecure"] is True
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8765
