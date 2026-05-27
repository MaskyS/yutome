from __future__ import annotations

import asyncio
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from yutome import contract, runtime, setup_prompts
from yutome.api import list_ as api_list
from yutome.config import DEFAULT_CONFIG_FILENAME, AppConfig, load_config
from yutome.db import bootstrap_catalog
from yutome.env import apply_env_to_config, load_dotenv
from yutome.paths import ProjectPaths
from yutome.remote_connection import (
    RELAY_TOKEN_REJECTED_MESSAGE,
    load_remote_state,
    mark_desktop_seen,
    normalize_remote_secret,
)


def _project_root(config_path: Path) -> Path:
    if config_path.is_absolute():
        return config_path.parent
    return (Path.cwd() / config_path).parent


def _load_config_or_exit(config_path: Path) -> AppConfig:
    try:
        return load_config(config_path)
    except FileNotFoundError as exc:
        typer.echo(
            f"yutome config not found at {config_path}. Run: yutome setup",
            err=True,
        )
        raise typer.Exit(code=2) from exc


def _load_runtime(config_path: Path) -> tuple[AppConfig, ProjectPaths]:
    load_dotenv(_project_root(config_path) / ".env")
    app_config = apply_env_to_config(_load_config_or_exit(config_path))
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config_path))
    bootstrap_catalog(paths.catalog_db)
    return app_config, paths

def _remote_bridge_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "yutome-bridge/0.1",
    }


def _remote_bridge_get_job(endpoint_url: str, *, token: str, timeout: float) -> dict[str, Any] | None:
    request = urllib.request.Request(
        f"{endpoint_url.rstrip('/')}/bridge/next",
        headers=_remote_bridge_headers(token),
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status == 204:
                return None
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"bridge poll failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"bridge poll failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("bridge poll returned non-object JSON")
    return payload


def _remote_bridge_post_result(endpoint_url: str, *, token: str, job_id: str, result: dict[str, Any], timeout: float) -> None:
    body = json.dumps({"job_id": job_id, "result": result}).encode("utf-8")
    request = urllib.request.Request(
        f"{endpoint_url.rstrip('/')}/bridge/result",
        data=body,
        headers=_remote_bridge_headers(token),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"bridge result upload failed: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"bridge result upload failed: {exc}") from exc


def _tool_result_text(tool: str, payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(raw) > 50000:
        raw = raw[:50000] + "\n... truncated ..."
    return f"Yutome {tool} result:\n{raw}"


def _bridge_tool_result(tool: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": _tool_result_text(tool, payload)}],
        "structuredContent": payload,
    }


def _bridge_tool_error(tool: str, message: str) -> dict[str, Any]:
    payload = {"ok": False, "tool": tool, "error": message}
    return {
        "isError": True,
        "content": [{"type": "text", "text": f"Yutome {tool} error: {message}"}],
        "structuredContent": payload,
    }


def _bridge_resource_result(uri: str, payload: dict[str, Any], mime_type: str) -> dict[str, Any]:
    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": mime_type,
                "text": json.dumps(payload, ensure_ascii=False),
            }
        ]
    }


# JSON-RPC error codes used in bridge envelopes. The Worker converts these
# into MCP-shaped errors on the wire.
_RPC_INVALID_PARAMS = -32602
_RPC_RESOURCE_NOT_FOUND = -32002
_RPC_INTERNAL_ERROR = -32603


