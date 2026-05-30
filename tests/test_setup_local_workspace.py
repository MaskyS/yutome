from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yutome import api
from yutome.cli import actions
from yutome.cli import app
from yutome.config import AppConfig, HostedConfig, load_config, write_default_config

WS_RE = re.compile(r"^ws_[0-9a-f]{24}$")


def test_default_config_round_trips_local_workspace_id(tmp_path: Path) -> None:
    # Regression guard: the written default config must load back cleanly under
    # extra="forbid" (previously DEFAULT_CONFIG_TOML carried backfill keys the model rejected).
    cfg = tmp_path / "yutome.toml"
    write_default_config(cfg)
    loaded = load_config(cfg)
    assert loaded.hosted.local_workspace_id == ""
    assert AppConfig().hosted.local_workspace_id == ""


def test_actions_workspace_id_precedence() -> None:
    explicit = AppConfig(hosted=HostedConfig(workspace_id="ws_personal", local_workspace_id="ws_local"))
    assert actions._workspace_id(explicit, explicit="ws_flag") == "ws_flag"
    assert actions._workspace_id(explicit) == "ws_personal"
    local_only = AppConfig(hosted=HostedConfig(local_workspace_id="ws_local"))
    assert actions._workspace_id(local_only) == "ws_local"
    with pytest.raises(actions.HostedRuntimeError):
        actions._workspace_id(AppConfig())


def test_api_workspace_id_precedence() -> None:
    assert api._workspace_id(AppConfig(hosted=HostedConfig(workspace_id="ws_personal"))) == "ws_personal"
    assert api._workspace_id(AppConfig(hosted=HostedConfig(local_workspace_id="ws_local"))) == "ws_local"
    with pytest.raises(ValueError):
        api._workspace_id(AppConfig())


def test_set_env_var_creates_updates_and_preserves(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    actions._set_env_var(env_path, "VOYAGE_API_KEY", "v1")
    assert env_path.read_text(encoding="utf-8") == "VOYAGE_API_KEY=v1\n"
    # An unrelated line is preserved; the existing key is updated in place (no duplicate).
    actions._set_env_var(env_path, "YUTOME_POSTGRES_URL", "postgresql://x")
    actions._set_env_var(env_path, "VOYAGE_API_KEY", "v2")
    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert lines == ["VOYAGE_API_KEY=v2", "YUTOME_POSTGRES_URL=postgresql://x"]


def test_generate_local_workspace_id_shape_and_uniqueness() -> None:
    a = actions._generate_local_workspace_id()
    b = actions._generate_local_workspace_id()
    assert WS_RE.match(a) and WS_RE.match(b)
    assert a != b


def test_setup_generates_and_persists_local_workspace_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YUTOME_POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = tmp_path / "yutome.toml"
    runner = CliRunner()

    result = runner.invoke(app, ["--config", str(cfg), "setup", "-y"])
    assert result.exit_code == 0, result.output
    assert "Local workspace: ws_" in result.output

    ws = load_config(cfg).hosted.local_workspace_id
    assert WS_RE.match(ws)

    # Re-running setup is idempotent: it does not regenerate the id.
    runner.invoke(app, ["--config", str(cfg), "setup", "-y"])
    assert load_config(cfg).hosted.local_workspace_id == ws


def test_setup_with_source_without_postgres_dsn_exits_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YUTOME_POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = tmp_path / "yutome.toml"

    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(cfg),
            "setup",
            "https://www.youtube.com/watch?v=OEDoJyhQhXs",
            "-y",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "YUTOME_POSTGRES_URL or DATABASE_URL is not set" in result.output
    assert "uv run yutome setup" in result.output
    assert "uv run yutome hosted login" in result.output
    assert "skipped and not saved or queued" in result.output
    assert "Traceback" not in result.output
    assert WS_RE.match(load_config(cfg).hosted.local_workspace_id)


def test_corpus_add_without_postgres_dsn_exits_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YUTOME_POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = tmp_path / "yutome.toml"
    runner = CliRunner()

    setup_result = runner.invoke(app, ["--config", str(cfg), "setup", "-y"])
    assert setup_result.exit_code == 0, setup_result.output

    result = runner.invoke(
        app,
        [
            "--config",
            str(cfg),
            "corpus",
            "add",
            "https://www.youtube.com/watch?v=OEDoJyhQhXs",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "YUTOME_POSTGRES_URL or DATABASE_URL is not set" in result.output
    assert "uv run yutome setup" in result.output
    assert "uv run yutome hosted login" in result.output
    assert "skipped and not saved or queued" in result.output
    assert "Traceback" not in result.output


def test_status_reports_setup_created_local_workspace_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YUTOME_POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = tmp_path / "yutome.toml"
    runner = CliRunner()

    setup_result = runner.invoke(app, ["--config", str(cfg), "setup", "-y"])
    assert setup_result.exit_code == 0, setup_result.output
    workspace_id = load_config(cfg).hosted.local_workspace_id

    result = runner.invoke(app, ["--config", str(cfg), "status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["workspace_id"] == workspace_id
    assert payload["workspace_mode"] == "local"
    assert payload["hosted"]["workspace_id"] == ""
    assert payload["hosted"]["local_workspace_id"] == workspace_id


def test_remote_prepare_writes_generated_token_to_project_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("YUTOME_HTTP_TOKEN", raising=False)
    monkeypatch.setattr(actions.secrets, "token_urlsafe", lambda _bytes: "generated-test-token")
    cfg = tmp_path / "yutome.toml"
    write_default_config(cfg)

    result = CliRunner().invoke(app, ["--config", str(cfg), "serve", "remote", "prepare"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "YUTOME_HTTP_TOKEN=generated-test-token\n"
    assert "YUTOME_HTTP_TOKEN=generated-test-token" not in result.output
    assert "Next: uv run yutome serve remote http --host 0.0.0.0 --port 8765" in result.output
