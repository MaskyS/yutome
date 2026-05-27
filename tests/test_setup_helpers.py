"""Regression tests for the setup-wizard / bridge helpers added in v0.1.10 /
v0.1.11.

Covers:

- `_normalize_pasted_connector_url` — URL preprocessor for the paste-URL
  connect flow.
- `setup_prompts.select` tuple-shape semantics — separators, value tokens,
  disabled choices, default resolution.
- `_wait_for_worker_online` — polls /healthz after `wrangler deploy`.
- `_launchd_bridge_pid` / `_systemd_bridge_pid` — service-PID introspection
  that fixes the dual-bridge bug.
- `bridge_start_command` — defers to the service manager when installed.
- `bridge_status_command` — reports the service PID and warns about a
  duplicate manual PID.
- `_offer_bridge_persistence` — short-circuits when already installed and
  warns when non-interactive.
- `_print_deploy_secrets_card` — clipboard write gated on TTY.
- `_install_bridge_service` — returns the error contract instead of raising.
"""

from __future__ import annotations

import http.server
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yutome import setup_prompts
from yutome.cli import (
    _normalize_pasted_connector_url,
    _print_deploy_secrets_card,
    app,
)
from yutome.cli._bridge import (
    _install_bridge_service,
    _launchd_bridge_pid,
    _offer_bridge_persistence,
    _service_bridge_pid,
    _systemd_bridge_pid,
)
from yutome.cli._worker_deploy import _wait_for_worker_online


# --------------------------------------------------------------------------- #
# _normalize_pasted_connector_url                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", ""),
        ("   ", ""),
        # Trailing slash is dropped.
        ("https://x.workers.dev/", "https://x.workers.dev"),
        # /pair and /authorize are stripped — they're OAuth flow paths a
        # user copies by mistake, not connector URLs.
        ("https://x.workers.dev/pair", "https://x.workers.dev"),
        ("https://x.workers.dev/authorize", "https://x.workers.dev"),
        # /mcp is preserved — the downstream normaliser accepts both base
        # and /mcp forms.
        ("https://x.workers.dev/mcp", "https://x.workers.dev/mcp"),
        # Missing scheme gets https:// prepended.
        ("x.workers.dev", "https://x.workers.dev"),
        # Query strings are dropped (the /pair?code=... pairing-URL shape).
        ("https://x.workers.dev/pair?code=ABC", "https://x.workers.dev"),
    ],
)
def test_normalize_pasted_connector_url_canonical_cases(raw, expected):
    assert _normalize_pasted_connector_url(raw) == expected


def test_normalize_pasted_connector_url_warns_on_zero_dot_host(capsys):
    # Hosts without a dot (besides .workers.dev) trigger the warning gate.
    _normalize_pasted_connector_url("https://localhost")
    captured = capsys.readouterr()
    assert "WARN" in captured.out


# --------------------------------------------------------------------------- #
# setup_prompts.select — tuple-shape semantics                                #
# --------------------------------------------------------------------------- #


def test_select_non_tty_plain_strings_backward_compatible(monkeypatch):
    monkeypatch.setattr("yutome.setup_prompts._is_tty", lambda: False)
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "2")
    result = setup_prompts.select("Pick", ["alpha", "beta", "gamma"], default="alpha")
    assert result == "beta"


def test_select_non_tty_returns_value_token_not_label(monkeypatch):
    monkeypatch.setattr("yutome.setup_prompts._is_tty", lambda: False)
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "1")
    result = setup_prompts.select(
        "Pick",
        [("Local apps on this Mac · …", "local"), ("Deploy to Cloudflare · …", "deploy")],
        default="local",
    )
    assert result == "local"


def test_select_non_tty_skips_separators_in_numbering(monkeypatch, capsys):
    monkeypatch.setattr("yutome.setup_prompts._is_tty", lambda: False)
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "2")
    result = setup_prompts.select(
        "Pick",
        [
            ("─── header ───",),
            ("First option", "first"),
            ("Second option", "second"),
        ],
    )
    # Separator is printed but not numbered — "2" maps to "Second option".
    assert result == "second"
    captured = capsys.readouterr()
    assert "─── header ───" in captured.out


