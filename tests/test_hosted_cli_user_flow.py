from __future__ import annotations

import json
import stat
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from yutome.channels import channel_from_input, list_library_channels
from yutome.cli import app
from yutome.config import write_default_config
from yutome.db import connect_catalog


def test_hosted_login_fake_browser_callback_stores_auth(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    seen: dict[str, Any] = {}

    def fake_open(url: str) -> bool:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query)
        redirect_uri = query["redirect_uri"][0]
        state = query["state"][0]
        seen["authorize_url"] = url

        def callback() -> None:
            time.sleep(0.05)
            callback_url = redirect_uri + "?" + urllib.parse.urlencode({"code": "code_123", "state": state})
            with urllib.request.urlopen(callback_url, timeout=5) as response:
                response.read()

        threading.Thread(target=callback, daemon=True).start()
        return True

    def fake_api(api_base: str, path: str, **kwargs):  # noqa: ANN001, ANN003
        seen["api_base"] = api_base
        seen["path"] = path
        seen["body"] = kwargs["body"]
        return {
            "ok": True,
            "access_token": "hosted-token",
            "token_type": "Bearer",
            "expires_at": "2026-06-01T00:00:00+00:00",
            "workspace_id": "ws_cli_test",
            "grant_id": "grant_cli_test",
            "scopes": ["yutome.source.write"],
        }

    monkeypatch.setattr("webbrowser.open", fake_open)
    monkeypatch.setattr("yutome.cli._legacy._hosted_api_request_json", fake_api)

    result = runner.invoke(app, ["--config", str(config_path), "hosted", "login"])

    assert result.exit_code == 0, result.output
    assert "Hosted CLI connected to workspace ws_cli_test" in result.output
    assert seen["path"] == "/account/cli/token"
    assert seen["body"]["code"] == "code_123"
    auth_path = tmp_path / "data/auth/yutome-hosted-cli.json"
    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    assert auth["access_token"] == "hosted-token"
    assert auth["workspace_id"] == "ws_cli_test"
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600
    config_text = config_path.read_text(encoding="utf-8")
    assert 'enabled = true' in config_text
    assert 'workspace_id = "ws_cli_test"' in config_text


def test_import_youtube_hosted_uploads_public_channel_rows(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    captured: dict[str, Any] = {}

    def fake_fetch(**_kwargs):  # noqa: ANN003
        return [
            channel_from_input(
                "UC9999999999999999999999",
                title="Hosted OAuth Channel",
                import_source="youtube_oauth",
            )
        ]

    def fake_import(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return {"ok": True, "imported": [{"source_id": "src_1"}], "jobs": [], "refresh_policies": [{"id": "srp_1"}]}

    monkeypatch.setattr("yutome.cli._legacy.fetch_user_subscription_channels_from_browser", fake_fetch)
    monkeypatch.setattr("yutome.cli._legacy._hosted_import_sources", fake_import)

    result = runner.invoke(app, ["--config", str(config_path), "corpus", "import-youtube", "--hosted"])

    assert result.exit_code == 0, result.output
    assert "Uploaded 1 YouTube subscription channel" in result.output
    descriptor = captured["descriptors"][0]
    assert descriptor["source_type"] == "channel"
    assert descriptor["channel_id"] == "UC9999999999999999999999"
    assert descriptor["import_source"] == "youtube_oauth"
    with connect_catalog(tmp_path / "data/indexes/catalog.sqlite") as connection:
        assert list_library_channels(connection) == []