def _bridge_rpc_error(code: int, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return error


def _parse_yutome_uri(uri: str) -> tuple[str, dict[str, str]]:
    """Parse ``yutome://<host>/<path>`` into ``(host, {param: value})``.

    Raises ValueError for malformed URIs or unknown hosts.
    """
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme != "yutome":
        raise ValueError(f"unsupported resource URI scheme: {parsed.scheme!r}")
    host = parsed.hostname or ""
    spec = contract.resource_by_host(host)
    if spec is None:
        raise ValueError(f"unknown resource host: {host!r}")
    # Extract the placeholder name from the URI template (e.g. {chunk_id}).
    placeholder_match = re.search(r"\{([^}]+)\}", spec.uri_template)
    if placeholder_match is None:
        return host, {}
    placeholder = placeholder_match.group(1)
    raw_path = parsed.path.lstrip("/")
    if not raw_path:
        raise ValueError(f"resource URI {uri!r} is missing the {placeholder} segment")
    value = urllib.parse.unquote(raw_path)
    return host, {placeholder: value}


def _install_bridge_runtime(app_config: AppConfig, paths: ProjectPaths) -> None:
    runtime.set_current(
        runtime.Runtime(config_path=Path("yutome.toml"), config=app_config, paths=paths)
    )


def _execute_bridge_tool(
    *,
    app_config: AppConfig,
    paths: ProjectPaths,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Legacy polling-bridge entry point. Accepts tool-call params
    (``{name, arguments}``) and returns a tool-result-shaped dict directly.
    Kept stable so the current Worker (which expects tool-result shape on
    /bridge/result) continues to work until the WebSocket migration."""
    _install_bridge_runtime(app_config, paths)
    return _dispatch_tool(params)


def _execute_bridge_job(
    *,
    app_config: AppConfig,
    paths: ProjectPaths,
    params: dict[str, Any],
) -> dict[str, Any]:
    """WebSocket-bridge entry point. Accepts the generalized
    ``{kind, method, params}`` envelope and returns ``{result|error}``.
    The new TS Worker will speak this shape over the bridge WebSocket."""
    _install_bridge_runtime(app_config, paths)

    kind = str(params.get("kind") or "tool")

    if kind == "tool":
        inner = params.get("params") if isinstance(params.get("params"), dict) else params
        return {"result": _dispatch_tool(inner)}

    if kind == "resource":
        return _dispatch_resource(params.get("params") or {})

    if kind == "resource_templates":
        return {"result": _list_resource_templates()}

    if kind == "resource_list":
        return {"result": _list_resources(params.get("params") or {})}

    return {
        "error": _bridge_rpc_error(
            _RPC_INVALID_PARAMS, f"unsupported bridge job kind: {kind!r}"
        )
    }


def _dispatch_tool(inner: dict[str, Any]) -> dict[str, Any]:
    tool = str(inner.get("name") or "")
    arguments = inner.get("arguments") or {}
    if not isinstance(arguments, dict):
        return _bridge_tool_error(tool or "unknown", "tool arguments must be a JSON object")
    spec = contract.tool_by_name(tool)
    if spec is None:
        return _bridge_tool_error(tool or "unknown", f"unsupported tool: {tool!r}")
    try:
        # ``in_`` arrives as ``in`` over the wire from some clients; normalize.
        if "in" in arguments and "in_" not in arguments:
            arguments = {**arguments, "in_": arguments.pop("in")}
        # ``id`` arrives as itself for show(kind="..."); the handler signature
        # uses ``id_`` to avoid the Python builtin shadow. Same normalization.
        if "id" in arguments and "id_" not in arguments and tool == "show":
            arguments = {**arguments, "id_": arguments.pop("id")}
        payload = spec.handler(**arguments)
    except Exception as exc:  # noqa: BLE001 - remote bridge should report tool errors cleanly.
        return _bridge_tool_error(tool or "unknown", str(exc))
    return _bridge_tool_result(tool, payload)


def _dispatch_resource(inner: dict[str, Any]) -> dict[str, Any]:
    uri = str(inner.get("uri") or "")
    if not uri:
        return {
            "error": _bridge_rpc_error(_RPC_INVALID_PARAMS, "resources/read requires uri")
        }
    try:
        host, kwargs = _parse_yutome_uri(uri)
    except ValueError as exc:
        return {"error": _bridge_rpc_error(_RPC_INVALID_PARAMS, str(exc))}
    spec = contract.resource_by_host(host)
    if spec is None:
        return {
            "error": _bridge_rpc_error(_RPC_RESOURCE_NOT_FOUND, f"unknown resource host: {host!r}")
        }
    try:
        payload = spec.handler(**kwargs)
    except ValueError as exc:
        return {"error": _bridge_rpc_error(_RPC_RESOURCE_NOT_FOUND, str(exc))}
    except Exception as exc:  # noqa: BLE001
        return {"error": _bridge_rpc_error(_RPC_INTERNAL_ERROR, str(exc))}
    return {"result": _bridge_resource_result(uri, payload, spec.mime_type)}


def _list_resource_templates() -> dict[str, Any]:
    return {
        "resourceTemplates": [
            {
                "uriTemplate": spec.uri_template,
                "name": spec.name,
                "description": spec.description,
                "mimeType": spec.mime_type,
            }
            for spec in contract.RESOURCES
        ]
    }


def _list_resources(params: dict[str, Any]) -> dict[str, Any]:
    """Enumerate concrete resource instances. Chunks are template-only by
    decision (millions of them); channels and recent videos are paginated."""
    host = str(params.get("host") or "")
    limit = max(1, min(int(params.get("limit") or 50), 200))
    offset = max(0, int(params.get("offset") or 0))

    if host in ("", "chunk"):
        return {"resources": []}

    if host == "channel":
        rows = api_list(
            config=runtime.current().config,
            paths=runtime.current().paths,
            entity="channels",
            limit=limit,
            offset=offset,
        ).model_dump()
        return {
            "resources": [
                {
                    "uri": f"yutome://channel/{row['channel_id']}",
                    "name": row.get("title") or row.get("handle") or row["channel_id"],
                    "mimeType": "application/json",
                }
                for row in rows.get("rows", [])
                if "channel_id" in row
            ]
        }

    if host == "video":
        rows = api_list(
            config=runtime.current().config,
            paths=runtime.current().paths,
            entity="videos",
            order_by="newest",
            limit=limit,
            offset=offset,
        ).model_dump()
        return {
            "resources": [
                {
                    "uri": f"yutome://video/{row['video_id']}",
                    "name": row.get("title") or row["video_id"],
                    "mimeType": "application/json",
                }
                for row in rows.get("rows", [])
                if "video_id" in row
            ]
        }

    return {"resources": []}




def _bridge_ws_url(endpoint_url: str) -> str:
    """Convert https://host[/...] to wss://host/relay/connect."""
    parsed = urllib.parse.urlsplit(endpoint_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urllib.parse.urlunsplit((scheme, parsed.netloc, "/relay/connect", "", ""))


def _bridge_connection_error_message(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        status_code = getattr(exc, "status_code", None)
    try:
        status = int(status_code)
    except (TypeError, ValueError):
        status = None
    if status == 401:
        return RELAY_TOKEN_REJECTED_MESSAGE
    return str(exc)


async def _bridge_ws_loop(
    *,
    app_config: AppConfig,
    paths: ProjectPaths,
    endpoint_url: str,
    token: str,
    once: bool,
) -> None:
    """Maintain a long-lived WebSocket to the Worker's relay DO. Each job
    frame is dispatched through ``_execute_bridge_job``; the result frame
    carries either ``result`` or ``error`` per the new envelope shape."""
    import websockets
    from websockets.exceptions import WebSocketException

    ws_url = _bridge_ws_url(endpoint_url)
    auth_token = normalize_remote_secret(token) or ""
    typer.echo(f"Yutome bridge connecting to {ws_url}")
    typer.echo("Keep this running while using Claude/ChatGPT remote MCP. Press Ctrl-C to stop.")

    backoff = 1.0
    while True:
        try:
            async with websockets.connect(
                ws_url,
                additional_headers=[("Authorization", f"Bearer {auth_token}")],
                max_size=8 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=20,
            ) as ws:
                typer.echo("[OK] Bridge connected")
                backoff = 1.0
                mark_desktop_seen(paths)
                processed = 0
                async for message in ws:
                    try:
                        frame = json.loads(message)
                    except json.JSONDecodeError:
                        typer.echo("[WARN] Ignoring non-JSON frame", err=True)
                        continue
                    if not isinstance(frame, dict):
                        continue
                    if frame.get("type") != "job":
                        # ping / control frames are handled by the lib
                        continue
                    job_id = str(frame.get("job_id") or "")
                    if not job_id:
                        continue
                    kind = str(frame.get("kind") or "tool")
                    method = str(frame.get("method") or "")
                    params = {
                        "kind": kind,
                        "method": method,
                        "params": frame.get("params") or {},
                    }
                    label = method or kind
                    typer.echo(f"[OK] Remote {label} ({job_id})")
                    envelope = _execute_bridge_job(
                        app_config=app_config, paths=paths, params=params
                    )
                    response = {"type": "result", "job_id": job_id}
                    response.update(envelope)  # adds "result" or "error"
                    await ws.send(json.dumps(response))
                    mark_desktop_seen(paths)
                    processed += 1
                    if once:
                        await ws.send(json.dumps({"type": "bye"}))
                        return
        except (OSError, WebSocketException) as exc:
            typer.echo(f"[WARN] Bridge disconnected: {_bridge_connection_error_message(exc)}", err=True)
            if once:
                raise typer.Exit(code=1) from exc
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


BRIDGE_PID_FILENAME = "bridge.pid"
BRIDGE_LOG_FILENAME = "bridge.log"
LAUNCHD_BRIDGE_LABEL = "ai.yutome.bridge"
SYSTEMD_BRIDGE_UNIT = "yutome-bridge.service"


def _bridge_pid_path(paths: ProjectPaths) -> Path:
    return paths.data_dir / "remote" / BRIDGE_PID_FILENAME


def _bridge_log_path(paths: ProjectPaths) -> Path:
    return paths.logs_dir / BRIDGE_LOG_FILENAME


def _read_bridge_pid(paths: ProjectPaths) -> int | None:
    try:
        content = _bridge_pid_path(paths).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    try:
        return int(content)
    except ValueError:
        return None


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop_bridge_pid(pid: int, *, timeout: float = 2.0) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_is_alive(pid):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def _bridge_binary_args() -> list[str]:
    binary = shutil.which("yutome")
    if binary:
        return [binary]
    return [sys.executable, "-m", "yutome"]


def _bridge_foreground_command(binary_args: list[str], config_path: Path) -> list[str]:
    return [
        *binary_args,
        "--config",
        str(config_path),
        "serve",
        "bridge",
        "start",
        "--foreground",
    ]


def _bridge_start_detached(config_path: Path, paths: ProjectPaths) -> tuple[int, Path]:
    """Spawn the bridge as a detached background process. Returns (pid, log_path)."""
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = _bridge_log_path(paths)
    pid_path = _bridge_pid_path(paths)
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    existing = _read_bridge_pid(paths)
    if existing and _pid_is_alive(existing):
        _stop_bridge_pid(existing)

    log_handle = log_path.open("ab")
    try:
        binary_args = _bridge_binary_args()
        command = _bridge_foreground_command(binary_args, config_path)
        proc = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=log_handle,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_handle.close()
    pid_path.write_text(str(proc.pid), encoding="utf-8")
    return proc.pid, log_path


def _bridge_run_foreground(config_path: Path, *, once: bool = False) -> None:
    """Run the asyncio WS loop in this process. Used by --foreground and by launchd/systemd."""
    app_config, paths = _load_runtime(config_path)
    state = load_remote_state(paths)
    if state is None:
        typer.echo("Remote connector is not configured. Run: yutome connect", err=True)
        raise typer.Exit(code=1)
    if not state.relay_token:
        typer.echo(
            "Remote connector has no bridge token. Redeploy with `yutome connect --deploy`, "
            "or save the existing Worker with `yutome connect --endpoint <url> --relay-token <token> --pairing-code <code>`.",
            err=True,
        )
        raise typer.Exit(code=1)
    asyncio.run(
        _bridge_ws_loop(
            app_config=app_config,
            paths=paths,
            endpoint_url=state.endpoint_url,
            token=state.relay_token,
            once=once,
        )
    )


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_BRIDGE_LABEL}.plist"


def _launchd_plist_content(
    binary_args: list[str], config_path: Path, project_root: Path, log_path: Path
) -> str:
    payload = {
        "Label": LAUNCHD_BRIDGE_LABEL,
        "ProgramArguments": _bridge_foreground_command(binary_args, config_path),
        "WorkingDirectory": str(project_root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "ProcessType": "Background",
    }
    return plistlib.dumps(payload, sort_keys=False).decode("utf-8")


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_BRIDGE_UNIT


def _systemd_quote(value: str | Path) -> str:
    text = str(value)
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


def _systemd_unit_content(
    binary_args: list[str], config_path: Path, project_root: Path, log_path: Path
) -> str:
    exec_start = " ".join(
        _systemd_quote(arg) for arg in _bridge_foreground_command(binary_args, config_path)
    )
    return (
        "[Unit]\n"
        "Description=Yutome bridge to Cloudflare remote MCP Worker\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        f"WorkingDirectory={_systemd_quote(project_root)}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        f"StandardOutput=append:{_systemd_quote(log_path)}\n"
        f"StandardError=append:{_systemd_quote(log_path)}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _launchd_installed() -> bool:
    return sys.platform == "darwin" and _launchd_plist_path().exists()


def _systemd_installed() -> bool:
    return sys.platform.startswith("linux") and _systemd_unit_path().exists()


def _config_arg_from_argv(argv: list[str]) -> Path | None:
    try:
        index = argv.index("--config")
    except ValueError:
        return None
    if index + 1 >= len(argv):
        return None
    return Path(argv[index + 1])


def _installed_bridge_config_path() -> Path | None:
    """Best-effort config path encoded in the installed service file."""
    if _launchd_installed():
        try:
            payload = plistlib.loads(_launchd_plist_path().read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError):
            return None
        argv = payload.get("ProgramArguments") if isinstance(payload, dict) else None
        if isinstance(argv, list) and all(isinstance(arg, str) for arg in argv):
            return _config_arg_from_argv(argv)
        return None
    if _systemd_installed():
        try:
            content = _systemd_unit_path().read_text(encoding="utf-8")
        except OSError:
            return None
        for line in content.splitlines():
            if not line.startswith("ExecStart="):
                continue
            try:
                argv = shlex.split(line.removeprefix("ExecStart="))
            except ValueError:
                return None
            return _config_arg_from_argv(argv)
    return None


def _same_config_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left.absolute() == right.absolute()


def _installed_service_matches_config(config_path: Path) -> bool:
    installed_config = _installed_bridge_config_path()
    if installed_config is None:
        # Unknown/legacy service files are treated as matching so existing
        # installs stay controllable.
        return True
    return _same_config_path(installed_config, config_path)


def _installed_service_mismatch_message(config_path: Path) -> str:
    installed_config = _installed_bridge_config_path()
    if installed_config is None:
        return "Bridge auto-start is installed, but yutome could not read its config path."
    return (
        "Bridge auto-start is installed for another config: "
        f"{installed_config}. Current config: {config_path.resolve()}."
    )


def _launchd_bridge_pid() -> int | None:
    """Return the PID of the launchd-managed bridge, or None if not running.

    launchd doesn't write a PID file the way our detached-process path
    does, so `bridge status` used to either show a stale manual PID or
    "not started" while the bridge was very much running. Query launchd
    directly via ``launchctl print gui/<uid>/<label>`` and parse the
    ``pid = N`` line. Falls back to ``launchctl list <label>`` if the
    modern form isn't recognised (older macOS / unusual launchctl).
    """
    if sys.platform != "darwin":
        return None
    target = f"gui/{os.getuid()}/{LAUNCHD_BRIDGE_LABEL}"
    try:
        result = subprocess.run(
            ["launchctl", "print", target],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode == 0:
        match = re.search(r"^\s*pid\s*=\s*(\d+)", result.stdout, re.MULTILINE)
        if match:
            pid = int(match.group(1))
            return pid if pid > 0 else None
    # Fallback to the older `launchctl list <label>` plist-like output.
    try:
        legacy = subprocess.run(
            ["launchctl", "list", LAUNCHD_BRIDGE_LABEL],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if legacy.returncode != 0:
        return None
    match = re.search(r'"PID"\s*=\s*(\d+)', legacy.stdout)
    if match:
        pid = int(match.group(1))
        return pid if pid > 0 else None
    return None


def _systemd_bridge_pid() -> int | None:
    """Return the PID of the systemd-managed bridge, or None if not running."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", SYSTEMD_BRIDGE_UNIT, "-p", "MainPID", "--value"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    text = (result.stdout or "").strip()
    if not text.isdigit():
        return None
    pid = int(text)
    return pid if pid > 0 else None


def _service_bridge_pid() -> int | None:
    """Return the auto-start service's bridge PID if installed and running.

    Used by `bridge status` and `bridge start` to avoid the duplicate-
    bridge bug: if launchd / systemd already runs the bridge, the manual
    PID file is irrelevant and a manual `bridge start` would spawn a
    second process the service manager can't see.
    """
    if _launchd_installed():
        return _launchd_bridge_pid()
    if _systemd_installed():
        return _systemd_bridge_pid()
    return None


def _start_launchd_bridge_service() -> subprocess.CompletedProcess[str]:
    plist_path = _launchd_plist_path()
    result = subprocess.run(
        ["launchctl", "load", str(plist_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 or _launchd_bridge_pid() is not None:
        return result
    return subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCHD_BRIDGE_LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )


def _stop_launchd_bridge_service() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", "unload", str(_launchd_plist_path())],
        capture_output=True,
        text=True,
        check=False,
    )


def _restart_bridge_after_deploy(config_path: Path, paths: ProjectPaths) -> None:
    """Called after connect/disconnect state changes. Restart any auto-managed bridge so it
    picks up the new relay token; or auto-start a detached bridge if no service is installed."""
    state = load_remote_state(paths)
    if state is None or not state.relay_token:
        return
    if _launchd_installed():
        if _installed_service_matches_config(config_path):
            subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LAUNCHD_BRIDGE_LABEL}"],
                capture_output=True, check=False,
            )
            typer.echo("Bridge: restarted via launchd to pick up the new token.")
            return
        typer.secho(f"[WARN] {_installed_service_mismatch_message(config_path)}", fg="yellow")
        typer.echo("Bridge: starting a project-local background bridge instead.")
    if _systemd_installed():
        if _installed_service_matches_config(config_path):
            subprocess.run(
                ["systemctl", "--user", "restart", SYSTEMD_BRIDGE_UNIT],
                capture_output=True, check=False,
            )
            typer.echo("Bridge: restarted via systemd to pick up the new token.")
            return
        typer.secho(f"[WARN] {_installed_service_mismatch_message(config_path)}", fg="yellow")
        typer.echo("Bridge: starting a project-local background bridge instead.")
    pid, log_path = _bridge_start_detached(config_path, paths)
    typer.echo(f"Bridge: started in background (PID {pid}, logs at {log_path}).")
    typer.echo("Survives this terminal session but not reboots. For persistence: yutome serve bridge install")


def _finalize_remote_bridge_setup(
    *,
    config_path: Path,
    paths: ProjectPaths,
    before_persistence: Callable[[], None] | None = None,
) -> None:
    """Start/restart the relay bridge, then handle reboot persistence consent.

    Used by every remote setup path that saves a relay token. If a remote
    endpoint is connector-only with no relay token, there is no laptop bridge
    to run, so this intentionally no-ops.
    """
    state = load_remote_state(paths)
    if state is None or not state.relay_token:
        return
    _restart_bridge_after_deploy(config_path=config_path, paths=paths)
    if before_persistence is not None:
        before_persistence()
    _offer_bridge_persistence(config_path)


def bridge_start_command(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    foreground: bool = typer.Option(
        False, "--foreground", help="Run in foreground (used by launchd/systemd)."
    ),
) -> None:
    """Start the bridge so remote MCP clients can reach this laptop."""
    if foreground:
        _bridge_run_foreground(config)
        return
    # If launchd / systemd is installed, defer to it instead of spawning a
    # detached process. Otherwise the manual start writes a PID file the
    # service manager can't see, both processes connect to the worker, and
    # `bridge stop` only kills the manual one — leaving a ghost bridge.
    if _launchd_installed():
        if not _installed_service_matches_config(config):
            typer.secho(f"[WARN] {_installed_service_mismatch_message(config)}", fg="yellow")
            typer.echo("Starting a project-local background bridge instead.")
        else:
            existing_pid = _launchd_bridge_pid()
            if existing_pid:
                typer.secho(
                    f"[OK] Bridge is already running under launchd (PID {existing_pid}).",
                    fg="green",
                )
                typer.echo(
                    f"     Restart it: launchctl kickstart -k gui/{os.getuid()}/{LAUNCHD_BRIDGE_LABEL}"
                )
                typer.echo("     Remove auto-start: yutome serve bridge uninstall")
                return
            typer.echo("Bridge auto-start is installed but the service isn't running. Starting via launchd…")
            result = _start_launchd_bridge_service()
            new_pid = _launchd_bridge_pid()
            if new_pid:
                typer.secho(f"[OK] Bridge running under launchd (PID {new_pid}).", fg="green")
            else:
                detail = (result.stderr or result.stdout or "").strip()
                typer.echo(
                    "[WARN] launchctl returned but no PID was reported. Check `yutome serve bridge status`."
                    + (f" Details: {detail}" if detail else "")
                )
            return
    if _systemd_installed():
        if not _installed_service_matches_config(config):
            typer.secho(f"[WARN] {_installed_service_mismatch_message(config)}", fg="yellow")
            typer.echo("Starting a project-local background bridge instead.")
        else:
            existing_pid = _systemd_bridge_pid()
            if existing_pid:
                typer.secho(
                    f"[OK] Bridge is already running under systemd (PID {existing_pid}).",
                    fg="green",
                )
                typer.echo(f"     Restart it: systemctl --user restart {SYSTEMD_BRIDGE_UNIT}")
                typer.echo("     Remove auto-start: yutome serve bridge uninstall")
                return
            typer.echo("Bridge auto-start is installed but the service isn't running. Starting via systemd…")
            subprocess.run(
                ["systemctl", "--user", "start", SYSTEMD_BRIDGE_UNIT],
                capture_output=True, check=False,
            )
            new_pid = _systemd_bridge_pid()
            if new_pid:
                typer.secho(f"[OK] Bridge running under systemd (PID {new_pid}).", fg="green")
            else:
                typer.echo("[WARN] systemctl start returned but no MainPID was reported. Check `yutome serve bridge status`.")
            return
    _, paths = _load_runtime(config)
    pid, log_path = _bridge_start_detached(config, paths)
    typer.echo(f"[OK] Bridge started (PID {pid}).")
    typer.echo(f"     Logs: {log_path}")
    typer.echo("     Stop with: yutome serve bridge stop")
    typer.echo("     Want it to survive reboots? Run: yutome serve bridge install")


def bridge_stop_command(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME), "--config", "-c", help="Path to the yutome TOML config."
    ),
) -> None:
    """Stop the background bridge process."""
    _, paths = _load_runtime(config)
    service_handled = False
    if _launchd_installed():
        if _installed_service_matches_config(config):
            result = _stop_launchd_bridge_service()
            service_handled = True
            if result.returncode == 0 or _launchd_bridge_pid() is None:
                typer.echo("[OK] Stopped launchd bridge service. Start it again with: yutome serve bridge start")
            else:
                detail = (result.stderr or result.stdout or "").strip()
                typer.echo(
                    "[WARN] launchd bridge service did not stop cleanly."
                    + (f" Details: {detail}" if detail else "")
                )
        else:
            typer.secho(f"[WARN] {_installed_service_mismatch_message(config)}", fg="yellow")
            typer.echo("Not stopping that service; checking for a project-local manual bridge.")
    elif _systemd_installed():
        if _installed_service_matches_config(config):
            result = subprocess.run(
                ["systemctl", "--user", "stop", SYSTEMD_BRIDGE_UNIT],
                capture_output=True,
                text=True,
                check=False,
            )
            service_handled = True
            if result.returncode == 0 or _systemd_bridge_pid() is None:
                typer.echo("[OK] Stopped systemd bridge service. Start it again with: yutome serve bridge start")
            else:
                detail = (result.stderr or result.stdout or "").strip()
                typer.echo(
                    "[WARN] systemd bridge service did not stop cleanly."
                    + (f" Details: {detail}" if detail else "")
                )
        else:
            typer.secho(f"[WARN] {_installed_service_mismatch_message(config)}", fg="yellow")
            typer.echo("Not stopping that service; checking for a project-local manual bridge.")

    pid = _read_bridge_pid(paths)
    if pid is None:
        if not service_handled:
            typer.echo("No bridge PID recorded; nothing to stop.")
        return
    if not _pid_is_alive(pid):
        typer.echo(f"PID {pid} is not running. Clearing stale PID file.")
        _bridge_pid_path(paths).unlink(missing_ok=True)
        return
    _stop_bridge_pid(pid)
    _bridge_pid_path(paths).unlink(missing_ok=True)
    typer.echo(f"[OK] Stopped bridge (PID {pid}).")


def bridge_status_command(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME), "--config", "-c", help="Path to the yutome TOML config."
    ),
) -> None:
    """Show bridge process status."""
    _, paths = _load_runtime(config)
    if _launchd_installed():
        typer.echo("Bridge auto-start: launchd")
        typer.echo(f"  plist: {_launchd_plist_path()}")
        if not _installed_service_matches_config(config):
            typer.secho(f"[WARN] {_installed_service_mismatch_message(config)}", fg="yellow")
            manual_pid = _read_bridge_pid(paths)
            if manual_pid is None:
                typer.echo("Bridge process: not started for this config (no PID file).")
                return
            if _pid_is_alive(manual_pid):
                typer.echo(f"Bridge process: running for this config (manual PID {manual_pid}).")
                typer.echo(f"  logs: {_bridge_log_path(paths)}")
            else:
                typer.echo(f"Bridge process: PID {manual_pid} recorded but process is not alive.")
            return
    elif _systemd_installed():
        typer.echo("Bridge auto-start: systemd")
        typer.echo(f"  unit: {_systemd_unit_path()}")
        if not _installed_service_matches_config(config):
            typer.secho(f"[WARN] {_installed_service_mismatch_message(config)}", fg="yellow")
            manual_pid = _read_bridge_pid(paths)
            if manual_pid is None:
                typer.echo("Bridge process: not started for this config (no PID file).")
                return
            if _pid_is_alive(manual_pid):
                typer.echo(f"Bridge process: running for this config (manual PID {manual_pid}).")
                typer.echo(f"  logs: {_bridge_log_path(paths)}")
            else:
                typer.echo(f"Bridge process: PID {manual_pid} recorded but process is not alive.")
            return
    else:
        typer.echo("Bridge auto-start: not installed (run `yutome serve bridge install` to persist across reboots)")
    # When auto-start is installed, the *service manager's* PID is the
    # source of truth — not the local PID file, which only the manual
    # detached-start path writes to.
    service_pid = _service_bridge_pid()
    if service_pid is not None:
        source = "launchd" if _launchd_installed() else "systemd"
        typer.echo(f"Bridge process: running via {source} (PID {service_pid}).")
        typer.echo(f"  logs: {_bridge_log_path(paths)}")
        manual_pid = _read_bridge_pid(paths)
        if manual_pid is not None and manual_pid != service_pid and _pid_is_alive(manual_pid):
            typer.secho(
                f"[WARN] A second bridge process is also running (manual PID {manual_pid}). "
                "Stop it with `yutome serve bridge stop` to avoid two bridges fighting for the worker.",
                fg="yellow",
            )
        return
    if _launchd_installed() or _systemd_installed():
        typer.echo("Bridge process: auto-start is configured but the service isn't running.")
        typer.echo("  Start it with: yutome serve bridge start")
        return
    manual_pid = _read_bridge_pid(paths)
    if manual_pid is None:
        typer.echo("Bridge process: not started (no PID file).")
        return
    if _pid_is_alive(manual_pid):
        typer.echo(f"Bridge process: running (PID {manual_pid}).")
        typer.echo(f"  logs: {_bridge_log_path(paths)}")
    else:
        typer.echo(f"Bridge process: PID {manual_pid} recorded but process is not alive.")
        typer.echo("  Run `yutome serve bridge start` to restart it.")


def _install_bridge_service(config_path: Path) -> tuple[bool, Path | None, str | None]:
    """Install the bridge as a launchd / systemd user service.

    Returns ``(installed, service_path, error_message)``. Used by both
    ``yutome serve bridge install`` and the post-deploy setup step that offers
    persistence after a successful Cloudflare deploy. Splitting it out
    lets the setup wizard call this without raising ``typer.Exit`` on
    failure — the wizard prefers to warn and continue.
    """
    # Fail fast on unsupported platforms — avoid doing any I/O or path
    # resolution if we know we can't install the service here.
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        return (
            False,
            None,
            f"`yutome serve bridge install` is not supported on platform {sys.platform!r} yet. "
            "Use `yutome serve bridge start` to run the bridge manually.",
        )
    _, paths = _load_runtime(config_path)
    config_abs = config_path.resolve()
    project_root = _project_root(config_path)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = _bridge_log_path(paths)
    binary_args = _bridge_binary_args()

    if sys.platform == "darwin":
        plist_path = _launchd_plist_path()
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(
            _launchd_plist_content(binary_args, config_abs, project_root, log_path),
            encoding="utf-8",
        )
        existing = _read_bridge_pid(paths)
        if existing and _pid_is_alive(existing):
            _stop_bridge_pid(existing)
        _bridge_pid_path(paths).unlink(missing_ok=True)
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, check=False)
        result = subprocess.run(
            ["launchctl", "load", str(plist_path)], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            return False, None, f"launchctl load failed: {result.stderr or result.stdout}"
        return True, plist_path, None

    if sys.platform.startswith("linux"):
        unit_path = _systemd_unit_path()
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(
            _systemd_unit_content(binary_args, config_abs, project_root, log_path),
            encoding="utf-8",
        )
        existing = _read_bridge_pid(paths)
        if existing and _pid_is_alive(existing):
            _stop_bridge_pid(existing)
        _bridge_pid_path(paths).unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)
        result = subprocess.run(
            ["systemctl", "--user", "enable", "--now", SYSTEMD_BRIDGE_UNIT],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            return False, None, f"systemctl enable --now failed: {result.stderr or result.stdout}"
        return True, unit_path, None

    # Unreachable — the platform check at the top of the function already
    # returned for non-darwin / non-linux. Kept as a defensive catch-all.
    return (
        False,
        None,
        f"`yutome serve bridge install` is not supported on platform {sys.platform!r}.",
    )


def _bridge_persistence_supported() -> bool:
    return sys.platform == "darwin" or sys.platform.startswith("linux")


def _bridge_install_command(config_path: Path) -> str:
    return f"yutome --config {shlex.quote(str(config_path.resolve()))} serve bridge install"


def _offer_bridge_persistence(config_path: Path) -> None:
    """Post-deploy: ask the user whether to install the bridge as a service.

    Today the bridge auto-runs as a detached background process (started
    by ``_restart_bridge_after_deploy``) — that survives this terminal
    session but not a reboot. For the "use yutome from my phone" case
    that the cloudflare deploy unlocks, "survives reboot" is what the
    user almost always wants, so we default the confirm to Yes.

    Non-interactive callers cannot consent, so they get an explicit warning
    and the exact command to install persistence later.
    """
    if not _bridge_persistence_supported():
        return
    if _launchd_installed() or _systemd_installed():
        if _installed_service_matches_config(config_path):
            typer.secho(
                "[OK] Bridge auto-start service is already installed — survives reboots.",
                fg="green",
            )
            return
        typer.secho(f"[WARN] {_installed_service_mismatch_message(config_path)}", fg="yellow")
        if not setup_prompts.is_interactive():
            typer.echo(
                "[WARN] Bridge auto-start was not installed because this run is non-interactive."
            )
            typer.echo(f"Run `{_bridge_install_command(config_path)}` to repoint auto-start.")
            return
    if not setup_prompts.is_interactive():
        typer.echo(
            "[WARN] Bridge auto-start was not installed because this run is non-interactive."
        )
        typer.echo(f"Run `{_bridge_install_command(config_path)}` to persist across reboots.")
        return
    typer.echo("")
    if _launchd_installed() or _systemd_installed():
        typer.echo(
            "Install bridge auto-start for this project, replacing the existing "
            "Yutome auto-start service?"
        )
    else:
        typer.echo(
            "The bridge is running in the background, but it'll stop the next time this "
            "computer reboots. Install it as a "
            + ("launchd agent (macOS)" if sys.platform == "darwin" else "systemd user service")
            + " so your assistant can still reach yutome after a restart?"
        )
    if not setup_prompts.confirm(
        "Install bridge auto-start so it survives reboots?",
        default=True,
    ):
        typer.echo(f"Skipped. Run `{_bridge_install_command(config_path)}` later to enable persistence.")
        return
    installed, service_path, error_message = _install_bridge_service(config_path)
    if installed and service_path is not None:
        typer.secho(f"[OK] Installed bridge auto-start service: {service_path}", fg="green")
        typer.echo("     Uninstall any time with: yutome serve bridge uninstall")
    else:
        typer.secho(
            f"[WARN] Bridge auto-start didn't install: {error_message or 'unknown error'}",
            fg="yellow",
        )
        typer.echo("       You can retry later with: yutome serve bridge install")


def bridge_install_command(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME), "--config", "-c", help="Path to the yutome TOML config."
    ),
) -> None:
    """Install the bridge as a launchd (macOS) or systemd user (Linux) service."""
    installed, service_path, error_message = _install_bridge_service(config)
    if not installed:
        if error_message:
            typer.echo(error_message, err=True)
        raise typer.Exit(code=1)
    _, paths = _load_runtime(config)
    log_path = _bridge_log_path(paths)
    typer.echo(f"[OK] Installed bridge auto-start service: {service_path}")
    typer.echo(f"     Logs: {log_path}")
    typer.echo("     Uninstall with: yutome serve bridge uninstall")


def bridge_uninstall_command() -> None:
    """Remove the launchd / systemd auto-start configuration."""
    if sys.platform == "darwin":
        plist_path = _launchd_plist_path()
        if not plist_path.exists():
            typer.echo("No launchd agent installed.")
            return
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, check=False)
        plist_path.unlink(missing_ok=True)
        typer.echo(f"[OK] Removed launchd agent: {plist_path}")
        return
    if sys.platform.startswith("linux"):
        unit_path = _systemd_unit_path()
        if not unit_path.exists():
            typer.echo("No systemd unit installed.")
            return
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", SYSTEMD_BRIDGE_UNIT],
            capture_output=True, check=False,
        )
        unit_path.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, check=False)
        typer.echo(f"[OK] Removed systemd unit: {unit_path}")
        return
    typer.echo(f"Nothing to uninstall on platform {sys.platform!r}.")