def test_select_non_tty_disabled_choice_decorated_but_still_selectable(monkeypatch, capsys):
    # Non-TTY intentionally lets disabled choices be picked so the
    # downstream late-check (e.g. "Node missing") fires with the user's
    # explicit intent rather than greying out.
    monkeypatch.setattr("yutome.setup_prompts._is_tty", lambda: False)
    monkeypatch.setattr("typer.prompt", lambda *args, **kwargs: "1")
    result = setup_prompts.select(
        "Pick",
        [("Deploy", "deploy", "Install Node.js 22+ first")],
    )
    assert result == "deploy"
    captured = capsys.readouterr()
    assert "Install Node.js 22+ first" in captured.out


def test_select_non_tty_default_matches_value(monkeypatch, capsys):
    # When `default` matches a tuple's value, the default-index printed by
    # the prompt should land on that row.
    monkeypatch.setattr("yutome.setup_prompts._is_tty", lambda: False)

    prompted_defaults = []

    def fake_prompt(message, default=None):
        prompted_defaults.append(default)
        return default

    monkeypatch.setattr("typer.prompt", fake_prompt)
    setup_prompts.select(
        "Pick",
        [("Alpha", "alpha"), ("Beta", "beta"), ("Gamma", "gamma")],
        default="beta",
    )
    assert prompted_defaults == ["2"]


def test_select_tty_passes_questionary_choice_and_separator(monkeypatch):
    # In TTY we route to questionary — verify the right Choice / Separator
    # types are constructed, the default_value lands on the right label,
    # and the helper returns whatever questionary's .ask() returned.
    monkeypatch.setattr("yutome.setup_prompts._is_tty", lambda: True)

    import questionary

    captured = {}

    class FakePrompt:
        def ask(self):
            return "deploy"

    def fake_select(message, choices, default, style):
        captured["choices"] = choices
        captured["default"] = default
        return FakePrompt()

    monkeypatch.setattr(questionary, "select", fake_select)
    result = setup_prompts.select(
        "Pick",
        [
            ("─── header ───",),
            ("Local", "local"),
            ("Deploy", "deploy", "needs Node"),
        ],
        default="deploy",
    )
    assert result == "deploy"
    types = [type(item).__name__ for item in captured["choices"]]
    assert types == ["Separator", "Choice", "Choice"]
    deploy_choice = captured["choices"][2]
    assert deploy_choice.value == "deploy"
    assert deploy_choice.disabled == "needs Node"
    # The default-value passed to questionary must be the stored value;
    # questionary validates defaults against Choice.value, not title.
    assert captured["default"] == "deploy"


# --------------------------------------------------------------------------- #
# _wait_for_worker_online                                                     #
# --------------------------------------------------------------------------- #


class _StubHTTPServer:
    """Tiny single-threaded HTTP server for testing /healthz polling.

    Returns ``status`` with ``body`` for every request. Used as a context
    manager so the server thread is cleaned up reliably.
    """

    def __init__(self, *, status: int = 200, body: bytes = b'{"ok":true}') -> None:
        self._status = status
        self._body = body
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.request_count = 0

    def __enter__(self) -> "_StubHTTPServer":
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.request_count += 1
                self.send_response(outer._status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(outer._body)))
                self.end_headers()
                self.wfile.write(outer._body)

            def log_message(self, *args, **kwargs) -> None:  # silence stderr noise
                return

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def __exit__(self, *exc) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


def test_wait_for_worker_online_returns_true_on_200():
    with _StubHTTPServer(status=200) as server:
        assert _wait_for_worker_online(server.url, timeout=2.0, interval=0.1) is True


def test_wait_for_worker_online_returns_true_on_401():
    # 4xx means the URL is reachable — the worker is serving traffic, even
    # if the response isn't 200. The polling treats this as "edge is up".
    with _StubHTTPServer(status=401, body=b'{"error":"unauthorized"}') as server:
        assert _wait_for_worker_online(server.url, timeout=2.0, interval=0.1) is True


def test_wait_for_worker_online_returns_false_when_unreachable(capsys):
    # Bind+close a socket to grab a port that's guaranteed unused.
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    url = f"http://127.0.0.1:{port}"

    started = time.monotonic()
    assert _wait_for_worker_online(url, timeout=0.8, interval=0.15) is False
    elapsed = time.monotonic() - started
    # The timeout should be respected — should not far exceed it.
    assert elapsed < 4.0, f"timeout not respected: {elapsed:.2f}s"
    captured = capsys.readouterr()
    assert "WARN" in captured.out
    assert "not yet reachable" in captured.out


# --------------------------------------------------------------------------- #
# _launchd_bridge_pid / _systemd_bridge_pid / _service_bridge_pid             #
# --------------------------------------------------------------------------- #


def _fake_completed(stdout: str, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def test_launchd_bridge_pid_parses_modern_print_output(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_run(cmd, **_kwargs):
        # Modern `launchctl print gui/<uid>/<label>` form.
        return _fake_completed(
            "\n".join(
                [
                    "gui/501/ai.yutome.bridge = {",
                    "\tactive count = 1",
                    "\tstate = running",
                    "\tpid = 12345",
                    "\tprogram = /usr/local/bin/yutome",
                    "}",
                ]
            )
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _launchd_bridge_pid() == 12345


def test_launchd_bridge_pid_falls_back_to_legacy_list(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            return _fake_completed("", returncode=1)  # modern form failed
        if cmd[:2] == ["launchctl", "list"]:
            return _fake_completed(
                '{\n\t"Label" = "ai.yutome.bridge";\n\t"PID" = 67890;\n};\n'
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _launchd_bridge_pid() == 67890
    assert len(calls) == 2  # print first, then list fallback


def test_launchd_bridge_pid_returns_none_when_both_fail(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        subprocess, "run", lambda *_a, **_k: _fake_completed("", returncode=1)
    )
    assert _launchd_bridge_pid() is None


def test_launchd_bridge_pid_returns_none_on_non_darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert _launchd_bridge_pid() is None


def test_systemd_bridge_pid_parses_main_pid(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _fake_completed("4242\n"))
    assert _systemd_bridge_pid() == 4242


def test_systemd_bridge_pid_treats_zero_main_pid_as_not_running(monkeypatch):
    # systemd reports MainPID=0 for a unit that's "known but inactive".
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _fake_completed("0\n"))
    assert _systemd_bridge_pid() is None


def test_systemd_bridge_pid_returns_none_on_non_numeric(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: _fake_completed(""))
    assert _systemd_bridge_pid() is None


def test_service_bridge_pid_routes_to_installed_manager(monkeypatch):
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._launchd_bridge_pid", lambda: 999)
    monkeypatch.setattr("yutome.cli._bridge._systemd_bridge_pid", lambda: 11111)
    assert _service_bridge_pid() == 999


def test_service_bridge_pid_returns_none_when_no_service_installed(monkeypatch):
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)
    assert _service_bridge_pid() is None


# --------------------------------------------------------------------------- #
# bridge_start_command — defer to service manager                              #
# --------------------------------------------------------------------------- #


def _make_config(tmp_path: Path) -> Path:
    """Write a minimal yutome config so `_load_runtime` works."""
    from yutome.config import write_default_config

    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    return config_path


def test_bridge_start_with_launchd_running_does_not_spawn_detached(monkeypatch, tmp_path):
    config_path = _make_config(tmp_path)
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._installed_service_matches_config", lambda _cfg: True)
    monkeypatch.setattr("yutome.cli._bridge._launchd_bridge_pid", lambda: 5555)

    spawned = {"hit": False}
    monkeypatch.setattr(
        "yutome.cli._bridge._bridge_start_detached",
        lambda *_a, **_k: spawned.update(hit=True) or (0, Path("/dev/null")),
    )

    result = CliRunner().invoke(app, ["--config", str(config_path), "serve", "bridge", "start"])
    assert result.exit_code == 0
    assert spawned["hit"] is False
    assert "already running under launchd" in result.output
    assert "PID 5555" in result.output


def test_bridge_start_with_launchd_installed_but_not_running_starts_service(monkeypatch, tmp_path):
    config_path = _make_config(tmp_path)
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._installed_service_matches_config", lambda _cfg: True)
    # First poll: not running. After kickstart: running.
    pid_returns = iter([None, 7777])
    monkeypatch.setattr("yutome.cli._bridge._launchd_bridge_pid", lambda: next(pid_returns))

    invocations: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        invocations.append(cmd)
        return _fake_completed("")

    monkeypatch.setattr("yutome.cli._bridge.subprocess.run", fake_run)

    spawned = {"hit": False}
    monkeypatch.setattr(
        "yutome.cli._bridge._bridge_start_detached",
        lambda *_a, **_k: spawned.update(hit=True) or (0, Path("/dev/null")),
    )

    result = CliRunner().invoke(app, ["--config", str(config_path), "serve", "bridge", "start"])
    assert result.exit_code == 0
    assert spawned["hit"] is False
    assert any(cmd[:2] == ["launchctl", "load"] for cmd in invocations), invocations
    assert "PID 7777" in result.output


def test_bridge_start_with_no_service_uses_detached(monkeypatch, tmp_path):
    config_path = _make_config(tmp_path)
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)

    spawned = {"pid": None}

    def fake_detached(_cfg, paths):
        spawned["pid"] = 1234
        return 1234, paths.logs_dir / "bridge.log"

    monkeypatch.setattr("yutome.cli._bridge._bridge_start_detached", fake_detached)

    result = CliRunner().invoke(app, ["--config", str(config_path), "serve", "bridge", "start"])
    assert result.exit_code == 0
    assert spawned["pid"] == 1234
    assert "PID 1234" in result.output


# --------------------------------------------------------------------------- #
# bridge_status_command — report service PID, warn on duplicate                #
# --------------------------------------------------------------------------- #


def test_bridge_status_reports_service_pid_when_launchd_running(monkeypatch, tmp_path):
    config_path = _make_config(tmp_path)
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._installed_service_matches_config", lambda _cfg: True)
    monkeypatch.setattr("yutome.cli._bridge._service_bridge_pid", lambda: 9001)
    monkeypatch.setattr("yutome.cli._bridge._read_bridge_pid", lambda _paths: None)

    result = CliRunner().invoke(app, ["--config", str(config_path), "serve", "bridge", "status"])
    assert result.exit_code == 0
    assert "running via launchd (PID 9001)" in result.output


def test_bridge_status_warns_on_duplicate_manual_bridge(monkeypatch, tmp_path):
    # launchd has the bridge running at PID 5555 AND the user has somehow
    # also got a manual bridge running at PID 8888. Status should flag the
    # duplicate so the user can stop one of them.
    config_path = _make_config(tmp_path)
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._installed_service_matches_config", lambda _cfg: True)
    monkeypatch.setattr("yutome.cli._bridge._service_bridge_pid", lambda: 5555)
    monkeypatch.setattr("yutome.cli._bridge._read_bridge_pid", lambda _paths: 8888)
    monkeypatch.setattr("yutome.cli._bridge._pid_is_alive", lambda _pid: True)

    result = CliRunner().invoke(app, ["--config", str(config_path), "serve", "bridge", "status"])
    assert result.exit_code == 0
    assert "PID 5555" in result.output
    assert "WARN" in result.output
    assert "8888" in result.output


def test_bridge_status_does_not_warn_when_manual_pid_matches_service_pid(monkeypatch, tmp_path):
    # Edge case: the manual PID file happens to hold the same PID the
    # service manager reports (e.g. left over from a previous manual run
    # that was inherited / coincidence). No duplicate warning expected.
    config_path = _make_config(tmp_path)
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._installed_service_matches_config", lambda _cfg: True)
    monkeypatch.setattr("yutome.cli._bridge._service_bridge_pid", lambda: 4242)
    monkeypatch.setattr("yutome.cli._bridge._read_bridge_pid", lambda _paths: 4242)
    monkeypatch.setattr("yutome.cli._bridge._pid_is_alive", lambda _pid: True)

    result = CliRunner().invoke(app, ["--config", str(config_path), "serve", "bridge", "status"])
    assert result.exit_code == 0
    assert "WARN" not in result.output


def test_bridge_status_when_service_installed_but_not_running(monkeypatch, tmp_path):
    config_path = _make_config(tmp_path)
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._installed_service_matches_config", lambda _cfg: True)
    monkeypatch.setattr("yutome.cli._bridge._service_bridge_pid", lambda: None)
    monkeypatch.setattr("yutome.cli._bridge._read_bridge_pid", lambda _paths: None)

    result = CliRunner().invoke(app, ["--config", str(config_path), "serve", "bridge", "status"])
    assert result.exit_code == 0
    assert "auto-start is configured but the service isn't running" in result.output


# --------------------------------------------------------------------------- #
# _offer_bridge_persistence                                                    #
# --------------------------------------------------------------------------- #


def test_offer_bridge_persistence_short_circuits_when_service_installed(monkeypatch, capsys, tmp_path):
    config_path = _make_config(tmp_path)
    monkeypatch.setattr("yutome.cli._bridge._bridge_persistence_supported", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._installed_service_matches_config", lambda _cfg: True)

    confirm_calls = {"n": 0}

    def fake_confirm(*args, **kwargs):
        confirm_calls["n"] += 1
        return True

    monkeypatch.setattr("yutome.setup_prompts.confirm", fake_confirm)
    monkeypatch.setattr("yutome.setup_prompts.is_interactive", lambda: True)

    install_calls = {"n": 0}

    def fake_install(_cfg):
        install_calls["n"] += 1
        return True, Path("/dev/null"), None

    monkeypatch.setattr("yutome.cli._bridge._install_bridge_service", fake_install)

    _offer_bridge_persistence(config_path)

    assert confirm_calls["n"] == 0
    assert install_calls["n"] == 0
    captured = capsys.readouterr()
    assert "already installed" in captured.out


def test_offer_bridge_persistence_warns_on_non_interactive(monkeypatch, capsys, tmp_path):
    config_path = _make_config(tmp_path)
    monkeypatch.setattr("yutome.cli._bridge._bridge_persistence_supported", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)
    monkeypatch.setattr("yutome.setup_prompts.is_interactive", lambda: False)

    confirm_calls = {"n": 0}

    def fake_confirm(*args, **kwargs):
        confirm_calls["n"] += 1
        return True

    monkeypatch.setattr("yutome.setup_prompts.confirm", fake_confirm)

    _offer_bridge_persistence(config_path)

    assert confirm_calls["n"] == 0
    captured = capsys.readouterr()
    assert "non-interactive" in captured.out
    assert "yutome --config" in captured.out
    assert "serve bridge install" in captured.out


def test_offer_bridge_persistence_noninteractive_mismatch_does_not_install(
    monkeypatch, capsys, tmp_path
):
    config_path = _make_config(tmp_path)
    monkeypatch.setattr("yutome.cli._bridge._bridge_persistence_supported", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._installed_service_matches_config", lambda _cfg: False)
    monkeypatch.setattr("yutome.cli._bridge._installed_bridge_config_path", lambda: tmp_path / "other.toml")
    monkeypatch.setattr("yutome.setup_prompts.is_interactive", lambda: False)

    install_calls = {"n": 0}
    monkeypatch.setattr(
        "yutome.cli._bridge._install_bridge_service",
        lambda _cfg: install_calls.update(n=install_calls["n"] + 1) or (True, Path("/x"), None),
    )

    _offer_bridge_persistence(config_path)

    assert install_calls["n"] == 0
    captured = capsys.readouterr()
    assert "another config" in captured.out
    assert "non-interactive" in captured.out
    assert "repoint auto-start" in captured.out


def test_offer_bridge_persistence_install_failure_warns_does_not_raise(
    monkeypatch, capsys, tmp_path
):
    config_path = _make_config(tmp_path)
    monkeypatch.setattr("yutome.cli._bridge._bridge_persistence_supported", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)
    monkeypatch.setattr("yutome.setup_prompts.is_interactive", lambda: True)
    monkeypatch.setattr("yutome.setup_prompts.confirm", lambda *a, **k: True)
    monkeypatch.setattr(
        "yutome.cli._bridge._install_bridge_service",
        lambda _cfg: (False, None, "launchctl boom"),
    )

    # Must NOT raise typer.Exit — the setup wizard prefers to warn and
    # continue instead of crashing mid-flow.
    _offer_bridge_persistence(config_path)

    captured = capsys.readouterr()
    assert "WARN" in captured.out
    assert "launchctl boom" in captured.out


def test_offer_bridge_persistence_user_declines_prints_skip(monkeypatch, capsys, tmp_path):
    config_path = _make_config(tmp_path)
    monkeypatch.setattr("yutome.cli._bridge._bridge_persistence_supported", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)
    monkeypatch.setattr("yutome.setup_prompts.is_interactive", lambda: True)
    monkeypatch.setattr("yutome.setup_prompts.confirm", lambda *a, **k: False)

    install_calls = {"n": 0}
    monkeypatch.setattr(
        "yutome.cli._bridge._install_bridge_service",
        lambda _cfg: install_calls.update(n=install_calls["n"] + 1) or (True, Path("/x"), None),
    )

    _offer_bridge_persistence(config_path)

    assert install_calls["n"] == 0
    captured = capsys.readouterr()
    assert "Skipped" in captured.out
    assert "yutome --config" in captured.out
    assert "serve bridge install" in captured.out


# --------------------------------------------------------------------------- #
# _print_deploy_secrets_card — clipboard gated on TTY                          #
# --------------------------------------------------------------------------- #


def test_deploy_secrets_card_skips_clipboard_in_non_interactive(monkeypatch, capsys):
    monkeypatch.setattr("yutome.setup_prompts.is_interactive", lambda: False)

    clipboard_calls = {"n": 0}

    def fake_copy(_text):
        clipboard_calls["n"] += 1
        return True

    monkeypatch.setattr("yutome.cli._legacy._copy_to_clipboard", fake_copy)

    _print_deploy_secrets_card("https://x.workers.dev/mcp", "ABCDEF")

    assert clipboard_calls["n"] == 0
    captured = capsys.readouterr()
    # Card still renders.
    assert "Yutome Worker is live" in captured.out
    assert "https://x.workers.dev/mcp" in captured.out
    assert "ABCDEF" in captured.out
    # The "copied to your clipboard" message is suppressed.
    assert "copied to your clipboard" not in captured.out


def test_deploy_secrets_card_copies_clipboard_in_interactive(monkeypatch, capsys):
    monkeypatch.setattr("yutome.setup_prompts.is_interactive", lambda: True)

    captured_copy: list[str] = []

    def fake_copy(text):
        captured_copy.append(text)
        return True

    monkeypatch.setattr("yutome.cli._legacy._copy_to_clipboard", fake_copy)

    _print_deploy_secrets_card("https://x.workers.dev/mcp", "ABCDEF")

    assert captured_copy == ["https://x.workers.dev/mcp"]
    captured = capsys.readouterr()
    assert "copied to your clipboard" in captured.out


def test_deploy_secrets_card_falls_back_when_clipboard_unavailable(monkeypatch, capsys):
    monkeypatch.setattr("yutome.setup_prompts.is_interactive", lambda: True)
    monkeypatch.setattr("yutome.cli._legacy._copy_to_clipboard", lambda _t: False)

    _print_deploy_secrets_card("https://x.workers.dev/mcp", "ABCDEF")

    captured = capsys.readouterr()
    assert "Couldn't auto-copy" in captured.out


# --------------------------------------------------------------------------- #
# _install_bridge_service — error contract                                     #
# --------------------------------------------------------------------------- #


def test_install_bridge_service_returns_error_tuple_on_launchctl_failure(monkeypatch, tmp_path):
    config_path = _make_config(tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        "yutome.cli._bridge._launchd_plist_path", lambda: tmp_path / "ai.yutome.bridge.plist"
    )
    monkeypatch.setattr(
        "yutome.cli._bridge._launchd_plist_content",
        lambda *a, **k: "<plist/>",
    )
    monkeypatch.setattr("yutome.cli._bridge._read_bridge_pid", lambda _p: None)
    monkeypatch.setattr("yutome.cli._bridge._stop_bridge_pid", lambda _p: True)

    def fake_run(cmd, **_kwargs):
        # `unload` is best-effort; `load` is the one whose return code matters.
        if cmd[:2] == ["launchctl", "load"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="permission denied")
        return _fake_completed("")

    monkeypatch.setattr("yutome.cli._bridge.subprocess.run", fake_run)

    installed, path, err = _install_bridge_service(config_path)

    assert installed is False
    assert path is None
    assert err is not None
    assert "permission denied" in err


def test_install_bridge_service_returns_error_on_unsupported_platform(monkeypatch, tmp_path):
    config_path = _make_config(tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")

    installed, path, err = _install_bridge_service(config_path)

    assert installed is False
    assert path is None
    assert err is not None
    assert "not supported on platform" in err


def test_install_bridge_service_success_returns_path(monkeypatch, tmp_path):
    config_path = _make_config(tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    plist_path = tmp_path / "ai.yutome.bridge.plist"
    monkeypatch.setattr("yutome.cli._bridge._launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr("yutome.cli._bridge._launchd_plist_content", lambda *a, **k: "<plist/>")
    monkeypatch.setattr("yutome.cli._bridge._read_bridge_pid", lambda _p: None)
    monkeypatch.setattr("yutome.cli._bridge._stop_bridge_pid", lambda _p: True)
    monkeypatch.setattr("yutome.cli._bridge.subprocess.run", lambda *a, **k: _fake_completed(""))

    installed, path, err = _install_bridge_service(config_path)

    assert installed is True
    assert path == plist_path
    assert err is None
    assert plist_path.exists()
