from __future__ import annotations

import asyncio
import http.server
import importlib.util
import json
import os
import plistlib
import platform
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from yutome.config import DEFAULT_CONFIG_FILENAME, AppConfig, load_config, write_default_config
from yutome import contract, runtime
from yutome.api import find as api_find
from yutome.api import list_ as api_list
from yutome.api import q as api_q
from yutome.api import show as api_show
from yutome.channels import (
    LibraryChannel,
    list_library_channels,
)
from yutome.db import bootstrap_catalog, catalog_is_initialized, connect_catalog, fts5_available
from yutome.embeddings import embed_pending_chunks, rebuild_lancedb_chunks
from yutome.env import apply_env_to_config, load_dotenv
from yutome.evals import load_eval_suite, run_eval_suite
from yutome.exports import export_markdown
from yutome.gemini import transcribe_youtube_url_with_gemini
from yutome.hosted.cli_helpers import append_demo_usage_events, summarize_usage_events
from yutome.hosted.account_cli import code_challenge_for_verifier, new_code_verifier
from yutome.hosted.ledger import JsonlUsageLedger, default_usage_ledger_path
from yutome.hosted.runtime import HostedCommandRunner, HostedRuntimeError
from yutome.indexer import sync_channel, sync_video
from yutome.paths import ProjectPaths
from yutome.maintenance import rebuild_active_chunks
from yutome.quality_upgrade import upgrade_active_transcripts
from yutome.query import QueryRequest
from yutome import setup_prompts
from yutome.remote_connection import (
    RELAY_TOKEN_REJECTED_MESSAGE,
    RemoteMode,
    build_remote_state,
    build_sync_dry_run_manifest,
    load_remote_state,
    mark_desktop_seen,
    normalize_endpoint,
    normalize_remote_secret,
    remote_status_payload,
    remote_state_path,
    save_remote_state,
)
from yutome.sources import (
    LibrarySource,
    import_sources_from_file,
    list_library_sources,
    set_library_source_selected,
    source_from_channel,
    source_from_input,
    upsert_library_source,
)
from yutome.youtube_oauth import fetch_subscription_channels, load_oauth_client, load_or_authorize_token
from yutome.youtube_import import (
    YouTubeImportError,
    fetch_public_subscription_channels_from_api,
    fetch_public_subscription_channels_from_scrape,
    fetch_user_subscription_channels_from_browser,
)
from yutome.youtube import (
    describe_proxy,
    fetch_subtitle_transcript_with_ytdlp,
    fetch_transcript,
    is_proxy_payment_error,
    proxy_payment_required_message,
    proxy_url_for_ytdlp,
    redact_proxy_secrets,
    redact_proxy_url,
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Local-first YouTube source knowledge base indexer.",
)
export_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Export indexed artifacts.")
list_app = typer.Typer(add_completion=False, no_args_is_help=True, help="List indexed corpus objects.")
show_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Show indexed corpus objects.")
quality_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Transcript quality tools.")
mcp_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Local MCP server (for Claude Desktop, Cursor, Claude Code, and other MCP-aware apps on this machine).")
http_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Local HTTP API server.")
eval_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Run retrieval quality checks.")
remote_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Run the authenticated HTTP / MCP server for private-network or reverse-proxy access.")
bridge_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Run the long-lived bridge that lets remote MCP clients reach this laptop's corpus.")
contract_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Inspect and export the MCP contract.")
hosted_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Hosted Postgres runtime commands.")
app.add_typer(export_app, name="export")
app.add_typer(list_app, name="list")
app.add_typer(show_app, name="show")
app.add_typer(quality_app, name="quality")
app.add_typer(mcp_app, name="mcp")
app.add_typer(http_app, name="http")
app.add_typer(eval_app, name="eval")
app.add_typer(remote_app, name="remote")
app.add_typer(bridge_app, name="bridge")
app.add_typer(contract_app, name="contract")
app.add_typer(hosted_app, name="hosted")


def _version_callback(value: bool) -> None:
    if not value:
        return
    from yutome import __version__

    typer.echo(f"yutome {__version__}")
    raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        is_eager=True,
        callback=_version_callback,
        help="Print the installed yutome version and exit.",
    ),
) -> None:
    """Local-first YouTube source knowledge base indexer."""
    del version  # handled by callback


ENV_TEMPLATE = """# Local secrets and proxy configuration. This file is ignored by git.

# Voyage embeddings. Needed for semantic/hybrid search.
VOYAGE_API_KEY=

# Generic HTTP/SOCKS proxy used by youtube-transcript-api and yt-dlp.
# YUTOME_PROXY_URLS=http://user:pass@host1:port,socks5://user:pass@host2:port
# YUTOME_HTTP_PROXY=http://user:pass@host:port
# YUTOME_HTTPS_PROXY=http://user:pass@host:port

# Webshare rotating residential proxy config.
# YUTOME_WEBSHARE_USERNAME=
# YUTOME_WEBSHARE_PASSWORD=
# YUTOME_WEBSHARE_DOMAIN=p.webshare.io
# YUTOME_WEBSHARE_PORT=80

# YouTube subscription imports. Keep OAuth JSON outside git.
# YUTOME_YOUTUBE_OAUTH_CLIENT_SECRETS=/absolute/path/to/client_secret.json
# YUTOME_YOUTUBE_API_KEY=

# Gemini fallback and transcript cleanup.
# GEMINI_API_KEY=
# GOOGLE_API_KEY=

# Remote API/MCP access. Generate with: yutome remote prepare
# YUTOME_HTTP_TOKEN=
# YUTOME_HTTP_CORS_ORIGINS=
"""

WEBSHARE_ENV_KEYS = (
    "YUTOME_WEBSHARE_USERNAME",
    "YUTOME_WEBSHARE_PASSWORD",
    "YUTOME_WEBSHARE_DOMAIN",
    "YUTOME_WEBSHARE_PORT",
)

CLOUDFLARE_WORKERS_DASHBOARD_URL = "https://dash.cloudflare.com/?to=/:account/workers-and-pages"
NODE_DOWNLOAD_URL = "https://nodejs.org/en/download"
CLOUDFLARE_MIN_NODE_VERSION = (22, 0, 0)
BACK_CHOICE = "Back"

# Sentinel returned by service-setup helpers when the user picks Back from a
# prompt that supports it. setup() loops on these so the user can re-decide
# without restarting the whole wizard.
BACK = object()

SETUP_TOTAL_STEPS = 6
HOSTED_SETUP_TOTAL_STEPS = 4
DEFAULT_HOSTED_APP_URL = "https://app.getyutome.com"
DEFAULT_HOSTED_API_URL = "https://api-production-e072.up.railway.app"
HOSTED_AUTH_FILENAME = "yutome-hosted-cli.json"


def _step_header(step: int, total: int, title: str) -> None:
    """Print a brand-coloured "Step N of M · title" divider.

    Color is stripped automatically by click when stdout is not a TTY, so
    CliRunner tests see plain text and existing substring assertions still
    match.
    """
    typer.echo("")
    line = f"━━ Step {step} of {total} · {title} "
    width = 72
    pad = max(2, width - len(line))
    typer.secho(line + ("━" * pad), fg="magenta", bold=True)
    typer.echo("")


def _status_skip(label: str, detail: str = "") -> None:
    """Dimmed [SKIP] line — for choices the user explicitly skipped.

    Visually distinct from [WARN] so a deliberate skip doesn't read as a
    problem to a first-time user.
    """
    suffix = f" - {detail}" if detail else ""
    typer.secho(f"[SKIP] {label}{suffix}", dim=True)


def _styled_url(url: str) -> str:
    """Return a click-styled URL string (blue + underline on TTY)."""
    return typer.style(url, fg="blue", underline=True)


class _SpinnerContext:
    """Context manager that shows a spinner in interactive terminals and is
    a no-op everywhere else. Falls back gracefully when Rich isn't usable.
    """

    def __init__(self, message: str) -> None:
        self._message = message
        self._status = None

    def __enter__(self):
        if not (sys.stdout.isatty() and setup_prompts.is_interactive()):
            return self
        try:
            from rich.console import Console
            from rich.status import Status

            self._console = Console(file=sys.stdout)
            self._status = Status(self._message, console=self._console, spinner="dots")
            self._status.__enter__()
        except Exception:
            # If rich isn't usable for any reason, fall back to a single
            # heartbeat line so the user still knows something started.
            self._status = None
            typer.echo(f"… {self._message}")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._status is not None:
            try:
                self._status.__exit__(exc_type, exc, tb)
            except Exception:
                pass
        return False

    def update(self, message: str) -> None:
        if self._status is not None:
            try:
                self._status.update(message)
            except Exception:
                pass


def _spinner(message: str) -> _SpinnerContext:
    """Return a spinner context manager. See `_SpinnerContext`."""
    return _SpinnerContext(message)


def _callout(text: str, *, kind: str = "info") -> None:
    """Print a one-line callout that draws the eye to a decision sentence.

    kind='info'    bright but neutral
    kind='warn'    yellow — for plan/option choices that bite if missed
    """
    if kind == "warn":
        typer.secho(text, fg="yellow", bold=True)
    else:
        typer.secho(text, bold=True)


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


def _load_paths(config_path: Path) -> ProjectPaths:
    config = _load_config_or_exit(config_path)
    return ProjectPaths.from_config(config, project_root=_project_root(config_path))


def _load_runtime(config_path: Path) -> tuple[object, ProjectPaths]:
    load_dotenv(_project_root(config_path) / ".env")
    app_config = apply_env_to_config(_load_config_or_exit(config_path))
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config_path))
    bootstrap_catalog(paths.catalog_db)
    return app_config, paths


def _load_hosted_config(config_path: Path) -> AppConfig:
    load_dotenv(_project_root(config_path) / ".env")
    return apply_env_to_config(_load_config_or_exit(config_path))


def _hosted_runner(config_path: Path) -> HostedCommandRunner:
    return HostedCommandRunner(_load_hosted_config(config_path))


def _hosted_api_app(config_path: Path) -> Any:
    from yutome.hosted.runtime import build_hosted_api_app

    return build_hosted_api_app(_hosted_runner(config_path))


def _run_hosted_api_app(api_app: Any, *, host: str, port: int, log_level: str = "info") -> None:
    import uvicorn

    uvicorn.run(api_app, host=host, port=port, log_level=log_level)


def _jsonable(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")  # type: ignore[no-any-return, attr-defined]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _echo_json(value: object) -> None:
    typer.echo(json.dumps(_jsonable(value), ensure_ascii=False, indent=2, default=str))


def _echo_query_result(result: object, *, json_output: bool) -> None:
    if hasattr(result, "model_dump"):
        payload = result.model_dump()
    else:
        payload = result
    if json_output:
        _echo_json(payload)
        return
    if not isinstance(payload, dict):
        typer.echo(str(payload))
        return
    for note in payload.get("notes", []):
        typer.echo(f"note: {note}")
    rows = payload.get("rows", [])
    if len(rows) == 1 and isinstance(rows[0], dict) and payload.get("total") == 1:
        _echo_json(rows[0])
    else:
        _echo_json(rows)


def _read_query_request(request: str | None, file: Path | None) -> dict[str, object]:
    if file is not None:
        raw = file.read_text(encoding="utf-8")
    elif request == "-":
        raw = sys.stdin.read()
    elif request:
        raw = request
    else:
        raise typer.BadParameter("Pass a JSON QueryRequest, '-' for stdin, or --file.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter("QueryRequest JSON must be an object.")
    return payload


def _status(ok: bool, label: str, detail: str = "") -> None:
    """Print a single-line status. Color is stripped by click on non-TTY."""
    marker = "OK" if ok else "WARN"
    suffix = f" - {detail}" if detail else ""
    typer.secho(f"[{marker}] {label}{suffix}", fg="green" if ok else "yellow", bold=True)


def _proxy_diagnostic_detail(app_config, exc: Exception, *, video_id: str) -> str:  # noqa: ANN001
    detail = redact_proxy_secrets(app_config.proxy, str(exc), key=video_id)
    if is_proxy_payment_error(detail):
        return proxy_payment_required_message(app_config.proxy, operation="proxy test")
    return detail[:500]


def _module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def _command_version(command: str) -> tuple[bool, str]:
    command_path = shutil.which(command)
    if command_path is None:
        return False, "not found"
    try:
        result = subprocess.run(
            [command_path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{command_path} failed: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        message = detail[-1] if detail else f"exit code {result.returncode}"
        return False, f"{command_path} failed: {message}"
    version = result.stdout.strip().splitlines()[0] if result.stdout.strip() else "version unknown"
    return True, f"{command_path} ({version})"


def _write_env_template(path: Path) -> bool:
    if path.exists():
        return False
    path.write_text(ENV_TEMPLATE, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return True


def _read_env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _env_has_webshare_credentials(path: Path) -> bool:
    values = _read_env_values(path)
    return bool(values.get("YUTOME_WEBSHARE_USERNAME") and values.get("YUTOME_WEBSHARE_PASSWORD"))


def _env_has_voyage_key(path: Path) -> bool:
    return bool(_env_value(path, "VOYAGE_API_KEY"))


def _env_has_gemini_key(path: Path) -> bool:
    return bool(_env_value(path, "GEMINI_API_KEY") or _env_value(path, "GOOGLE_API_KEY"))


def _merge_env_values(path: Path, updates: dict[str, str], *, overwrite: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    merged: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, current_value = stripped.split("=", 1)
            key = key.strip()
            if key in updates:
                seen.add(key)
                if current_value.strip().strip('"').strip("'") and not overwrite:
                    merged.append(line)
                else:
                    merged.append(f"{key}={updates[key]}")
                continue
        merged.append(line)
    for key, value in updates.items():
        if key not in seen:
            merged.append(f"{key}={value}")
    path.write_text("\n".join(merged).rstrip() + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _env_value(path: Path, key: str) -> str | None:
    return _read_env_values(path).get(key) or os.environ.get(key)


def _configured_oauth_client_secrets(app_config: AppConfig, project_root: Path, env_path: Path) -> Path | None:
    raw_path = _env_value(env_path, "YUTOME_YOUTUBE_OAUTH_CLIENT_SECRETS") or app_config.youtube.oauth_client_secrets
    if raw_path is None:
        return None
    path = Path(raw_path)
    return path if path.is_absolute() else project_root / path


def _youtube_api_key(app_config: AppConfig, env_path: Path) -> str | None:
    return _env_value(env_path, app_config.youtube.api_key_env)


def _fetch_youtube_import_channels(
    *,
    target: str | None,
    app_config: AppConfig,
    paths: ProjectPaths,
    project_root: Path,
    env_path: Path,
    port: int = 0,
    open_browser: bool = True,
    status_callback: Any = typer.echo,
) -> list[LibraryChannel]:
    if target:
        api_key = _youtube_api_key(app_config, env_path)
        if api_key:
            try:
                return fetch_public_subscription_channels_from_api(target, api_key=api_key)
            except YouTubeImportError as exc:
                if status_callback:
                    status_callback(f"YouTube API import unavailable: {exc}")
                    status_callback("Trying public page scrape instead.")
        return fetch_public_subscription_channels_from_scrape(target)

    try:
        return fetch_user_subscription_channels_from_browser(
            browsers=app_config.youtube.browser_cookie_browsers,
            status_callback=status_callback,
        )
    except YouTubeImportError as cookie_error:
        client_secrets = _configured_oauth_client_secrets(app_config, project_root, env_path)
        if client_secrets is None:
            raise YouTubeImportError(
                f"{cookie_error} Configure YUTOME_YOUTUBE_OAUTH_CLIENT_SECRETS in .env "
                "to use browser consent instead."
            ) from cookie_error
        client = load_oauth_client(client_secrets)
        oauth_token = load_or_authorize_token(
            client=client,
            token_path=paths.data_dir / "auth" / "youtube-oauth-token.json",
            port=port,
            open_browser=open_browser,
            status_callback=status_callback,
        )
        return fetch_subscription_channels(str(oauth_token["access_token"]))


def _save_imported_channels(
    paths: ProjectPaths,
    channels: list[LibraryChannel],
    *,
    selected: bool,
) -> int:
    with connect_catalog(paths.catalog_db) as connection:
        for channel in channels:
            upsert_library_source(connection, source_from_channel(channel), selected=selected)
        connection.commit()
    return len(channels)


def _generate_http_token() -> str:
    return secrets.token_urlsafe(32)


def _header_token(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _set_toml_bool(config_path: Path, section: str, key: str, value: bool) -> None:
    _set_toml_value(config_path, section, key, "true" if value else "false")


def _set_toml_string(config_path: Path, section: str, key: str, value: str) -> None:
    escaped = value.replace("\\", "\\\\").replace("\"", "\\\"")
    _set_toml_value(config_path, section, key, f'"{escaped}"')


def _set_toml_value(config_path: Path, section: str, key: str, rendered_value: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = config_path.read_text(encoding="utf-8").splitlines() if config_path.exists() else []
    target_header = f"[{section}]"
    rendered_key = f"{key} = {rendered_value}"
    output: list[str] = []
    in_section = False
    found_section = False
    wrote_key = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section and not wrote_key:
                output.append(rendered_key)
                wrote_key = True
            in_section = stripped == target_header
            found_section = found_section or in_section
        if in_section and stripped.startswith(f"{key}") and "=" in stripped:
            output.append(rendered_key)
            wrote_key = True
            continue
        output.append(line)

    if found_section and in_section and not wrote_key:
        output.append(rendered_key)
    if not found_section:
        if output and output[-1].strip():
            output.append("")
        output.extend([target_header, rendered_key])

    config_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


class HostedCliApiError(RuntimeError):
    pass


class HostedCliLoginError(RuntimeError):
    pass


def _hosted_auth_path(paths: ProjectPaths) -> Path:
    return paths.data_dir / "auth" / HOSTED_AUTH_FILENAME


def _normalize_url_base(value: str | None, *, fallback: str) -> str:
    raw = (value or fallback).strip() or fallback
    return raw.rstrip("/")


def _hosted_api_url(app_config: AppConfig, override: str | None = None) -> str:
    return _normalize_url_base(override or app_config.hosted.api_url, fallback=DEFAULT_HOSTED_API_URL)


def _hosted_app_url(app_config: AppConfig, override: str | None = None) -> str:
    return _normalize_url_base(override or app_config.hosted.app_url, fallback=DEFAULT_HOSTED_APP_URL)


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, sort_keys=True)
        file.write("\n")
    path.chmod(0o600)


def _load_hosted_auth(paths: ProjectPaths) -> dict[str, Any]:
    auth_path = _hosted_auth_path(paths)
    if not auth_path.exists():
        raise HostedCliLoginError("Run `yutome hosted login` before using hosted CLI source import.")
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HostedCliLoginError(f"Hosted CLI auth file is unreadable: {auth_path}") from exc
    token = payload.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise HostedCliLoginError("Hosted CLI auth file is missing an access token. Run `yutome hosted login` again.")
    return payload


def _hosted_api_request_json(
    api_base: str,
    path: str,
    *,
    method: str = "POST",
    body: Mapping[str, Any] | None = None,
    access_token: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    url = _normalize_url_base(api_base, fallback=DEFAULT_HOSTED_API_URL) + path
    data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw_error = exc.read().decode("utf-8", errors="replace")
        raise HostedCliApiError(_hosted_api_error_message(raw_error, fallback=f"Hosted API returned HTTP {exc.code}.")) from exc
    except urllib.error.URLError as exc:
        raise HostedCliApiError(f"Could not reach hosted API at {api_base}: {exc.reason}") from exc
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise HostedCliApiError("Hosted API returned a non-JSON response.") from exc
    if not isinstance(parsed, dict):
        raise HostedCliApiError("Hosted API returned an unexpected response.")
    if parsed.get("ok") is False:
        raise HostedCliApiError(_hosted_api_error_message(json.dumps(parsed), fallback="Hosted API request failed."))
    return parsed


def _hosted_api_error_message(raw: str, *, fallback: str) -> str:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return fallback
    if not isinstance(parsed, dict):
        return fallback
    detail = parsed.get("detail")
    if isinstance(detail, dict):
        message = detail.get("message")
        code = detail.get("code")
        if isinstance(message, str) and isinstance(code, str):
            return f"{message} ({code})"
        if isinstance(message, str):
            return message
    message = parsed.get("message")
    return message if isinstance(message, str) else fallback


def _run_hosted_login_flow(
    *,
    config_path: Path,
    app_url: str | None = None,
    api_url: str | None = None,
    port: int = 0,
    open_browser: bool = True,
) -> dict[str, Any]:
    write_default_config(config_path)
    app_config = load_config(config_path)
    project_root = _project_root(config_path)
    paths = ProjectPaths.from_config(app_config, project_root=project_root)
    paths.ensure_base_dirs()
    app_base = _hosted_app_url(app_config, app_url)
    api_base = _hosted_api_url(app_config, api_url)
    verifier = new_code_verifier()
    challenge = code_challenge_for_verifier(verifier)
    state = secrets.token_urlsafe(24)
    callback: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback API
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            received_state = params.get("state", [""])[0]
            code = params.get("code", [""])[0]
            error = params.get("error", [""])[0]
            if received_state != state:
                callback["error"] = "Hosted CLI login returned an invalid state."
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Invalid hosted CLI login state.")
                return
            if error:
                callback["error"] = error
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Hosted CLI login failed.")
                return
            callback["code"] = code
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Yutome CLI is connected. You can return to your terminal.")

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    with http.server.HTTPServer(("127.0.0.1", port), Handler) as server:
        server.timeout = 300
        redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
        authorize_url = app_base + "/cli/authorize?" + urllib.parse.urlencode(
            {
                "client_id": "yutome-cli",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        typer.echo(f"Opening hosted Yutome login: {authorize_url}")
        if open_browser:
            webbrowser.open(authorize_url)
        else:
            typer.echo(authorize_url)
        server.handle_request()

    if callback.get("error"):
        raise HostedCliLoginError(callback["error"])
    code = callback.get("code")
    if not code:
        raise HostedCliLoginError("Timed out waiting for hosted CLI login callback.")
    token_response = _hosted_api_request_json(
        api_base,
        "/account/cli/token",
        body={"code": code, "code_verifier": verifier, "redirect_uri": redirect_uri},
    )
    access_token = token_response.get("access_token")
    workspace_id = token_response.get("workspace_id")
    if not isinstance(access_token, str) or not isinstance(workspace_id, str):
        raise HostedCliApiError("Hosted API token response was missing access_token/workspace_id.")
    auth_payload = {
        "access_token": access_token,
        "token_type": token_response.get("token_type", "Bearer"),
        "expires_at": token_response.get("expires_at"),
        "workspace_id": workspace_id,
        "grant_id": token_response.get("grant_id"),
        "api_url": api_base,
        "app_url": app_base,
        "scopes": token_response.get("scopes", []),
    }
    _write_private_json(_hosted_auth_path(paths), auth_payload)
    _set_toml_bool(config_path, "hosted", "enabled", True)
    _set_toml_string(config_path, "hosted", "workspace_id", workspace_id)
    _set_toml_string(config_path, "hosted", "api_url", api_base)
    _set_toml_string(config_path, "hosted", "app_url", app_base)
    return auth_payload


def _hosted_import_sources(
    *,
    app_config: AppConfig,
    paths: ProjectPaths,
    descriptors: list[dict[str, Any]],
) -> dict[str, Any]:
    auth = _load_hosted_auth(paths)
    api_base = str(auth.get("api_url") or app_config.hosted.api_url or DEFAULT_HOSTED_API_URL)
    return _hosted_api_request_json(
        api_base,
        "/account/sources/import",
        body={"sources": descriptors},
        access_token=str(auth["access_token"]),
        timeout=60.0,
    )


def _channel_import_descriptor(channel: LibraryChannel, *, selected: bool) -> dict[str, Any]:
    return {
        "source_url": channel.source_url,
        "source_type": "channel",
        "channel_id": channel.channel_id,
        "display_name": channel.title or channel.handle or channel.channel_id,
        "selected": selected,
        "import_source": channel.import_source or "cli",
        "metadata": {"legacy_source": channel.source, "handle": channel.handle},
    }


def _library_source_import_descriptor(source: LibrarySource, *, selected: bool | None = None) -> dict[str, Any]:
    source_type = {
        "youtube_channel": "channel",
        "youtube_video": "video",
        "youtube_playlist": "playlist",
    }.get(source.source_type, "url")
    descriptor: dict[str, Any] = {
        "source_url": source.source_url,
        "source_type": source_type,
        "display_name": source.title or source.handle or source.video_id or source.channel_id,
        "selected": source.selected if selected is None else selected,
        "import_source": source.import_source or "cli",
        "metadata": {"legacy_source": source.source, "handle": source.handle},
    }
    if source.channel_id:
        descriptor["channel_id"] = source.channel_id
    if source.video_id:
        descriptor["video_id"] = source.video_id
    return descriptor


def _run_hosted_setup_after_project_init(
    *,
    config: Path,
    channel: str | None,
    yes: bool,
    app_config: AppConfig,
    paths: ProjectPaths,
    env_path: Path,
) -> None:
    _step_header(2, HOSTED_SETUP_TOTAL_STEPS, "Hosted Yutome account")
    try:
        auth = _run_hosted_login_flow(config_path=config, app_url=app_config.hosted.app_url, api_url=app_config.hosted.api_url)
    except (HostedCliLoginError, HostedCliApiError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"[OK] Hosted CLI connected to workspace {auth['workspace_id']}.")

    app_config = apply_env_to_config(load_config(config))
    imported = 0
    _step_header(3, HOSTED_SETUP_TOTAL_STEPS, "YouTube sources")
    if channel:
        source = source_from_input(channel, import_source="cli")
        if source is None:
            typer.echo("[WARN] Could not parse the source passed to setup.")
        else:
            try:
                result = _hosted_import_sources(
                    app_config=app_config,
                    paths=paths,
                    descriptors=[_library_source_import_descriptor(source, selected=True)],
                )
            except (HostedCliLoginError, HostedCliApiError) as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=1) from exc
            imported = len(result.get("imported", []))
            typer.echo(f"[OK] Uploaded {imported} hosted source{'s' if imported != 1 else ''}.")
    elif not yes:
        channels: list[LibraryChannel] = []
        if typer.confirm("Import YouTube subscription channels from this machine now?", default=True):
            try:
                channels = _fetch_youtube_import_channels(
                    target=None,
                    app_config=app_config,
                    paths=paths,
                    project_root=_project_root(config),
                    env_path=env_path,
                    port=0,
                    open_browser=True,
                    status_callback=typer.echo,
                )
            except YouTubeImportError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=1) from exc
            if channels:
                try:
                    result = _hosted_import_sources(
                        app_config=app_config,
                        paths=paths,
                        descriptors=[_channel_import_descriptor(channel, selected=True) for channel in channels],
                    )
                except (HostedCliLoginError, HostedCliApiError) as exc:
                    typer.echo(str(exc), err=True)
                    raise typer.Exit(code=1) from exc
                imported = len(result.get("imported", []))
        if imported:
            typer.echo(f"[OK] Uploaded {imported} YouTube source{'s' if imported != 1 else ''} to hosted Yutome.")
        else:
            typer.echo("[OK] No hosted sources imported yet.")
    else:
        typer.echo("[OK] Hosted login saved. Import sources later with `yutome import-youtube --hosted`.")

    _step_header(4, HOSTED_SETUP_TOTAL_STEPS, "Next steps")
    typer.echo("  yutome import-youtube --hosted")
    typer.echo("  yutome hosted jobs")
    typer.echo("  https://app.getyutome.com/dashboard/connect")


def _setup_semantic_search(config_path: Path, env_path: Path, *, yes: bool) -> bool:
    app_config = load_config(config_path)
    has_voyage_key = _env_has_voyage_key(env_path)
    deps_ready = _module_available("lancedb") and _module_available("voyageai")

    if app_config.embeddings.enabled and has_voyage_key:
        _status(True, "Semantic/hybrid search", "enabled with Voyage embeddings")
        return True

    if yes:
        if has_voyage_key:
            _set_toml_bool(config_path, "embeddings", "enabled", True)
            _status(True, "Semantic/hybrid search", "enabled with existing VOYAGE_API_KEY")
            return True
        _status(
            False,
            "Semantic/hybrid search",
            "not configured; add VOYAGE_API_KEY to .env, then run `yutome setup`",
        )
        return False

    typer.echo(
        "Semantic/hybrid search lets yutome find paraphrases and concepts, not just "
        "exact words. It uses Voyage embeddings during sync."
    )
    _callout(
        "If you skip, lexical (keyword) search still works fully — you can enable "
        "semantic later with `yutome setup`."
    )
    typer.echo("")
    typer.echo(f"  Sign up:  {_styled_url('https://www.voyageai.com/')}")
    typer.echo("  Cost:     free tier covers small/medium libraries; pay-as-you-go past that")
    typer.echo(f"  Docs:     {_styled_url('https://docs.voyageai.com/docs/embeddings')}")
    typer.echo("")
    if not deps_ready:
        _status(False, "Semantic search dependencies", "run `uv sync` or reinstall yutome")
    if not setup_prompts.confirm("Enable semantic/hybrid search now?", default=False):
        _status_skip("Semantic/hybrid search", "lexical search still works")
        return False

    if not has_voyage_key:
        setup_prompts.offer_to_open(
            "https://www.voyageai.com/",
            prompt="Open the Voyage signup page in your browser?",
        )
        voyage_key = setup_prompts.password("Voyage API key")
        if voyage_key:
            _merge_env_values(env_path, {"VOYAGE_API_KEY": voyage_key})
            has_voyage_key = True
    _set_toml_bool(config_path, "embeddings", "enabled", True)
    if has_voyage_key:
        _status(True, "Semantic/hybrid search", "enabled; vectors will build during sync")
    else:
        _status(False, "Semantic/hybrid search", "enabled in config but missing VOYAGE_API_KEY")
    return has_voyage_key


def _setup_webshare(env_path: Path, *, yes: bool) -> None:
    values = _read_env_values(env_path)
    if values.get("YUTOME_WEBSHARE_USERNAME") and values.get("YUTOME_WEBSHARE_PASSWORD"):
        _status(True, "Webshare residential proxy", "configured in .env")
        return
    if yes:
        _status(
            False,
            "Webshare residential proxy",
            "not configured; add YUTOME_WEBSHARE_USERNAME and YUTOME_WEBSHARE_PASSWORD to .env",
        )
        return
    typer.echo(
        "Webshare is a paid residential-proxy service that helps large YouTube imports "
        "avoid local IP blocks."
    )
    _callout(
        "Skip it for small tests; configure it before importing hundreds of videos. "
        "If you skip, yutome will ask again the first time YouTube blocks a fetch."
    )
    typer.echo("")
    typer.echo(f"  Sign up:  {_styled_url('https://www.webshare.io/residential-proxy')}")
    typer.echo("  Cost:     ~$3.50/month for ~1 GB residential traffic (usually plenty)")
    typer.echo("  Plan:     'Residential Proxy' (rotating)")
    _callout(
        "            NOT the cheaper 'Proxy Server' (datacenter) — YouTube blocks those",
        kind="warn",
    )
    typer.echo(f"  Why:      {_styled_url('https://github.com/maskys/yutome/blob/main/docs/proxy-strategy.md')}")
    typer.echo("")
    if not setup_prompts.confirm("Configure Webshare residential proxy now?", default=False):
        _status_skip("Webshare residential proxy", "yutome can ask again if YouTube blocks a fetch")
        return

    setup_prompts.offer_to_open(
        "https://www.webshare.io/residential-proxy",
        prompt="Open the Webshare signup page in your browser?",
    )
    username = setup_prompts.text("Webshare username")
    password = setup_prompts.password("Webshare password")
    domain_default = values.get("YUTOME_WEBSHARE_DOMAIN") or "p.webshare.io"
    port_default = values.get("YUTOME_WEBSHARE_PORT") or "80"
    domain = setup_prompts.text("Webshare domain", default=domain_default)
    port = setup_prompts.text("Webshare port", default=port_default)
    _merge_env_values(
        env_path,
        {
            "YUTOME_WEBSHARE_USERNAME": username,
            "YUTOME_WEBSHARE_PASSWORD": password,
            "YUTOME_WEBSHARE_DOMAIN": domain,
            "YUTOME_WEBSHARE_PORT": port,
        },
    )
    _status(True, "Webshare residential proxy", "saved to .env")


def _setup_gemini(config_path: Path, env_path: Path, *, yes: bool) -> None:
    app_config = load_config(config_path)
    has_key = _env_has_gemini_key(env_path)
    deps_ready = _module_available("google.genai")

    if app_config.gemini.enabled and has_key:
        _status(True, "Gemini (transcript repair + fallback)", "enabled with existing key")
        return

    if yes:
        if has_key:
            _set_toml_bool(config_path, "gemini", "enabled", True)
            _status(True, "Gemini (transcript repair + fallback)", "enabled with existing key")
            return
        _status(
            False,
            "Gemini (transcript repair + fallback)",
            "not configured; add GEMINI_API_KEY to .env, then run `yutome setup`",
        )
        return

    typer.echo(
        "Gemini does two jobs for yutome: it repairs noisy auto-captions into clean "
        "readable transcripts after each sync, and it transcribes videos directly when "
        "captions and ASR fail."
    )
    _callout(
        "If you skip, raw auto-captions are still fully searchable — just messier. "
        "You can enable Gemini later with `yutome setup`."
    )
    typer.echo("")
    typer.echo(f"  Sign up:  {_styled_url('https://aistudio.google.com/apikey')}  (Google account required)")
    typer.echo("  Cost:     free tier covers casual use; pay-as-you-go past the daily quota")
    typer.echo(f"  Docs:     {_styled_url('https://ai.google.dev/gemini-api/docs')}")
    typer.echo("")
    if not deps_ready:
        _status(False, "Gemini dependency", "run `uv sync` or reinstall yutome")
    if not setup_prompts.confirm("Enable Gemini transcript repair and fallback now?", default=False):
        _status_skip(
            "Gemini (transcript repair + fallback)",
            "yutome can ask again before repair/fallback runs",
        )
        return

    if not has_key:
        setup_prompts.offer_to_open(
            "https://aistudio.google.com/apikey",
            prompt="Open AI Studio (Google) to create an API key?",
        )
        gemini_key = setup_prompts.password("Gemini API key")
        if gemini_key:
            _merge_env_values(env_path, {"GEMINI_API_KEY": gemini_key})
            has_key = True
    _set_toml_bool(config_path, "gemini", "enabled", True)
    _set_toml_bool(config_path, "gemini", "fallback_enabled", True)
    if has_key:
        _status(True, "Gemini (transcript repair + fallback)", "enabled; repair runs after sync")
    else:
        _status(False, "Gemini (transcript repair + fallback)", "enabled in config but missing GEMINI_API_KEY")


def _add_setup_source(config: Path, target: str) -> LibrarySource | None:
    app_config = load_config(config)
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    bootstrap_catalog(paths.catalog_db)
    source = source_from_input(target, import_source="setup")
    if source is None:
        return None
    with connect_catalog(paths.catalog_db) as connection:
        upsert_library_source(connection, source, selected=True)
        connection.commit()
    return source


def _channel_picker_name(channel: LibraryChannel) -> str:
    return channel.title or channel.handle or channel.source_url or "Untitled channel"


def _channel_picker_labels(channels: list[LibraryChannel]) -> dict[int, str]:
    base_names = [_channel_picker_name(channel) for channel in channels]
    counts = Counter(base_names)
    seen: Counter[str] = Counter()
    labels: dict[int, str] = {}
    for index, (channel, base_name) in enumerate(zip(channels, base_names, strict=True)):
        if counts[base_name] == 1:
            labels[index] = base_name
            continue
        seen[base_name] += 1
        suffix = channel.handle if channel.handle and channel.handle != base_name else f"duplicate {seen[base_name]}"
        labels[index] = f"{base_name} - {suffix}"
    return labels


def _sorted_picker_channels(channels: list[LibraryChannel]) -> list[LibraryChannel]:
    return sorted(channels, key=lambda channel: (channel.title or channel.handle or channel.source_url).lower())


def _parse_channel_selection(raw: str, channel_count: int) -> set[int]:
    value = raw.strip().lower()
    if value in {"", "none", "skip"}:
        return set()
    if value == "all":
        return set(range(channel_count))
    selected: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = [piece.strip() for piece in part.split("-", 1)]
            if not left.isdigit() or not right.isdigit():
                raise ValueError(f"Invalid range: {part}")
            start = int(left)
            end = int(right)
            if start > end:
                raise ValueError(f"Invalid descending range: {part}")
            indexes = range(start, end + 1)
        else:
            if not part.isdigit():
                raise ValueError(f"Invalid selection: {part}")
            indexes = range(int(part), int(part) + 1)
        for index in indexes:
            if index < 1 or index > channel_count:
                raise ValueError(f"Selection out of range: {index}")
            selected.add(index - 1)
    return selected


def _display_channel_picker(channels: list[LibraryChannel], *, title: str, query: str | None = None) -> None:
    lowered_query = query.lower() if query else None
    labels = _channel_picker_labels(channels)
    visible: list[tuple[int, LibraryChannel]] = []
    for index, channel in enumerate(channels):
        searchable = " ".join(
            piece
            for piece in (
                labels[index],
                channel.handle,
                channel.source_url,
            )
            if piece
        )
        if lowered_query and lowered_query not in searchable.lower():
            continue
        visible.append((index, channel))

    typer.echo("")
    typer.echo(title)
    for index, channel in visible[:30]:
        typer.echo(f"  {index + 1:>3}. {labels[index]}")
    if len(visible) > 30:
        typer.echo(f"  ... {len(visible) - 30} more match; use /search text to narrow.")
    if query and not visible:
        typer.echo(f"  No matches for: {query}")
    typer.echo("Enter numbers/ranges like 1,3,8-12, or all, none, /search text.")


def _prompt_channels_to_select(
    channels: list[LibraryChannel],
    *,
    title: str = "Choose channels:",
    prompt: str = "Select",
    default: str = "none",
    allow_back: bool = False,
) -> list[LibraryChannel] | None:
    ordered = _sorted_picker_channels(channels)
    labels = _channel_picker_labels(ordered)
    if setup_prompts.is_interactive():
        typer.clear()
        typer.echo(f"Found {len(ordered)} channel{'s' if len(ordered) != 1 else ''}.")
        typer.echo("Type to filter; press space to select; press enter to confirm.")
        if allow_back:
            typer.echo("Choose Back to return to the previous step.")
        typer.echo("")
        default_indexes = _parse_channel_selection(default, len(ordered))
        select_all_label = "All channels"
        back_label = BACK_CHOICE
        label_to_index = {label: index for index, label in labels.items()}
        default_labels = (
            [select_all_label]
            if len(default_indexes) == len(ordered) and ordered
            else [labels[index] for index in sorted(default_indexes)]
        )
        choices = [select_all_label, *labels.values()]
        if allow_back:
            choices.append(back_label)
        empty_selection_message = (
            "Select at least one channel, All channels, or Back."
            if allow_back
            else "Select at least one channel or All channels."
        )
        selected_labels = setup_prompts.checkbox(
            title,
            choices=choices,
            defaults=default_labels,
            instruction=(
                "Use arrows to move, space to select, type to search, enter to confirm. "
                "Choose 'All channels' to select everything."
            ),
            use_search_filter=True,
            erase_when_done=True,
            validate=lambda selected: bool(selected) or empty_selection_message,
        )
        if allow_back and back_label in selected_labels:
            return None
        if select_all_label in selected_labels:
            return ordered
        return [ordered[label_to_index[label]] for label in selected_labels if label in label_to_index]

    query: str | None = None
    while True:
        _display_channel_picker(ordered, title=title, query=query)
        raw = typer.prompt(prompt, default=default).strip()
        if raw.startswith("/"):
            query = raw[1:].strip() or None
            continue
        try:
            indexes = _parse_channel_selection(raw, len(ordered))
        except ValueError as exc:
            typer.echo(f"[WARN] {exc}")
            continue
        return [ordered[index] for index in sorted(indexes)]


_OAUTH_CONSOLE_STEPS: tuple[tuple[str, str | None, tuple[str, ...]], ...] = (
    (
        "Google Cloud project",
        "https://console.cloud.google.com/",
        ("Create a new project or pick an existing one.",),
    ),
    (
        "Enable the YouTube Data API",
        "https://console.cloud.google.com/apis/library/youtube.googleapis.com",
        ("Click ENABLE on the YouTube Data API v3 page.",),
    ),
    (
        "OAuth consent screen",
        "https://console.cloud.google.com/apis/credentials/consent",
        (
            "User type: External",
            "Add the scope: .../auth/youtube.readonly",
            "Add your own Gmail under 'Test users'.",
            "⚠ This last one is required while the app is in 'Testing' — easy to miss.",
        ),
    ),
    (
        "Credentials → OAuth client ID",
        "https://console.cloud.google.com/apis/credentials",
        ("Create Credentials → OAuth client ID → Application type: Desktop app.",),
    ),
    (
        "Download the JSON",
        None,
        ("Click DOWNLOAD JSON on the new client. Save it somewhere private (e.g. ~/.yutome/).",),
    ),
)


def _prompt_oauth_client_secrets(env_path: Path) -> bool:
    """Walk the user through creating a Google OAuth Desktop client and storing its JSON path.

    Interactive callers see the six steps one at a time, gated on press-Enter
    so they can complete each step before reading the next. Non-interactive
    callers (scripted setup, CI) get the legacy dump-everything-at-once
    behaviour so test inputs don't have to step through gates.

    Returns True when the YUTOME_YOUTUBE_OAUTH_CLIENT_SECRETS env value is set.
    """
    walkthrough = "https://github.com/MaskyS/yutome/blob/main/docs/oauth-testing.md"
    if setup_prompts.is_interactive():
        typer.echo("")
        typer.echo(
            "You picked OAuth, so we need to register yutome as a Desktop OAuth "
            "client inside your Google Cloud project. One-time per Google account; "
            "expect 5–10 minutes the first time."
        )
        typer.echo(f"Screenshots walkthrough: {_styled_url(walkthrough)}")
        typer.echo("")
        for index, (title, url, lines) in enumerate(_OAUTH_CONSOLE_STEPS, 1):
            typer.secho(f"  Step {index} of 6 · {title}", fg="cyan", bold=True)
            if url:
                typer.echo(f"    Open: {_styled_url(url)}")
            for line in lines:
                typer.echo(f"    {line}")
            setup_prompts.press_any_key("    Press Enter when this step is done …")
            typer.echo("")
        typer.secho("  Step 6 of 6 · Paste the absolute path to the downloaded JSON", fg="cyan", bold=True)
    else:
        typer.echo("")
        typer.echo(
            "You picked OAuth, so we need to register yutome as a Desktop OAuth client "
            "inside your Google Cloud project. This is a one-time thing per Google account "
            "— yutome reuses the client every time. Expect 5–10 minutes the first time you "
            "use Google Cloud Console; faster if you've done it before."
        )
        typer.echo("")
        typer.echo("Friendlier walk-through with screenshots:")
        typer.echo(f"  {walkthrough}")
        typer.echo("")
        typer.echo("The six steps (open each URL in your browser):")
        for index, (title, url, lines) in enumerate(_OAUTH_CONSOLE_STEPS, 1):
            url_part = f"   {url}" if url else ""
            typer.echo(f"  {index}. {title}{url_part}")
            for line in lines:
                typer.echo(f"     {line}")
        typer.echo("  6. Paste the absolute path to that JSON below.")
    client_secret_path = typer.prompt(
        "OAuth client secrets JSON path (blank to skip)",
        default="",
        show_default=False,
    ).strip()
    if not client_secret_path:
        return False
    _merge_env_values(env_path, {"YUTOME_YOUTUBE_OAUTH_CLIENT_SECRETS": client_secret_path})
    os.environ["YUTOME_YOUTUBE_OAUTH_CLIENT_SECRETS"] = client_secret_path
    return True


def _prompt_public_subscription_target() -> str | None:
    back_values = {"b", "back"}
    question = (
        "Also pull in another YouTube channel's public subscriptions? "
        "(You can stack several together — yours plus theirs — into one combined list to pick from.)"
    )
    if setup_prompts.is_interactive():
        add_choice = "Yes - stack another channel's public subscriptions"
        skip_choice = "No - move on to picking channels"
        back_choice = "Back - choose a different import method"
        choice = setup_prompts.select(
            question,
            choices=[skip_choice, add_choice, back_choice],
            default=skip_choice,
        )
        if choice == back_choice:
            return BACK_CHOICE
        if choice == skip_choice:
            return None
        raw = setup_prompts.text("Public channel URL, handle, or channel id (blank to skip, b to go back)")
    else:
        if not setup_prompts.confirm(question, default=False):
            return None
        raw = setup_prompts.text("Public channel URL, handle, or channel id")

    target = raw.strip()
    if target.lower() in back_values:
        return BACK_CHOICE
    return target or None


def _setup_import_youtube_subscriptions(
    *,
    config: Path,
    app_config: AppConfig,
    paths: ProjectPaths,
    env_path: Path,
) -> list[LibraryChannel]:
    imported: list[LibraryChannel] = []
    project_root = _project_root(config)
    typer.echo("")
    typer.echo(
        "Yutome can pull your YouTube subscription list so you don't have to add channels "
        "one by one. It only reads the list of channels you subscribe to — not your watch "
        "history, not your private playlists, not any video data. The result is just a set "
        "of channels added to your library; nothing is sent anywhere."
    )
    typer.echo("")
    typer.echo("Two ways to get the list:")
    typer.echo("")
    typer.echo("  Browser cookies (recommended)")
    typer.echo("    Reads the YouTube account already logged into Chrome / Brave / Safari /")
    typer.echo("    Firefox / Edge on this computer. Zero setup. Whichever Google account")
    typer.echo("    is signed into that browser is the one we read.")
    typer.secho(
        "    Heads up: macOS will ask for your login password or Touch ID once to unlock",
        bold=True,
    )
    typer.secho(
        "    the cookie store — that prompt comes from macOS, not Yutome.",
        bold=True,
    )
    typer.echo("")
    typer.echo("  Google OAuth")
    typer.echo("    You pick a specific Google account during a browser sign-in flow. Best")
    typer.echo("    when you have multiple Google accounts and want to be explicit, or when")
    typer.echo("    cookies don't work. One-time ~5-min Google Cloud Console setup.")
    typer.echo("")
    method_choices = [
        "Browser cookies (recommended)",
        "Google OAuth (pick a specific account)",
        "Skip - I'll add channels manually with `yutome add`",
    ]
    while True:
        method = setup_prompts.select(
            "How do you want to import your subscriptions?",
            choices=method_choices,
            default="Browser cookies (recommended)",
        )
        imported = []
        if method.startswith("Skip"):
            break
        use_oauth = method.startswith("Google OAuth")
        if use_oauth:
            if _configured_oauth_client_secrets(app_config, project_root, env_path) is None:
                if _prompt_oauth_client_secrets(env_path):
                    app_config = apply_env_to_config(app_config)
            if _configured_oauth_client_secrets(app_config, project_root, env_path) is None:
                typer.echo("[WARN] OAuth subscription import skipped: no client secrets provided.")
            else:
                try:
                    imported.extend(
                        _fetch_youtube_import_channels(
                            target=None,
                            app_config=app_config,
                            paths=paths,
                            project_root=project_root,
                            env_path=env_path,
                            status_callback=typer.echo,
                        )
                    )
                except YouTubeImportError as oauth_exc:
                    typer.echo(f"[WARN] OAuth subscription import skipped: {oauth_exc}")
        else:
            try:
                with _spinner(
                    "Reading your YouTube subscriptions from the browser cookie store… "
                    "macOS may ask for your password or Touch ID once."
                ):
                    imported.extend(
                        _fetch_youtube_import_channels(
                            target=None,
                            app_config=app_config,
                            paths=paths,
                            project_root=project_root,
                            env_path=env_path,
                            status_callback=None,
                        )
                    )
            except YouTubeImportError as exc:
                typer.echo(f"[WARN] Browser-cookie subscription import did not work: {exc}")
                if _configured_oauth_client_secrets(app_config, project_root, env_path) is None:
                    if _prompt_oauth_client_secrets(env_path):
                        app_config = apply_env_to_config(app_config)
                if _configured_oauth_client_secrets(app_config, project_root, env_path) is not None:
                    try:
                        imported.extend(
                            _fetch_youtube_import_channels(
                                target=None,
                                app_config=app_config,
                                paths=paths,
                                project_root=project_root,
                                env_path=env_path,
                                status_callback=typer.echo,
                            )
                        )
                    except YouTubeImportError as oauth_exc:
                        typer.echo(f"[WARN] YouTube OAuth subscription import skipped: {oauth_exc}")

        if imported:
            typer.echo(
                f"Found {len(imported)} subscription channel"
                f"{'s' if len(imported) != 1 else ''} from your YouTube account."
            )
        public_target = _prompt_public_subscription_target()
        if public_target == BACK_CHOICE:
            continue
        if public_target:
            try:
                with _spinner(f"Fetching public subscriptions for {public_target}…"):
                    imported.extend(
                        _fetch_youtube_import_channels(
                            target=public_target,
                            app_config=app_config,
                            paths=paths,
                            project_root=project_root,
                            env_path=env_path,
                            status_callback=typer.echo,
                        )
                    )
            except YouTubeImportError as exc:
                typer.echo(f"[WARN] Public subscription import skipped: {exc}")
        break

    if not imported:
        return []
    typer.echo(
        f"Found {len(imported)} subscription channel{'s' if len(imported) != 1 else ''}. "
        "If this looks like the wrong YouTube account, choose Back and use OAuth instead."
    )
    while True:
        selected_channels = _prompt_channels_to_select(
            imported,
            title="Choose channels to add to the library:",
            prompt="Add to library",
            default="none",
            allow_back=True,
        )
        if selected_channels is not None:
            break
        method = setup_prompts.select(
            "How do you want to import your subscriptions?",
            choices=["Google OAuth (pick a specific account)", "Skip - I'll add channels manually with `yutome add`"],
            default="Google OAuth (pick a specific account)",
        )
        if method.startswith("Skip"):
            return []
        if _configured_oauth_client_secrets(app_config, project_root, env_path) is None:
            if _prompt_oauth_client_secrets(env_path):
                app_config = apply_env_to_config(app_config)
        if _configured_oauth_client_secrets(app_config, project_root, env_path) is None:
            typer.echo("[WARN] OAuth subscription import skipped: no client secrets provided.")
            return []
        try:
            imported = _fetch_youtube_import_channels(
                target=None,
                app_config=app_config,
                paths=paths,
                project_root=project_root,
                env_path=env_path,
                status_callback=typer.echo,
            )
        except YouTubeImportError as oauth_exc:
            typer.echo(f"[WARN] OAuth subscription import skipped: {oauth_exc}")
            return []
        typer.echo(f"Found {len(imported)} subscription channel{'s' if len(imported) != 1 else ''}.")
    selected_count = _save_imported_channels(paths, selected_channels, selected=True)
    typer.echo(
        f"[OK] Added {selected_count} selected channel{'s' if selected_count != 1 else ''} "
        f"to the library; skipped {max(0, len(imported) - selected_count)}."
    )
    return selected_channels


def _run_sync_targets(
    *,
    app_config: AppConfig,
    paths: ProjectPaths,
    sync_targets: list[tuple[str, str | None, str]],
    use_catalog: bool,
    limit: int | None,
    effective_embed: bool,
    force: bool,
    effective_max_process: int | None,
    retry_failed: bool,
    stop_on_rate_limit: bool,
    verbose_skips: bool,
    effective_workers: int,
    asr_fallback: bool,
    gemini_fallback: bool,
    sleep_seconds: float,
    status_filter: list[str] | None,
    source_filter: list[str] | None,
    max_duration_seconds: int | None,
    shortest_first: bool,
) -> None:
    typer.echo("Import plan:")
    typer.echo(f"  targets: {len(sync_targets)}")
    target_types = sorted({target_type for _, _, target_type in sync_targets})
    typer.echo(f"  source types: {', '.join(target_types)}")
    typer.echo(f"  discovery: {'catalog cache' if use_catalog else 'source-specific'}")
    typer.echo(f"  max-process: {effective_max_process if effective_max_process is not None else 'unlimited'}")
    typer.echo(f"  workers: {effective_workers}")
    typer.echo(f"  staged fallback: transcript API first, yt-dlp retry second, metadata backfill third")
    typer.echo(f"  embeddings: {'enabled' if effective_embed else 'disabled'}")
    typer.echo(f"  retry failed/deferred: {retry_failed}")
    typer.echo("")

    totals: dict[str, Any] = {
        "discovered": 0,
        "processed": 0,
        "metadata_saved": 0,
        "metadata_failed": 0,
        "transcripts_saved": 0,
        "chunks_saved": 0,
        "skipped_existing": 0,
        "skipped_failed": 0,
        "deferred": 0,
        "failed": 0,
        "embedded_chunks": 0,
        "cleanup_scanned": 0,
        "cleanup_upgraded": 0,
        "cleanup_skipped_unchanged": 0,
        "cleanup_skipped_missing": 0,
        "cleanup_skipped_quality": 0,
        "cleanup_failed": 0,
        "cleanup_chunks_saved": 0,
        "elapsed_seconds": 0.0,
        "stopped_early": False,
        "embedding_messages": [],
    }
    for sync_target, label, target_type in sync_targets:
        if len(sync_targets) > 1:
            typer.echo("")
            typer.echo(f"Syncing {label or sync_target}")
        if target_type == "youtube_playlist":
            typer.echo(f"Skipping playlist source; playlist sync is not supported yet: {sync_target}")
            continue
        if target_type == "youtube_video":
            stats = sync_video(
                target=sync_target,
                config=app_config,
                paths=paths,
                embed=effective_embed,
                sleep_seconds=sleep_seconds,
                force=force,
                asr_fallback=asr_fallback,
                gemini_fallback=gemini_fallback,
                retry_failed=retry_failed,
                stop_on_rate_limit=stop_on_rate_limit,
                verbose_skips=verbose_skips,
                workers=effective_workers,
                status_filters=status_filter,
                source_filters=source_filter,
                max_duration_seconds=max_duration_seconds,
                progress=typer.echo,
            )
        else:
            stats = sync_channel(
                target=sync_target,
                config=app_config,
                paths=paths,
                limit=limit,
                embed=effective_embed,
                sleep_seconds=sleep_seconds,
                force=force,
                asr_fallback=asr_fallback,
                gemini_fallback=gemini_fallback,
                max_process=effective_max_process,
                retry_failed=retry_failed,
                stop_on_rate_limit=stop_on_rate_limit,
                refresh_discovery=not use_catalog,
                verbose_skips=verbose_skips,
                workers=effective_workers,
                status_filters=status_filter,
                source_filters=source_filter,
                max_duration_seconds=max_duration_seconds,
                shortest_first=shortest_first,
                progress=typer.echo,
            )
        for field in (
            "discovered",
            "processed",
            "metadata_saved",
            "metadata_failed",
            "transcripts_saved",
            "chunks_saved",
            "skipped_existing",
            "skipped_failed",
            "deferred",
            "failed",
            "embedded_chunks",
            "cleanup_scanned",
            "cleanup_upgraded",
            "cleanup_skipped_unchanged",
            "cleanup_skipped_missing",
            "cleanup_skipped_quality",
            "cleanup_failed",
            "cleanup_chunks_saved",
        ):
            totals[field] += getattr(stats, field)
        totals["elapsed_seconds"] += stats.elapsed_seconds
        totals["stopped_early"] = bool(totals["stopped_early"] or stats.stopped_early)
        if stats.embedding_message:
            totals["embedding_messages"].append(stats.embedding_message)
        if stats.stopped_early and stop_on_rate_limit:
            break

    typer.echo(f"Discovered videos: {totals['discovered']}")
    typer.echo(f"Processed this run: {totals['processed']}")
    typer.echo(f"Metadata saved: {totals['metadata_saved']}")
    typer.echo(f"Metadata failed: {totals['metadata_failed']}")
    typer.echo(f"Transcripts saved: {totals['transcripts_saved']}")
    typer.echo(f"Chunks saved: {totals['chunks_saved']}")
    typer.echo(f"Skipped existing: {totals['skipped_existing']}")
    typer.echo(f"Skipped failed/deferred: {totals['skipped_failed']}")
    typer.echo(f"Deferred videos: {totals['deferred']}")
    typer.echo(f"Failed videos: {totals['failed']}")
    typer.echo(f"Cleanup scanned: {totals['cleanup_scanned']}")
    typer.echo(f"Cleanup upgraded: {totals['cleanup_upgraded']}")
    typer.echo(f"Cleanup skipped by heuristic: {totals['cleanup_skipped_quality']}")
    typer.echo(f"Cleanup failed: {totals['cleanup_failed']}")
    typer.echo(f"Cleanup chunks saved: {totals['cleanup_chunks_saved']}")
    typer.echo(f"Embedded chunks: {totals['embedded_chunks']}")
    for message in totals["embedding_messages"]:
        typer.echo(f"Embedding note: {message}")
    typer.echo(f"Elapsed seconds: {totals['elapsed_seconds']:.1f}")
    throughput = 0.0
    if totals["elapsed_seconds"] > 0:
        throughput = totals["transcripts_saved"] / (totals["elapsed_seconds"] / 60)
    typer.echo(f"Transcript throughput: {throughput:.2f} videos/min")
    typer.echo(f"Stopped early: {totals['stopped_early']}")


def _first_run_default_selection(channels: list[LibraryChannel]) -> str:
    if len(channels) <= 10:
        return "all"
    return "1-10"


def _run_setup_first_sync(
    config: Path,
    *,
    channels: list[LibraryChannel] | None = None,
    max_videos_per_channel: int | None,
) -> None:
    load_dotenv(_project_root(config) / ".env")
    app_config = apply_env_to_config(load_config(config))
    if app_config.embeddings.enabled:
        app_config = app_config.model_copy(
            update={"embeddings": app_config.embeddings.model_copy(update={"enabled": True})}
        )
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    bootstrap_catalog(paths.catalog_db)
    if channels is None:
        with connect_catalog(paths.catalog_db) as connection:
            selected_channels = list_library_channels(connection, selected_only=True)
    else:
        selected_channels = channels
    if not selected_channels:
        typer.echo("[WARN] No selected sources to index.")
        return
    effective_workers = app_config.backfill.workers
    effective_max_process: int | None = max_videos_per_channel
    if effective_max_process is None:
        typer.echo(
            f"First sync upper bound: {len(selected_channels)} channel(s) x "
            f"all available videos; workers: {effective_workers}."
        )
    else:
        upper_bound = len(selected_channels) * effective_max_process
        typer.echo(
            f"First sync upper bound: {len(selected_channels)} channel(s) x "
            f"{effective_max_process} videos = {upper_bound} videos; workers: {effective_workers}."
        )
    _run_sync_targets(
        app_config=app_config,
        paths=paths,
        sync_targets=[
            (channel.source_url, channel.title or channel.handle or channel.channel_id, "youtube_channel")
            for channel in selected_channels
        ],
        use_catalog=False,
        limit=None,
        effective_embed=app_config.embeddings.enabled,
        force=False,
        effective_max_process=effective_max_process,
        retry_failed=False,
        stop_on_rate_limit=False,
        verbose_skips=False,
        effective_workers=effective_workers,
        asr_fallback=False,
        gemini_fallback=False,
        sleep_seconds=0.0,
        status_filter=None,
        source_filter=None,
        max_duration_seconds=None,
        shortest_first=False,
    )


def _remote_mode_from_option(mode: str) -> RemoteMode:
    normalized = mode.strip().lower().replace("-", "_")
    if normalized not in {"connector_only", "replica"}:
        raise typer.BadParameter("mode must be 'connector-only' or 'replica'")
    return normalized  # type: ignore[return-value]


def _prepare_connect_project(config: Path) -> ProjectPaths:
    write_default_config(config)
    project_root = _project_root(config)
    _write_env_template(project_root / ".env")
    paths = _load_paths(config)
    paths.ensure_base_dirs()
    bootstrap_catalog(paths.catalog_db)
    return paths


def _print_cloudflare_connect_instructions() -> None:
    typer.echo("Yutome remote MCP uses one public endpoint for Claude and ChatGPT.")
    typer.echo("")
    typer.echo("For now this uses a small Cloudflare Worker as the public connector endpoint.")
    typer.echo("Claude and ChatGPT call this URL; Yutome answers from this computer while the bridge is running.")
    typer.echo("If Yutome or your team already provides that endpoint, save it with:")
    typer.echo("  yutome connect --endpoint https://your-worker.example.workers.dev --relay-token <token> --pairing-code <code>")
    typer.echo("If not, yutome can prepare the Cloudflare Worker and try to deploy it from this computer.")
    typer.echo("You may need to create or sign into a Cloudflare account during deploy.")
    typer.echo("")
    typer.echo("The endpoint can be a base Worker URL or the full /mcp URL.")
    typer.echo("Remote MCP mode does not require Voyage, Webshare, Gemini, or proxy credentials.")
    typer.echo("The basic laptop-backed connector is designed for Cloudflare's free Workers plan.")
    typer.echo("Always-on/offline search is a later mode and may require enabling Cloudflare billing.")


def _cloudflare_deploy_tools() -> dict[str, str | None]:
    return {
        "node": shutil.which("node"),
        "npm": shutil.which("npm"),
        "npx": shutil.which("npx"),
    }


def _parse_node_version(raw: str) -> tuple[int, int, int] | None:
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", raw)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _node_version() -> tuple[tuple[int, int, int] | None, str]:
    node_path = shutil.which("node")
    if node_path is None:
        return None, "not found"
    try:
        result = subprocess.run(
            [node_path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{node_path} failed: {exc}"
    detail = (result.stdout or result.stderr or "").strip() or f"exit code {result.returncode}"
    if result.returncode != 0:
        return None, f"{node_path} failed: {detail}"
    return _parse_node_version(detail), f"{node_path} ({detail})"


def _cloudflare_deploy_runtime_problem() -> str | None:
    missing = _missing_cloudflare_deploy_tools()
    if missing:
        return f"Missing: {', '.join(missing)}"
    version, detail = _node_version()
    required = ".".join(str(part) for part in CLOUDFLARE_MIN_NODE_VERSION)
    if version is None:
        return f"Could not determine Node.js version from {detail}."
    if version < CLOUDFLARE_MIN_NODE_VERSION:
        return f"Node.js {required}+ is required by Wrangler; found {detail}."
    return None


def _require_cloudflare_deploy_runtime() -> None:
    problem = _cloudflare_deploy_runtime_problem()
    if problem is None:
        return
    typer.echo("Cannot deploy the Cloudflare Worker from this computer yet.", err=True)
    typer.echo(problem, err=True)
    typer.echo(f"Install Node.js 22 LTS or newer from {NODE_DOWNLOAD_URL}, then rerun `yutome connect --deploy`.", err=True)
    typer.echo("If you use Homebrew: `brew install node@22` and make sure that `node --version` prints v22+.", err=True)
    raise typer.Exit(code=1)


def _can_run_cloudflare_deploy() -> bool:
    return _cloudflare_deploy_runtime_problem() is None


def _missing_cloudflare_deploy_tools() -> list[str]:
    tools = _cloudflare_deploy_tools()
    return [name for name in ("node", "npm", "npx") if not tools[name]]


def _assistant_app_targets(value: str | None = None) -> set[str]:
    raw = (value or "all").strip().lower()
    pieces = [piece.strip() for piece in re.split(r"[,/ ]+", raw) if piece.strip()]
    if not pieces:
        return {"claude", "chatgpt", "other"}
    targets: set[str] = set()
    aliases = {
        "c": "claude",
        "claude": "claude",
        "anthropic": "claude",
        "g": "chatgpt",
        "gpt": "chatgpt",
        "chat": "chatgpt",
        "chatgpt": "chatgpt",
        "openai": "chatgpt",
        "oai": "chatgpt",
        "other": "other",
        "mcp": "other",
    }
    for piece in pieces:
        if piece in {"all", "any"}:
            targets.update({"claude", "chatgpt", "other"})
            continue
        if piece in {"both", "claude+chatgpt", "claude-chatgpt"}:
            targets.update({"claude", "chatgpt"})
            continue
        target = aliases.get(piece)
        if target is None:
            raise ValueError("assistant app must be one of: claude, chatgpt, both, other, all")
        targets.add(target)
    return targets or {"claude", "chatgpt", "other"}


def _assistant_app_label(targets: set[str]) -> str:
    labels = []
    if "claude" in targets:
        labels.append("Claude")
    if "chatgpt" in targets:
        labels.append("ChatGPT")
    if "other" in targets:
        labels.append("other MCP clients")
    return ", ".join(labels)


def _prompt_first_run_video_cap(default_cap: int) -> int | None:
    """Ask how many recent videos per channel to index on the first sync.

    Interactive callers get a four-choice questionary picker ("Custom number"
    falls through to a number prompt). Non-TTY callers (CliRunner, scripted
    setup) keep the legacy free-text typer.prompt so the existing tests'
    numeric input (``"50\\n"``, ``"all\\n"``) still works untouched.
    """
    typer.echo("")
    typer.echo("How many recent videos per channel should Yutome index first?")
    if setup_prompts.is_interactive():
        quick_value = 10
        all_value = "all"
        custom_value = "custom"
        choices = [
            (f"Quick try · {quick_value} videos per channel", quick_value),
            (f"Default · {default_cap} videos per channel", default_cap),
            (f"All available · slow, large library", all_value),
            ("Custom number…", custom_value),
        ]
        chosen = setup_prompts.select(
            "Per channel",
            choices=choices,
            default=default_cap,
        )
        if chosen == all_value:
            return None
        if chosen == custom_value:
            while True:
                raw = setup_prompts.text("Videos per channel", default=str(default_cap)).strip().lower()
                if raw in {"all", "everything", "unlimited"}:
                    return None
                try:
                    value = int(raw)
                except ValueError:
                    typer.echo("[WARN] Enter a number, or 'all'.")
                    continue
                if value < 1:
                    typer.echo("[WARN] Enter a positive number, or 'all'.")
                    continue
                return value
        return chosen  # int
    # Non-TTY: legacy free-text prompt — kept verbatim so existing tests pass.
    typer.echo(f"  10    quick try")
    typer.echo(f"  {default_cap}    default")
    typer.echo("  all   everything available (slow, large)")
    typer.echo("  N     type any custom number")
    while True:
        raw = typer.prompt("Per channel", default=str(default_cap)).strip().lower()
        if raw in {"all", "everything", "unlimited"}:
            return None
        try:
            value = int(raw)
        except ValueError:
            typer.echo("[WARN] Enter a number, or 'all'.")
            continue
        if value < 1:
            typer.echo("[WARN] Enter a positive number, or 'all'.")
            continue
        return value


def _prompt_assistant_apps(*, allow_back: bool = False) -> str | None:
    label_to_value = {
        "Claude (web, Desktop, mobile)": "claude",
        "ChatGPT": "chatgpt",
        "Both Claude and ChatGPT": "both",
        "Another remote MCP client": "other",
    }
    choices = list(label_to_value.keys())
    if allow_back:
        choices.append(BACK_CHOICE)
    choice = setup_prompts.select(
        "Which assistant app do you want connector instructions for?",
        choices=choices,
        default="Claude (web, Desktop, mobile)",
    )
    if allow_back and choice == BACK_CHOICE:
        return None
    return label_to_value[choice]


def _print_connector_next_steps(
    mcp_url: str,
    *,
    bridge_configured: bool = True,
    assistant_apps: str | None = None,
) -> None:
    targets = _assistant_app_targets(assistant_apps)
    typer.echo("")
    typer.echo(f"Connect this MCP URL in {_assistant_app_label(targets)}:")
    typer.echo(f"  {mcp_url}")
    typer.echo("")
    typer.echo("Use this one URL for Claude/ChatGPT across your devices. You do not need")
    typer.echo("a new Yutome endpoint for every phone, laptop, or tablet.")
    typer.echo("Paste this exact /mcp URL into the assistant. Do not paste /authorize or /pair;")
    typer.echo("the assistant opens those OAuth pages itself.")
    typer.echo("If an old Yutome connector already exists with a different URL, remove it first")
    typer.echo("and add this one again.")
    typer.echo("")
    if bridge_configured:
        typer.echo("The laptop bridge keeps a WebSocket open to the Worker so Claude/ChatGPT can")
        typer.echo("reach the local corpus. `yutome connect --deploy` auto-starts it in the background.")
        typer.echo("")
        typer.echo("Bridge controls:")
        typer.echo("  yutome bridge status     # see if it's running")
        typer.echo("  yutome bridge start      # start manually (e.g. if you stopped it)")
        typer.echo("  yutome bridge stop       # stop it")
        typer.echo("  yutome bridge install    # run it via launchd/systemd so it survives reboots")
        typer.echo("  (Behind a corporate proxy that blocks WebSockets, requests fall back to the offline response.)")
    else:
        typer.echo("This endpoint is saved, but the local bridge token is not. This computer")
        typer.echo("cannot answer assistant requests until you save the Worker secrets locally:")
        typer.echo("  yutome connect --endpoint <url> --relay-token <token> --pairing-code <code>")
    typer.echo("")
    if "claude" in targets:
        typer.echo("Claude:")
        typer.echo("  Docs: https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp")
        typer.echo("  1. Open Claude Customize > Connectors.")
        typer.echo("  2. Click +, choose Add custom connector, then Custom > Web if Claude asks.")
        typer.echo("  3. Paste the /mcp URL above. Leave advanced OAuth fields blank.")
        typer.echo("  4. Click Add, then Connect. Claude opens the Yutome pairing tab.")
        typer.echo("  5. Paste the latest pairing code printed above, then approve.")
        typer.echo("  6. Back in Claude, expand Read-only tools and choose Allowed always if you")
        typer.echo("     trust this read-only connector; otherwise Claude will prompt every tool call.")
        typer.echo("  Claude may route Settings > Connectors to Customize > Connectors.")
        typer.echo("  The same Claude account should make it available across web, mobile, Desktop, and Cowork.")
        typer.echo("  Enable/select Yutome in each chat if Claude shows a per-chat connector picker.")
    if "chatgpt" in targets:
        typer.echo("ChatGPT:")
        typer.echo("  Docs: https://developers.openai.com/api/docs/guides/developer-mode")
        typer.echo("  MCP auth notes: https://developers.openai.com/api/docs/mcp")
        typer.echo("  1. Turn on Developer mode from Settings > Apps > Advanced settings.")
        typer.echo("  2. Open Settings > Apps, click Create app, and paste the /mcp URL above.")
        typer.echo("  3. Choose OAuth/authenticated when ChatGPT asks.")
        typer.echo("  4. During OAuth, paste the latest pairing code printed above.")
        typer.echo("  5. In each chat, select Yutome from + > More / composer tools before asking.")
    if "other" in targets:
        typer.echo("Other MCP clients:")
        typer.echo("  MCP transport docs: https://modelcontextprotocol.io/docs/concepts/transports")
        typer.echo("  Remote test guide: https://developers.cloudflare.com/agents/guides/test-remote-mcp-server/")
        typer.echo("  Reuse the same /mcp URL; configure each app/account once, not every physical device.")
        typer.echo("  Pick Streamable HTTP if the client asks for a transport. OAuth/DCR is handled")
        typer.echo("  by the Worker; the only user-facing secret is the latest pairing code.")
    typer.echo("")
    typer.echo("If this computer or bridge is off, the connector stays installed but reports Yutome Desktop offline.")
    typer.echo("No Yutome account, Auth0, Clerk, or Cloudflare Access setup is required.")


def _print_deploy_secrets_card(mcp_url: str, pairing_code: str | None) -> None:
    """Visually-distinct success card printed after a successful Cloudflare deploy.

    Renders MCP URL + pairing code in a magenta-bordered Rich panel and
    auto-copies the MCP URL to the clipboard. Falls back to a plain framed
    block if Rich somehow isn't available (it's a transitive of typer, so
    this is defensive only).
    """
    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.text import Text
    except ImportError:  # pragma: no cover — Rich ships with typer.
        typer.echo("")
        typer.echo("✓ Yutome Worker is live")
        typer.echo(f"  Paste this URL into your assistant: {mcp_url}")
        if pairing_code:
            typer.echo(f"  Pairing code (one-time during OAuth): {pairing_code}")
        return

    console = Console(file=sys.stdout, soft_wrap=False)
    body = Text()
    body.append("Paste this URL into your assistant:\n", style="dim")
    body.append(f"  {mcp_url}\n", style="bold cyan")
    if pairing_code:
        body.append("\nPairing code (asked once, during the assistant's OAuth step):\n", style="dim")
        body.append(f"  {pairing_code}\n", style="bold yellow")
    console.print("")
    console.print(
        Panel(
            body,
            title="[bold green]✓ Yutome Worker is live[/bold green]",
            title_align="left",
            border_style="magenta",
            padding=(1, 2),
        )
    )
    # Only mutate the system clipboard for interactive runs. Non-TTY
    # callers (CI tests, `yutome connect --deploy > log.txt`, automation)
    # must not have their clipboard silently overwritten — the sibling
    # `_setup_local_mcp` snippet path gates the same operation behind a
    # confirm; we mirror that intent here.
    if setup_prompts.is_interactive():
        if _copy_to_clipboard(mcp_url):
            typer.secho("[OK] MCP URL copied to your clipboard.", fg="green")
        else:
            typer.echo("(Couldn't auto-copy. Select the URL above to copy by hand.)")


def _print_pairing_next_steps(state: Any) -> None:
    pairing_code = getattr(state, "pairing_code", None)
    endpoint = getattr(state, "endpoint_url", None)
    if not endpoint:
        return
    typer.echo("")
    typer.echo("Pair this connector during assistant setup:")
    if pairing_code:
        typer.echo(f"  Code: {pairing_code}")
        typer.echo("  Use the latest code printed by `yutome connect`; rerunning deploy refreshes it.")
        typer.echo("  Claude/ChatGPT will open the Yutome browser tab during OAuth setup.")
        typer.echo("  Do not open /pair manually. Paste the code in the assistant-opened tab.")
        typer.echo("  If several Yutome tabs are open, use the newest tab/newest code and close the rest after success.")
    else:
        typer.echo("  You will need the YUTOME_PAIRING_CODE secret set on the Worker.")
    typer.echo("")
    typer.echo("Tip: in Claude Desktop, after pairing succeeds, open the connector settings,")
    typer.echo("expand 'Read-only tools', and switch the per-group permission from")
    typer.echo("'Needs approval' to 'Allowed always' — otherwise every tool call will prompt.")


def _print_setup_mcp_section(*, yes: bool) -> None:
    typer.echo("")
    typer.echo("Use Yutome from your AI assistant:")
    typer.echo("")
    typer.echo("  Right now, to search your library you run `yutome find \"topic\"`. After")
    typer.echo("  this step, you can ask an AI assistant the same question and it searches")
    typer.echo("  yutome for you, citing the videos. Works with any assistant that speaks")
    typer.echo("  MCP — Claude (Desktop, Code, web, mobile), ChatGPT, Cursor, Cherry")
    typer.echo("  Studio, LibreChat, Goose, and others. Your transcripts stay on this")
    typer.echo("  computer either way.")
    typer.echo("")
    typer.echo("  Two ways to connect, depending on where you use the assistant:")
    typer.echo("")
    typer.echo("  [Local apps]   Assistants running on this Mac")
    typer.echo("    - Examples: Claude Desktop, Cursor, Cherry Studio, LibreChat, Claude Code")
    typer.echo("    - Yutome shows you a config snippet, copies it to your clipboard, and")
    typer.echo("      can open the right folder so you can paste it in")
    typer.echo("    - Free, no accounts to create, ready in ~30 seconds")
    typer.echo("    - Only works while you're on THIS Mac — not your phone or another laptop")
    typer.echo("")
    typer.echo("  [Web + mobile]   Assistants you reach from anywhere")
    typer.echo("    - Examples: claude.ai (web), ChatGPT, your phone, another laptop")
    typer.echo("    - Yutome deploys a small piece to your free Cloudflare account (you'll")
    typer.echo("      sign in to Cloudflare during setup; no card needed for the free tier)")
    typer.echo("    - Add one URL to your assistant once; it works from every device after")
    typer.echo("    - Catch: this computer has to be on (the laptop bridge runs in the")
    typer.echo("      background; `yutome bridge install` makes it survive reboots). When")
    typer.echo("      it's off, the assistant just says 'Yutome Desktop offline' and the")
    typer.echo("      rest of the chat keeps working.")
    typer.echo("")
    typer.echo("  You can pick one, both, or skip and run `yutome connect` later.")
    if yes:
        typer.echo("")
        typer.echo("  Optional next step: yutome connect --app claude")


def _claude_desktop_config_path() -> Path:
    """Where Claude Desktop reads its MCP server config. Path is conventional —
    the file may not exist yet until the user creates it via Settings →
    Developer → Edit Config or by editing manually."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "claude-desktop" / "claude_desktop_config.json"


def _copy_to_clipboard(text: str) -> bool:
    """Best-effort clipboard copy. Returns True if the platform tool ran cleanly."""
    if sys.platform == "darwin":
        tool = ["pbcopy"]
    elif sys.platform.startswith("win"):
        tool = ["clip"]
    elif shutil.which("wl-copy"):
        tool = ["wl-copy"]
    elif shutil.which("xclip"):
        tool = ["xclip", "-selection", "clipboard"]
    elif shutil.which("xsel"):
        tool = ["xsel", "--clipboard", "--input"]
    else:
        return False
    try:
        subprocess.run(tool, input=text, text=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _reveal_in_file_manager(path: Path) -> bool:
    """Open the path (or its parent if missing) in Finder / Explorer / xdg-open."""
    target = path if path.exists() else path.parent
    if not target.exists():
        target = target.parent
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=True)
        elif sys.platform.startswith("win"):
            subprocess.run(["explorer", str(target)], check=False)
        else:
            subprocess.run(["xdg-open", str(target)], check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


MCPB_MANIFEST_VERSION = "0.3"


def _yutome_icon_for_bundle() -> Path | None:
    candidate = Path(__file__).resolve().parent / "assets" / "yutome-icon-256.png"
    return candidate if candidate.exists() else None


def _yutome_mcpb_manifest(yutome_cmd: str, abs_config: Path) -> dict[str, Any]:
    from yutome import __version__

    return {
        "manifest_version": MCPB_MANIFEST_VERSION,
        "name": "yutome",
        "display_name": "Yutome",
        "version": __version__,
        "description": "Search your local YouTube transcript library from any MCP-aware assistant.",
        "author": {"name": "Kifah Meeran", "url": "https://github.com/MaskyS/yutome"},
        "icon": "icon.png",
        "server": {
            "type": "binary",
            "entry_point": yutome_cmd,
            "mcp_config": {
                "command": yutome_cmd,
                "args": ["mcp", "serve", "--config", str(abs_config)],
            },
        },
    }


def _build_yutome_mcpb(config_path: Path, *, output_path: Path) -> Path:
    yutome_cmd = shutil.which("yutome") or "yutome"
    abs_config = config_path.resolve()
    manifest = _yutome_mcpb_manifest(yutome_cmd, abs_config)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    icon = _yutome_icon_for_bundle()
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        if icon is not None:
            zf.write(icon, "icon.png")
    return output_path


def _open_with_default_app(path: Path) -> bool:
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=True)
        elif sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.run(["xdg-open", str(path)], check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


def _offer_claude_desktop_bundle(config_path: Path) -> bool:
    """Offer to build a Claude Desktop `.mcpb` bundle and open it for one-click install.

    Returns True if a bundle was built and opened (or the user accepted and we tried),
    False if declined or the platform doesn't support Claude Desktop.
    """
    if sys.platform != "darwin" and not sys.platform.startswith("win"):
        return False
    typer.echo("")
    typer.echo("Claude Desktop one-click installer (.mcpb bundle)")
    typer.echo("  Yutome can build a bundle pointing at this machine's config and open it so")
    typer.echo("  Claude Desktop pops its 'Install Extension' dialog. No JSON editing.")
    if not setup_prompts.confirm("Build the .mcpb bundle and open it now?", default=True):
        return False
    output_path = config_path.parent.resolve() / "yutome.mcpb"
    bundle = _build_yutome_mcpb(config_path, output_path=output_path)
    typer.echo(f"[OK] Built {bundle}")
    if _open_with_default_app(bundle):
        typer.echo("Claude Desktop should pop the install dialog. Click 'Install' to add Yutome.")
        _callout(
            "After clicking Install: restart Claude Desktop, then look for Yutome under "
            "Settings → Connectors. If it's not there, quit Claude fully (⌘Q) and reopen."
        )
    else:
        # Don't print the "After installing…" hint here: the install never
        # started (otherwise `open` wouldn't have failed). Pairing it with
        # the WARN above misleads the user into thinking restart is the
        # next step when the next step is actually to double-click the
        # bundle themselves.
        typer.echo(f"[WARN] Couldn't open it automatically. Double-click {bundle} to install,")
        typer.echo("       then restart Claude Desktop and look under Settings → Connectors.")
    return True


def _setup_local_mcp(config_path: Path) -> None:
    """Help a user wire yutome into Claude Desktop / Code / Cursor on this machine.

    On macOS / Windows we first offer a one-click `.mcpb` bundle for Claude Desktop.
    For everything else (Cursor, Cherry Studio, Claude Code, Goose) we fall through
    to a generic JSON snippet — Claude Desktop's config is often touched by hand
    or by other installers, and JSON-merging into it from a CLI is a foot-gun:
    comments don't round-trip, key ordering changes, and a botched write loses
    other server entries.
    """
    if _offer_claude_desktop_bundle(config_path):
        if not setup_prompts.confirm(
            "Also show the JSON snippet for other apps (Cursor, Claude Code, Cherry Studio, ...)?",
            default=False,
        ):
            return
    abs_config = config_path.resolve()
    yutome_cmd = shutil.which("yutome") or "yutome"
    snippet = json.dumps(
        {
            "mcpServers": {
                "yutome": {
                    "command": yutome_cmd,
                    "args": ["mcp", "serve", "--config", str(abs_config)],
                }
            }
        },
        indent=2,
    )
    desktop_path = _claude_desktop_config_path()
    typer.echo("")
    typer.echo("Local MCP setup — for any AI assistant running on this Mac")
    typer.echo("")
    typer.echo("  Yutome's config snippet (same for every MCP-aware app):")
    typer.echo("")
    for line in snippet.splitlines():
        typer.echo(f"    {line}")
    typer.echo("")
    typer.echo("  Where to paste it, by app:")
    typer.echo("")
    typer.echo("    Claude Desktop")
    typer.echo(f"      Config file: {desktop_path}")
    typer.echo("      (Inside Claude Desktop: Settings → Developer → Edit Config opens it.)")
    typer.echo("      If `mcpServers` already exists, add the `\"yutome\": { ... }` entry")
    typer.echo("      inside it — don't replace what's there. Restart Claude Desktop after.")
    typer.echo("")
    typer.echo("    Cursor")
    typer.echo("      Paste into ~/.cursor/mcp.json (global) or .cursor/mcp.json (per-project).")
    typer.echo("")
    typer.echo("    Claude Code  (one-liner, no JSON editing)")
    typer.echo(f"      claude mcp add yutome -- {yutome_cmd} mcp serve --config {abs_config}")
    typer.echo("")
    typer.echo("    Cherry Studio, LibreChat, Goose, others")
    typer.echo("      Find each app's MCP server settings and paste the same snippet.")
    typer.echo("      Search '<app> MCP config' if you're not sure where it lives.")
    typer.echo("")
    if setup_prompts.confirm("Copy the snippet to your clipboard?", default=True):
        if _copy_to_clipboard(snippet):
            typer.echo("[OK] Snippet copied to clipboard.")
        else:
            typer.echo("[WARN] No clipboard tool found; copy the snippet above by hand.")
    if desktop_path.exists():
        prompt = "Open the Claude Desktop config folder in Finder?"
    else:
        prompt = (
            "Claude Desktop's config doesn't exist yet (you may not have Claude Desktop "
            "installed). Open the folder anyway?"
        )
    if setup_prompts.confirm(prompt, default=False):
        if not _reveal_in_file_manager(desktop_path):
            typer.echo(f"[WARN] Couldn't open the folder automatically. Path: {desktop_path}")
    typer.echo("")
    typer.echo("  Local MCP only works while you're on this Mac. For phone, claude.ai web,")
    typer.echo("  or another device, pick the 'Web + mobile' option instead.")


def _normalize_pasted_connector_url(raw: str) -> str:
    """Clean up a connector URL pasted by a user.

    Common foot-guns we forgive silently:

    * Stray ``/mcp`` / ``/authorize`` / ``/pair`` paths copied from the
      assistant pairing flow (we want the *base* worker URL or ``/mcp`` —
      both are accepted by ``_save_deployed_worker_endpoint``, but
      ``/authorize`` and ``/pair`` are not, so strip those).
    * Trailing slashes and whitespace.
    * Query strings (notably the ``?code=...`` fragment pairing URLs carry).

    If the result doesn't look like a Cloudflare workers.dev URL we warn,
    but we still return it — users can host the connector on their own
    domain. The save call will reject genuinely-invalid URLs.
    """
    value = (raw or "").strip()
    if not value:
        return ""
    parsed = urllib.parse.urlsplit(value)
    if not parsed.scheme:
        parsed = urllib.parse.urlsplit("https://" + value)
    path = parsed.path or ""
    for suffix in ("/authorize", "/pair"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    path = path.rstrip("/")
    cleaned = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    if cleaned != value:
        typer.echo(f"[OK] Normalized URL to: {cleaned}")
    host = parsed.netloc.lower()
    if host and not (host.endswith(".workers.dev") or "." in host):
        typer.secho(
            f"[WARN] {host!r} doesn't look like a Cloudflare workers.dev host. "
            "Continuing — yutome will reject it later if it's not reachable.",
            fg="yellow",
        )
    return cleaned


def _pairing_url(endpoint_url: str, pairing_code: str | None = None) -> str:
    url = f"{endpoint_url.rstrip('/')}/pair"
    return f"{url}?code={pairing_code}" if pairing_code else url


def _save_remote_connection(
    config: Path,
    *,
    endpoint: str,
    mode: RemoteMode,
    worker_name: str | None = None,
    relay_token: str | None = None,
    pairing_code: str | None = None,
    token_secret: str | None = None,
) -> Path:
    paths = _prepare_connect_project(config)
    existing = load_remote_state(paths)
    state = build_remote_state(
        endpoint=endpoint,
        mode=mode,
        worker_name=worker_name,
        relay_token=relay_token,
        pairing_code=pairing_code,
        token_secret=token_secret,
        existing=existing,
    )
    return save_remote_state(paths, state)


def _extract_worker_url(output: str) -> str | None:
    match = re.search(r"https://[a-zA-Z0-9.-]+\.workers\.dev(?:/[^\s]*)?", output)
    return match.group(0).rstrip("/") if match else None


def _should_use_interactive_command_stream() -> bool:
    return os.name == "posix" and setup_prompts.is_interactive()


def _command_exit_code(status: int) -> int:
    if hasattr(os, "waitstatus_to_exitcode"):
        return os.waitstatus_to_exitcode(status)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return status


def _run_command_streamed_pty(command: list[str], *, cwd: Path) -> tuple[int, str]:
    """Run an interactive subprocess through a PTY while capturing output.

    Wrangler detects piped stdout as non-interactive and skips prompts such as
    workers.dev subdomain registration. A PTY keeps those prompts available.
    """
    import pty

    output: list[bytes] = []

    def read_master(fd: int) -> bytes:
        try:
            chunk = os.read(fd, 1024)
        except OSError:
            return b""
        output.append(chunk)
        return chunk

    previous_cwd = Path.cwd()
    os.chdir(cwd)
    try:
        status = pty.spawn(command, master_read=read_master)
    finally:
        os.chdir(previous_cwd)
    return _command_exit_code(status), b"".join(output).decode(errors="replace")


def _run_command_streamed(command: list[str], *, cwd: Path) -> tuple[int, str]:
    if _should_use_interactive_command_stream():
        return _run_command_streamed_pty(command, cwd=cwd)

    process = subprocess.Popen(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output: list[str] = []
    if process.stdout is not None:
        for line in process.stdout:
            output.append(line)
            typer.echo(line.rstrip())
    return process.wait(), "".join(output)


def _cloudflare_workers_onboarding_url(output: str) -> str:
    match = re.search(r"/accounts/([0-9a-f]{32})/", output)
    if match:
        return f"https://dash.cloudflare.com/{match.group(1)}/workers/onboarding"
    return "https://dash.cloudflare.com/?to=/:account/workers/onboarding"


def _is_workers_dev_subdomain_error(output: str) -> bool:
    lower = output.lower()
    return "10063" in lower or "workers.dev subdomain" in lower


def _print_workers_dev_subdomain_help(output: str) -> None:
    onboarding_url = _cloudflare_workers_onboarding_url(output)
    typer.echo("", err=True)
    typer.echo("This Cloudflare account has not finished Workers setup yet.", err=True)
    typer.echo(
        "Create the account workers.dev subdomain, then rerun `yutome connect --deploy`.",
        err=True,
    )
    typer.echo(f"Cloudflare Workers onboarding: {onboarding_url}", err=True)
    if not _should_use_interactive_command_stream():
        typer.echo(
            "In an interactive terminal, Yutome runs Wrangler with a real TTY so Wrangler can prompt "
            "for this during deploy.",
            err=True,
        )


def _is_email_unverified_error(output: str) -> bool:
    lower = output.lower()
    return "10034" in lower or "verify your email" in lower


def _print_email_unverified_help(output: str) -> None:
    typer.echo("", err=True)
    typer.echo("This Cloudflare account hasn't verified its email address yet.", err=True)
    typer.echo(
        "Check the inbox for the address you signed into Cloudflare with for a "
        "verification email, click the link, then rerun `yutome connect --deploy`.",
        err=True,
    )
    typer.echo(
        "To resend the verification email, sign in at https://dash.cloudflare.com/ "
        "and open the profile menu in the top right.",
        err=True,
    )
    typer.echo(
        "More info: https://developers.cloudflare.com/fundamentals/setup/account/verify-email-address/",
        err=True,
    )


_WRANGLER_OAUTH_CONFIG_CANDIDATES = (
    Path.home() / "Library/Preferences/.wrangler/config/default.toml",
    Path.home() / ".config/.wrangler/config/default.toml",
    Path.home() / ".wrangler/config/default.toml",
)


def _read_wrangler_oauth_token() -> str | None:
    for path in _WRANGLER_OAUTH_CONFIG_CANDIDATES:
        try:
            content = path.read_text(encoding="utf-8")
        except (FileNotFoundError, PermissionError, OSError):
            continue
        match = re.search(r'^oauth_token\s*=\s*"([^"]+)"', content, re.MULTILINE)
        if match:
            return match.group(1)
    return None


def _cloudflare_bearer_token() -> str | None:
    if env_token := os.environ.get("CLOUDFLARE_API_TOKEN"):
        return env_token
    return _read_wrangler_oauth_token()


def _wrangler_account_id(capsule: Path) -> str | None:
    if env_id := os.environ.get("CLOUDFLARE_ACCOUNT_ID"):
        return env_id
    completed = _run_wrangler_capture(capsule, ["whoami"])
    if completed.returncode != 0:
        return None
    matches = re.findall(r"\b([0-9a-f]{32})\b", completed.stdout or "")
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def _cloudflare_api_call(
    method: str,
    path: str,
    token: str,
    payload: dict | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4{path}",
        method=method,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8") or "{}")
            return response.status, data
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read().decode("utf-8") or "{}")
        except (json.JSONDecodeError, OSError):
            data = {}
        return exc.code, data
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return 0, {}


def _workers_dev_subdomain_state(account_id: str, token: str) -> tuple[bool | None, str | None]:
    """Return (exists, name). exists=None means we couldn't tell."""
    status, body = _cloudflare_api_call(
        "GET", f"/accounts/{account_id}/workers/subdomain", token
    )
    if status == 200 and body.get("success"):
        name = (body.get("result") or {}).get("subdomain")
        return (True, name) if name else (False, None)
    error_codes = {error.get("code") for error in body.get("errors", []) if isinstance(error, dict)}
    if status in (404,) or error_codes & {10007, 10063}:
        return False, None
    return None, None


def _create_workers_dev_subdomain(account_id: str, token: str, name: str) -> tuple[bool, str]:
    """Return (success, message). On success, message is the created subdomain."""
    status, body = _cloudflare_api_call(
        "PUT",
        f"/accounts/{account_id}/workers/subdomain",
        token,
        {"subdomain": name},
    )
    if status == 200 and body.get("success"):
        return True, (body.get("result") or {}).get("subdomain", name)
    errors = body.get("errors") or [{}]
    return False, errors[0].get("message", f"HTTP {status}")


def _suggest_workers_dev_subdomain_name() -> str:
    return f"yutome-{secrets.token_hex(4)}"


def _ensure_workers_dev_subdomain(capsule: Path) -> None:
    """Best-effort: make sure the user's account has a workers.dev subdomain.

    If we can't tell or can't create one (missing token, multi-account ambiguity,
    network error, insufficient permissions), we stay silent and let the
    subsequent ``wrangler deploy`` surface the real error with the existing
    10063 help message.
    """
    token = _cloudflare_bearer_token()
    if not token:
        return
    account_id = _wrangler_account_id(capsule)
    if not account_id:
        return
    exists, _ = _workers_dev_subdomain_state(account_id, token)
    if exists is True or exists is None:
        return
    for _ in range(3):
        name = _suggest_workers_dev_subdomain_name()
        success, message = _create_workers_dev_subdomain(account_id, token, name)
        if success:
            typer.echo(f"[OK] Created workers.dev subdomain: {message}.workers.dev")
            return
        if "taken" not in message.lower() and "already" not in message.lower():
            return


def _run_wrangler_capture(capsule: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["npx", "--yes", "wrangler", *args],
        cwd=capsule,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _wrangler_auth_message() -> str:
    return (
        "Cloudflare authentication is required. Wrangler can open a browser sign-in from an "
        "interactive terminal, or you can set CLOUDFLARE_API_TOKEN for scripted/non-interactive runs."
    )


def _wrangler_whoami_authenticated(completed: subprocess.CompletedProcess[str]) -> bool:
    output = (completed.stdout or "").lower()
    if "not authenticated" in output or "not logged in" in output:
        return False
    return completed.returncode == 0


def _ensure_wrangler_authenticated(capsule: Path) -> None:
    if os.environ.get("CLOUDFLARE_API_TOKEN"):
        return
    completed = _run_wrangler_capture(capsule, ["whoami"])
    if _wrangler_whoami_authenticated(completed):
        return
    if not setup_prompts.is_interactive():
        typer.echo(_wrangler_auth_message(), err=True)
        typer.echo(
            "Create a token with Workers Scripts and Workers KV permissions, then rerun with "
            "CLOUDFLARE_API_TOKEN set.",
            err=True,
        )
        if completed.stdout:
            typer.echo(completed.stdout.rstrip(), err=True)
        raise typer.Exit(code=completed.returncode or 1)

    typer.echo("")
    typer.echo(_wrangler_auth_message())
    typer.echo("Starting Cloudflare browser sign-in with Wrangler...")
    login = subprocess.run(["npx", "--yes", "wrangler", "login"], cwd=capsule, check=False)
    if login.returncode != 0:
        typer.echo("Cloudflare sign-in failed. Rerun `yutome connect --deploy` after signing in.", err=True)
        raise typer.Exit(code=login.returncode)
    verified = _run_wrangler_capture(capsule, ["whoami"])
    if not _wrangler_whoami_authenticated(verified):
        typer.echo("Cloudflare sign-in did not complete successfully.", err=True)
        if verified.stdout:
            typer.echo(verified.stdout.rstrip(), err=True)
        raise typer.Exit(code=verified.returncode or 1)


# ---------- Tracked TypeScript Worker (cloudflare/yutome-capsule) ----------

CAPSULE_PROJECT_NAME = "yutome-remote-mcp"  # matches name in wrangler.toml
GENERATED_WRANGLER_FILENAME = "wrangler.generated.toml"


def _tracked_capsule_path() -> Path:
    """Path to the tracked TypeScript Worker project.

    Editable checkouts use the repo-level cloudflare/ tree. Wheels include the
    same files under yutome/cloudflare/ so uv/pipx installs can deploy too.
    """
    here = Path(__file__).resolve()
    repo_capsule = here.parents[2] / "cloudflare" / "yutome-capsule"
    if repo_capsule.exists():
        return repo_capsule
    return here.parent / "cloudflare" / "yutome-capsule"


def _ensure_capsule_node_modules(capsule: Path) -> None:
    if (capsule / "node_modules").exists():
        return
    _require_cloudflare_deploy_runtime()
    typer.echo(f"Installing TypeScript Worker dependencies in {capsule}")
    returncode, _ = _run_command_streamed(["npm", "install"], cwd=capsule)
    if returncode != 0:
        typer.echo("npm install failed. Fix the error above and rerun `yutome connect --deploy`.", err=True)
        raise typer.Exit(code=returncode)


_OAUTH_KV_ID_RE = re.compile(r'id\s*=\s*"([0-9a-f]{8,})"')


def _generated_cloudflare_dir(paths: ProjectPaths) -> Path:
    return paths.data_dir / "remote" / "cloudflare"


def _generated_wrangler_config_path(paths: ProjectPaths) -> Path:
    return _generated_cloudflare_dir(paths) / GENERATED_WRANGLER_FILENAME


def _active_oauth_kv_id(content: str) -> str | None:
    active = "\n".join(line for line in content.splitlines() if not line.lstrip().startswith("#"))
    if 'binding = "OAUTH_KV"' not in active:
        return None
    match = _OAUTH_KV_ID_RE.search(active.split('binding = "OAUTH_KV"', 1)[1])
    return match.group(1) if match else None


def _strip_oauth_kv_binding(content: str) -> str:
    lines = content.splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip() == "[[kv_namespaces]]":
            block = [line]
            index += 1
            while index < len(lines) and not lines[index].lstrip().startswith("["):
                block.append(lines[index])
                index += 1
            if any('binding = "OAUTH_KV"' in block_line for block_line in block):
                continue
            output.extend(block)
            continue
        output.append(line)
        index += 1
    return "\n".join(output).rstrip() + "\n"


def _write_generated_wrangler_config(capsule: Path, paths: ProjectPaths, namespace_id: str) -> Path:
    source_config = capsule / "wrangler.toml"
    content = _strip_oauth_kv_binding(source_config.read_text(encoding="utf-8"))
    absolute_main = str((capsule / "src" / "index.ts").resolve()).replace("\\", "\\\\")
    content = re.sub(r'^main\s*=\s*"[^"]+"', f'main = "{absolute_main}"', content, count=1, flags=re.MULTILINE)
    content = (
        content.rstrip()
        + "\n\n# Generated by `yutome connect --deploy`; account-specific and ignored by git.\n"
        + "[[kv_namespaces]]\n"
        + 'binding = "OAUTH_KV"\n'
        + f'id = "{namespace_id}"\n'
    )
    generated_path = _generated_wrangler_config_path(paths)
    generated_path.parent.mkdir(parents=True, exist_ok=True)
    generated_path.write_text(content, encoding="utf-8")
    try:
        generated_path.chmod(0o600)
    except OSError:
        pass
    return generated_path


def _existing_oauth_kv_namespace_id(capsule: Path) -> str | None:
    completed = _run_wrangler_capture(capsule, ["kv", "namespace", "list"])
    if completed.returncode != 0:
        return None
    try:
        namespaces = json.loads(completed.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(namespaces, list):
        return None
    for namespace in namespaces:
        if not isinstance(namespace, dict):
            continue
        if namespace.get("title") == "OAUTH_KV" and isinstance(namespace.get("id"), str):
            return namespace["id"]
    return None


def _ensure_oauth_kv_namespace(capsule: Path, paths: ProjectPaths) -> Path:
    """Create or reuse an account-local OAUTH_KV binding in ignored state.

    The tracked Worker config deliberately does not contain a real KV id because
    Cloudflare namespace ids are account-specific. Assisted deploy writes the
    actual binding to data/remote/cloudflare/wrangler.generated.toml instead.
    """
    generated_config = _generated_wrangler_config_path(paths)
    if generated_config.exists():
        existing_id = _active_oauth_kv_id(generated_config.read_text(encoding="utf-8"))
        if existing_id:
            current_namespace_id = _existing_oauth_kv_namespace_id(capsule)
            if current_namespace_id is None or current_namespace_id == existing_id:
                return generated_config
            typer.echo(
                f"[WARN] Refreshing stale OAUTH_KV namespace id={existing_id}; "
                f"current account has id={current_namespace_id}"
            )
            generated_config = _write_generated_wrangler_config(capsule, paths, current_namespace_id)
            typer.echo(f"[OK] Wrote account-local Wrangler config: {generated_config}")
            return generated_config

    existing_namespace_id = _existing_oauth_kv_namespace_id(capsule)
    if existing_namespace_id:
        typer.echo(f"[OK] Reusing existing OAUTH_KV namespace id={existing_namespace_id}")
        generated_config = _write_generated_wrangler_config(capsule, paths, existing_namespace_id)
        typer.echo(f"[OK] Wrote account-local Wrangler config: {generated_config}")
        return generated_config

    typer.echo("Creating Cloudflare KV namespace OAUTH_KV (one-time setup)…")
    completed = _run_wrangler_capture(capsule, ["kv", "namespace", "create", "OAUTH_KV"])
    if completed.returncode != 0:
        typer.echo("Failed to create OAUTH_KV namespace.", err=True)
        if completed.stdout:
            typer.echo(completed.stdout.rstrip(), err=True)
        raise typer.Exit(code=completed.returncode)

    match = _OAUTH_KV_ID_RE.search(completed.stdout or "")
    if not match:
        typer.echo(
            "OAUTH_KV namespace created but Wrangler's id could not be parsed. "
            "Paste the binding block manually into wrangler.toml.",
            err=True,
        )
        if completed.stdout:
            typer.echo(completed.stdout.rstrip(), err=True)
        raise typer.Exit(code=1)
    namespace_id = match.group(1)
    typer.echo(f"[OK] Created OAUTH_KV namespace id={namespace_id}")
    generated_config = _write_generated_wrangler_config(capsule, paths, namespace_id)
    typer.echo(f"[OK] Wrote account-local Wrangler config: {generated_config}")
    return generated_config


def _wait_for_worker_online(
    url: str,
    *,
    timeout: float = 60.0,
    interval: float = 1.5,
    request_timeout: float = 3.0,
) -> bool:
    """Poll ``{url}/healthz`` until it responds 200, or ``timeout`` elapses.

    ``wrangler deploy`` returns as soon as Cloudflare's control plane has
    accepted the bundle, but the URL can take several more seconds to
    actually serve requests from the edge (and the secret pushes that
    follow take their own propagation time). Without this wait, the
    deploy-success card and the per-assistant pairing prose are printed
    while the user's first paste into Claude / ChatGPT still 502s.

    Returns True if the worker started responding within the window, False
    if we timed out. A False return is non-fatal — callers print a soft
    warning and proceed; the worker almost always comes up in another
    minute or two.
    """
    health_url = url.rstrip("/") + "/healthz"
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    with _spinner(
        f"Waiting for Cloudflare edge to start serving {url} (up to {int(timeout)}s)…"
    ):
        while time.monotonic() < deadline:
            try:
                req = urllib.request.Request(
                    health_url,
                    headers={
                        "accept": "application/json",
                        "user-agent": "yutome-setup/healthz-probe",
                    },
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=request_timeout) as response:
                    if 200 <= response.status < 300:
                        return True
                    last_error = f"HTTP {response.status}"
            except urllib.error.HTTPError as exc:
                # A 4xx from /healthz means the route is registered (worker
                # is up) but the response wasn't successful — still counts
                # as "reachable enough" since the URL is serving traffic.
                if 400 <= exc.code < 500:
                    return True
                last_error = f"HTTP {exc.code}"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = str(exc)
            time.sleep(interval)
    typer.secho(
        f"[WARN] Worker not yet reachable at {url} after {int(timeout)}s"
        + (f" (last error: {last_error})" if last_error else "")
        + ". It usually comes up within another minute — try pairing in your assistant in ~60s.",
        fg="yellow",
    )
    return False


def _push_wrangler_secret(capsule: Path, name: str, value: str, *, wrangler_config: Path | None = None) -> None:
    """Push a secret to the deployed Worker via `wrangler secret put`."""
    typer.echo(f"Setting Cloudflare secret {name}")
    command = ["npx", "--yes", "wrangler", "secret", "put", name]
    if wrangler_config is not None:
        command.extend(["--config", str(wrangler_config)])
    completed = subprocess.run(
        command,
        cwd=capsule,
        input=f"{value}\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        typer.echo(f"Failed to set {name}.", err=True)
        if completed.stderr:
            typer.echo(completed.stderr.rstrip(), err=True)
        raise typer.Exit(code=completed.returncode)


def _deploy_tracked_capsule(
    *,
    paths: ProjectPaths,
    refresh_contract: bool = True,
    relay_token: str | None = None,
    pairing_code: str | None = None,
) -> tuple[str | None, str, str, str]:
    """Deploy the tracked TypeScript Worker from cloudflare/yutome-capsule.

    Generates ``YUTOME_RELAY_TOKEN`` and ``YUTOME_PAIRING_CODE`` if not
    supplied, pushes them to Cloudflare as encrypted secrets, and returns
    ``(deployed_url, worker_name, relay_token, pairing_code)`` so the caller
    can persist them to local state.
    """
    capsule = _tracked_capsule_path()
    if not capsule.exists():
        typer.echo(
            f"Expected bundled TypeScript Worker project at {capsule}, but it is missing.",
            err=True,
        )
        typer.echo(
            "Reinstall yutome from a build that includes cloudflare/yutome-capsule, "
            "or run the deploy from a repository checkout.",
            err=True,
        )
        raise typer.Exit(code=1)
    _require_cloudflare_deploy_runtime()

    effective_relay_token = relay_token or secrets.token_urlsafe(32)
    effective_pairing_code = pairing_code or secrets.token_hex(5).upper()

    if refresh_contract:
        from yutome.contract_export import emit_contract_json

        contract_path = capsule / "src" / "contract.json"
        emit_contract_json(contract_path)
        typer.echo(f"[OK] Refreshed contract: {contract_path}")

    _ensure_capsule_node_modules(capsule)
    _ensure_wrangler_authenticated(capsule)
    _ensure_workers_dev_subdomain(capsule)
    wrangler_config = _ensure_oauth_kv_namespace(capsule, paths)

    typer.echo(f"Deploying Cloudflare Worker from {capsule}")
    command = ["npx", "--yes", "wrangler", "deploy", "--config", str(wrangler_config)]
    while True:
        returncode, output = _run_command_streamed(command, cwd=capsule)
        if returncode == 0:
            break
        if _is_email_unverified_error(output):
            _print_email_unverified_help(output)
            recoverable = True
        elif _is_workers_dev_subdomain_error(output):
            _print_workers_dev_subdomain_help(output)
            recoverable = True
        else:
            recoverable = False
        if recoverable and setup_prompts.is_interactive():
            typer.echo("", err=True)
            if setup_prompts.confirm(
                "Press Enter to retry the deploy once you've fixed the issue above (or 'n' to abort)",
                default=True,
            ):
                continue
        typer.echo(
            "Cloudflare Worker deploy failed. Fix the Wrangler error above and rerun `yutome connect --deploy`.",
            err=True,
        )
        raise typer.Exit(code=returncode)

    # Worker is up. Push secrets so OAuth pairing + bridge auth work.
    _push_wrangler_secret(capsule, "YUTOME_RELAY_TOKEN", effective_relay_token, wrangler_config=wrangler_config)
    _push_wrangler_secret(capsule, "YUTOME_PAIRING_CODE", effective_pairing_code, wrangler_config=wrangler_config)

    deployed_url = _extract_worker_url(output)
    # Bridge the gap between `wrangler deploy` returning and the Cloudflare
    # edge actually serving requests. Without this, the success card + next
    # steps print before /mcp is reachable and the user's first paste into
    # Claude / ChatGPT 502s.
    if deployed_url:
        if _wait_for_worker_online(deployed_url):
            typer.secho(f"[OK] Worker responding at {deployed_url}", fg="green")
    return deployed_url, CAPSULE_PROJECT_NAME, effective_relay_token, effective_pairing_code


def _delete_tracked_capsule(worker_name: str) -> None:
    """Run `wrangler delete` from the tracked Worker project directory."""
    capsule = _tracked_capsule_path()
    problem = _cloudflare_deploy_runtime_problem()
    if problem is not None:
        typer.echo(f"{problem} Delete the Worker manually in the Cloudflare dashboard.", err=True)
        typer.echo(f"Cloudflare Workers dashboard: {CLOUDFLARE_WORKERS_DASHBOARD_URL}", err=True)
        raise typer.Exit(code=1)
    command = ["npx", "--yes", "wrangler", "delete", worker_name, "--force"]
    typer.echo(f"Removing Cloudflare Worker {worker_name!r} via wrangler in {capsule}")
    completed = subprocess.run(
        command, cwd=capsule, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if completed.stdout:
        typer.echo(completed.stdout.rstrip())
    if completed.stderr:
        typer.echo(completed.stderr.rstrip(), err=True)
    if completed.returncode != 0:
        typer.echo("Worker removal failed. Fix the error above and rerun.", err=True)
        raise typer.Exit(code=completed.returncode)


def _save_deployed_worker_endpoint(
    config: Path,
    *,
    endpoint: str,
    mode: RemoteMode,
    worker_name: str | None = None,
    relay_token: str | None = None,
    pairing_code: str | None = None,
    token_secret: str | None = None,
    assistant_apps: str | None = None,
) -> None:
    state_path = _save_remote_connection(
        config,
        endpoint=endpoint,
        mode=mode,
        worker_name=worker_name,
        relay_token=relay_token,
        pairing_code=pairing_code,
        token_secret=token_secret,
    )
    paths = _load_paths(config)
    state = load_remote_state(paths)
    typer.echo(f"[OK] Saved remote connector state: {state_path}")
    if state is not None:
        typer.echo(f"[OK] MCP URL: {state.mcp_url}")
        _print_pairing_next_steps(state)
        _print_connector_next_steps(
            state.mcp_url,
            bridge_configured=bool(state.relay_token),
            assistant_apps=assistant_apps,
        )


def _disconnect_remote(
    *,
    config: Path,
    worker_name: str | None,
    remove_cloudflare: bool,
    keep_state: bool,
    dry_run: bool,
    yes: bool,
) -> None:
    paths = _load_paths(config)
    state = load_remote_state(paths)
    saved_worker = None
    if state is not None:
        saved_worker = state.cloud_resources.get("cloudflare_worker_name")
    effective_worker = worker_name or saved_worker
    state_path = remote_state_path(paths)

    typer.echo("Disconnect Yutome remote MCP:")
    if state is None:
        typer.echo("  Local connector state: not configured")
    else:
        typer.echo(f"  MCP URL: {state.mcp_url}")
        typer.echo(f"  Local connector state: {state_path}")
    if effective_worker:
        action = "remove" if remove_cloudflare else "keep"
        typer.echo(f"  Cloudflare Worker: {effective_worker} ({action})")
    else:
        typer.echo("  Cloudflare Worker: no Yutome-managed worker recorded")

    if dry_run:
        typer.echo("Dry run only. Nothing was disconnected or removed.")
        return

    if effective_worker and remove_cloudflare:
        if not yes and not typer.confirm("Remove the Yutome Cloudflare Worker from your Cloudflare account too?", default=True):
            remove_cloudflare = False
        if remove_cloudflare:
            _delete_tracked_capsule(effective_worker)

    if state_path.exists() and not keep_state:
        state_path.unlink()
        typer.echo(f"[OK] Removed local remote connector state: {state_path}")
    elif keep_state:
        typer.echo("[OK] Kept local remote connector state.")
    else:
        typer.echo("[OK] No local remote connector state to remove.")
    typer.echo("[OK] Disconnect complete.")


@app.command()
def setup(
    channel: str | None = typer.Argument(
        None,
        help="Optional channel or video URL/id to add during setup.",
    ),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Run non-interactively and print next steps instead of prompting.",
    ),
    hosted: bool = typer.Option(
        False,
        "--hosted",
        help="Configure hosted Yutome account mode instead of local provider keys.",
    ),
) -> None:
    """Guided first-run setup for a local yutome project."""
    typer.secho("yutome · guided setup", fg="magenta", bold=True)
    typer.secho("─" * 21, fg="magenta")
    if not yes and setup_prompts.is_interactive():
        typer.echo(
            "Six short steps. Picked the wrong answer? Ctrl-C and rerun "
            "`yutome setup` — choices already saved are detected and skipped."
        )

    _step_header(1, SETUP_TOTAL_STEPS, "Project setup")
    config_written = write_default_config(config)
    if config_written:
        typer.echo(f"[OK] Wrote config: {config}")
    else:
        typer.echo(f"[OK] Using existing config: {config}")

    project_root = _project_root(config)
    env_path = project_root / ".env"
    env_written = _write_env_template(env_path)
    typer.echo(f"[OK] {'Wrote' if env_written else 'Using existing'} local secrets file: {env_path}")

    paths = _load_paths(config)
    paths.ensure_base_dirs()
    bootstrap_catalog(paths.catalog_db)
    typer.echo(f"[OK] Initialized data directory: {paths.data_dir}")
    typer.echo(f"[OK] Initialized catalog: {paths.catalog_db}")

    load_dotenv(env_path)
    ingest_ok = _module_available("yt_dlp") and _module_available("youtube_transcript_api")
    vectors_ok = _module_available("lancedb")
    embeddings_ok = _module_available("voyageai")
    _status(ingest_ok, "Ingest dependencies", "uv sync" if not ingest_ok else "ready")
    _status(vectors_ok, "Vector database dependency", "uv sync" if not vectors_ok else "ready")
    _status(embeddings_ok, "Embedding client", "uv sync" if not embeddings_ok else "ready")

    if hosted:
        _run_hosted_setup_after_project_init(
            config=config,
            channel=channel,
            yes=yes,
            app_config=apply_env_to_config(load_config(config)),
            paths=paths,
            env_path=env_path,
        )
        return

    _step_header(2, SETUP_TOTAL_STEPS, "Webshare residential proxy")
    _setup_webshare(env_path, yes=yes)

    _step_header(3, SETUP_TOTAL_STEPS, "Gemini (transcript repair + fallback)")
    _setup_gemini(config, env_path, yes=yes)

    _step_header(4, SETUP_TOTAL_STEPS, "Semantic search (Voyage)")
    semantic_enabled = _setup_semantic_search(config, env_path, yes=yes)
    _set_toml_string(config, "find", "default_mode", "hybrid" if semantic_enabled else "lexical")
    load_dotenv(env_path)
    app_config = apply_env_to_config(load_config(config))

    setup_library_channels: list[LibraryChannel] = []
    if not yes:
        _step_header(5, SETUP_TOTAL_STEPS, "YouTube subscriptions & first sync")
        setup_library_channels.extend(
            _setup_import_youtube_subscriptions(
                config=config,
                app_config=app_config,
                paths=paths,
                env_path=env_path,
            )
        )
    selected_channel = channel
    if selected_channel is None and not yes and not setup_library_channels:
        if typer.confirm("Add a YouTube source now?", default=True):
            selected_channel = typer.prompt("Channel or video URL, handle, or id").strip()
    if selected_channel:
        added_source = _add_setup_source(config, selected_channel)
        if added_source is not None and added_source.source_type == "youtube_channel":
            setup_library_channels.append(
                LibraryChannel(
                    library_channel_id=added_source.source_id,
                    source=added_source.source,
                    source_url=added_source.source_url,
                    channel_id=added_source.channel_id,
                    handle=added_source.handle,
                    title=added_source.title,
                    selected=added_source.selected,
                    import_source=added_source.import_source,
                )
            )
        added = 1 if added_source is not None else 0
        typer.echo(f"[OK] Added {added} selected source{'s' if added != 1 else ''}.")
    elif not setup_library_channels:
        typer.echo("[OK] No channel added yet.")

    ran_sync = False
    if not yes and setup_library_channels and typer.confirm("Start indexing some of these channels now?", default=True):
        first_run_channels = _prompt_channels_to_select(
            setup_library_channels,
            title="Choose channels to index in this first run:",
            prompt="Index in this run",
            default=_first_run_default_selection(setup_library_channels),
        )
        if first_run_channels:
            videos_per_channel = _prompt_first_run_video_cap(app_config.backfill.max_videos_per_run)
            if videos_per_channel is None:
                typer.echo(
                    f"This first run will index all available videos across "
                    f"{len(first_run_channels)} channel(s)."
                )
            else:
                upper_bound = len(first_run_channels) * videos_per_channel
                typer.echo(
                    f"This first run may index up to {len(first_run_channels)} channel(s) "
                    f"x {videos_per_channel} videos = {upper_bound} videos."
                )
            _run_setup_first_sync(
                config,
                channels=first_run_channels,
                max_videos_per_channel=videos_per_channel,
            )
            ran_sync = True
        else:
            typer.echo("[OK] No channels selected for immediate indexing.")

    typer.echo("")
    typer.echo("Next steps:" if not ran_sync else "After this run:")
    if setup_library_channels:
        typer.echo("  yutome sync")
    else:
        typer.echo("  yutome add https://www.youtube.com/@SomeChannel")
        typer.echo("  yutome sync")
    if semantic_enabled:
        typer.echo("  yutome find \"topic I remember\" --mode hybrid")
    else:
        typer.echo("  # Optional semantic search:")
        typer.echo("  #   add VOYAGE_API_KEY to .env")
        typer.echo("  #   yutome setup")
    typer.echo("  yutome status")
    typer.echo('  yutome find "topic I remember"')
    if _env_has_webshare_credentials(env_path):
        typer.echo("  yutome proxy-info")
    if not yes:
        _step_header(6, SETUP_TOTAL_STEPS, "Connect Yutome to your assistant")
    _print_setup_mcp_section(yes=yes)
    if not yes:
        node_ready = _can_run_cloudflare_deploy()
        # Stable identity tokens — the *value* returned by the prompt is one
        # of these short strings, not the user-facing label. That way we can
        # restyle the label freely while keeping the dispatch branches simple.
        local_value = "local"
        deploy_value = "deploy"
        paste_value = "paste"
        skip_value = "skip"

        local_label = (
            "Local apps on this Mac · Claude Desktop, Cursor, Cherry Studio "
            "(recommended · free · ~30 sec)"
        )
        deploy_label = (
            "Deploy to Cloudflare · works from claude.ai, ChatGPT, phone "
            "(free plan; sign-in needed)"
        )
        # When Node 22+ isn't installed we leave the choice *selectable* —
        # picking it falls into the late `if not node_ready:` check at
        # cli.py:3019 which opens the Cloudflare dashboard and tells the
        # user how to install Node. Using questionary's `disabled=` here
        # would grey the row out and trap the user behind a hint they
        # can't act on.
        if not node_ready:
            deploy_label = (
                deploy_label
                + " — needs Node.js 22+; yutome will help"
            )
        paste_label = "I already have a Yutome URL · paste it"
        skip_label = "Skip · run `yutome connect` later"

        choices = [
            ("─── On this Mac ─────────────────────────────────────",),
            (local_label, local_value),
            ("─── From anywhere (web, phone, other devices) ───────",),
            (deploy_label, deploy_value),
            (paste_label, paste_value),
            ("─────────────────────────────────────────────────────",),
            (skip_label, skip_value),
        ]
        while True:
            connect_choice = setup_prompts.select(
                "How do you want to connect Yutome to your assistant?",
                choices=choices,
                default=local_value,
            )
            if connect_choice in {paste_value, deploy_value}:
                assistant_apps = _prompt_assistant_apps(allow_back=True)
                if assistant_apps is None:
                    continue
            else:
                assistant_apps = None
            break
        if connect_choice == skip_value:
            pass
        elif connect_choice == local_value:
            _setup_local_mcp(config)
        elif connect_choice == paste_value:
            typer.echo("")
            typer.echo(
                "Paste the connector URL printed by whoever set up the Worker. The URL can be "
                "the base Worker URL or the full /mcp URL — yutome handles both."
            )
            endpoint = setup_prompts.text("Connector URL")
            endpoint = _normalize_pasted_connector_url(endpoint)
            if not endpoint:
                typer.echo("[WARN] No URL entered; skipping.")
            else:
                typer.echo("")
                typer.echo(
                    "Two secrets pair this laptop to the Worker. Leave blank if you don't have "
                    "them yet — you can save them later with `yutome connect --endpoint ...`."
                )
                typer.echo("  - RELAY_TOKEN   authenticates the laptop bridge to the Worker")
                typer.echo("  - PAIRING_CODE  one-time code Claude/ChatGPT will ask for during OAuth")
                relay_token = setup_prompts.password("Relay token (blank to skip)")
                pairing_code = setup_prompts.text("Pairing code (blank to skip)")
                try:
                    _save_deployed_worker_endpoint(
                        config,
                        endpoint=endpoint,
                        mode="connector_only",
                        worker_name=CAPSULE_PROJECT_NAME,
                        relay_token=relay_token or None,
                        pairing_code=pairing_code or None,
                        assistant_apps=assistant_apps,
                    )
                except ValueError as exc:
                    typer.echo(f"[WARN] Remote endpoint not saved: {exc}")
                else:
                    _finalize_remote_bridge_setup(config_path=config, paths=paths)
        else:
            # Deploy path
            if not node_ready:
                typer.echo("")
                typer.echo(
                    "Deploying needs Node.js 22+ (free, https://nodejs.org). Yutome can open the "
                    "Cloudflare dashboard so you can install Node alongside creating an account."
                )
                if setup_prompts.confirm(
                    "Open the Cloudflare Workers dashboard in your browser?", default=True
                ):
                    webbrowser.open(CLOUDFLARE_WORKERS_DASHBOARD_URL)
                    typer.echo("")
                    typer.echo("Once Node.js 22 LTS or newer is installed, rerun:")
                    typer.echo("  yutome connect --deploy")
                return
            typer.echo("")
            typer.echo(
                "Yutome will deploy a small Worker to your own Cloudflare account (free plan). "
                "If you're not signed in, Wrangler will open your browser to sign in or create "
                "an account — no card required for the free tier."
            )
            if not setup_prompts.confirm("Continue with the Cloudflare deploy?", default=True):
                typer.echo("Skipped. You can run `yutome connect --deploy` later.")
                return
            (
                deployed_url,
                deployed_worker_name,
                deployed_relay_token,
                deployed_pairing_code,
            ) = _deploy_tracked_capsule(paths=paths)
            if deployed_url:
                _save_deployed_worker_endpoint(
                    config,
                    endpoint=deployed_url,
                    mode="connector_only",
                    worker_name=deployed_worker_name,
                    relay_token=deployed_relay_token,
                    pairing_code=deployed_pairing_code,
                    assistant_apps=assistant_apps,
                )
                try:
                    _, _deploy_mcp_url = normalize_endpoint(deployed_url)
                except ValueError:
                    _deploy_mcp_url = deployed_url
                _finalize_remote_bridge_setup(
                    config_path=config,
                    paths=paths,
                    before_persistence=lambda: _print_deploy_secrets_card(
                        _deploy_mcp_url, deployed_pairing_code
                    ),
                )
            else:
                typer.echo("Deploy succeeded, but no workers.dev URL was detected in Wrangler output.")
                typer.echo("Save the endpoint manually with `yutome connect --endpoint <url>`.")


@app.command("connect")
def connect_command(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    endpoint: str | None = typer.Option(
        None,
        "--endpoint",
        help="Cloudflare Worker endpoint URL. Pass either the base URL or the full /mcp URL.",
    ),
    deploy: bool = typer.Option(
        False,
        "--deploy",
        help="Deploy the tracked Cloudflare Worker (cloudflare/yutome-capsule) with Wrangler through npx.",
    ),
    open_cloudflare: bool = typer.Option(
        False,
        "--open-cloudflare",
        help="Open the Cloudflare Workers dashboard after preparing the Worker project.",
    ),
    worker_name: str | None = typer.Option(
        None,
        "--worker-name",
        help="Cloudflare Worker name for generated deployments or for later cleanup of a pasted endpoint.",
    ),
    relay_token: str | None = typer.Option(
        None,
        "--relay-token",
        help="Bridge bearer token for an already-deployed Worker endpoint.",
    ),
    pairing_code: str | None = typer.Option(
        None,
        "--pairing-code",
        help="Pairing code secret for an already-deployed Worker endpoint.",
    ),
    assistant_app: str = typer.Option(
        "all",
        "--app",
        "--assistant",
        help="Assistant instructions to print: claude, chatgpt, both, other, or all.",
    ),
    mode: str = typer.Option(
        "connector-only",
        "--mode",
        help="Remote mode: connector-only for laptop-backed remote MCP, or replica for always-on search foundations.",
    ),
) -> None:
    """Set up remote access so claude.ai, ChatGPT, or any MCP-aware app on your phone or another laptop can reach yutome.

    Deploys a small Cloudflare Worker to your own free Cloudflare account
    (or registers an existing endpoint URL via --endpoint), generates the
    OAuth + pairing secrets needed for the assistant to authenticate, saves
    everything locally, and prints per-assistant pairing instructions. Pick
    the assistant with --app (claude, chatgpt, both, other, all). For
    Claude Desktop / Cursor / other apps on this same machine, you don't
    need this command — paste the local MCP snippet `yutome setup` shows
    you instead.
    """
    try:
        _assistant_app_targets(assistant_app)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    remote_mode = _remote_mode_from_option(mode)
    paths = _prepare_connect_project(config)
    if endpoint is None:
        if relay_token or pairing_code:
            typer.echo("--relay-token and --pairing-code are only used with --endpoint.", err=True)
            raise typer.Exit(code=1)
        _print_cloudflare_connect_instructions()
        typer.echo(f"Remote state will be saved at: {remote_state_path(paths)}")
        if open_cloudflare:
            webbrowser.open(CLOUDFLARE_WORKERS_DASHBOARD_URL)
            typer.echo(f"[OK] Opened Cloudflare Workers dashboard: {CLOUDFLARE_WORKERS_DASHBOARD_URL}")

        if not deploy:
            typer.echo("")
            typer.echo("Tracked TypeScript Worker project lives at:")
            typer.echo(f"  {_tracked_capsule_path()}")
            typer.echo("")
            typer.echo("Run the assisted deploy with:")
            typer.echo("  yutome connect --deploy")
            return

        deployed_url, deployed_worker_name, deployed_relay_token, deployed_pairing_code = (
            _deploy_tracked_capsule(paths=paths)
        )
        if deployed_url is None:
            typer.echo("Deploy succeeded, but no workers.dev URL was detected in Wrangler output.")
            typer.echo("Save the endpoint manually with `yutome connect --endpoint <url>`.")
            return
        _save_deployed_worker_endpoint(
            config,
            endpoint=deployed_url,
            mode=remote_mode,
            worker_name=deployed_worker_name,
            relay_token=deployed_relay_token,
            pairing_code=deployed_pairing_code,
            assistant_apps=assistant_app,
        )
        try:
            _, _deploy_mcp_url = normalize_endpoint(deployed_url)
        except ValueError:
            _deploy_mcp_url = deployed_url
        _finalize_remote_bridge_setup(
            config_path=config,
            paths=paths,
            before_persistence=lambda: _print_deploy_secrets_card(
                _deploy_mcp_url, deployed_pairing_code
            ),
        )
        return
    try:
        state_path = _save_remote_connection(
            config,
            endpoint=endpoint,
            mode=remote_mode,
            worker_name=worker_name,
            relay_token=relay_token,
            pairing_code=pairing_code,
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    state = load_remote_state(paths)
    typer.echo(f"[OK] Saved remote connector state: {state_path}")
    if state is not None:
        typer.echo(f"[OK] Mode: {state.mode}")
        typer.echo(f"[OK] Provider: {state.provider}")
        typer.echo(f"[OK] MCP URL: {state.mcp_url}")
        _print_pairing_next_steps(state)
        _print_connector_next_steps(
            state.mcp_url,
            bridge_configured=bool(state.relay_token),
            assistant_apps=assistant_app,
        )
        if state.relay_token:
            _finalize_remote_bridge_setup(config_path=config, paths=paths)


@app.command("disconnect")
def disconnect_command(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    worker_name: str | None = typer.Option(
        None,
        "--worker-name",
        help="Cloudflare Worker name to remove if it was not saved in local state.",
    ),
    remove_cloudflare: bool = typer.Option(
        True,
        "--remove-cloudflare/--keep-cloudflare",
        help="Remove the Yutome-managed Cloudflare Worker when one is recorded.",
    ),
    keep_state: bool = typer.Option(False, "--keep-state", help="Keep local remote connector state."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be disconnected without changing anything."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Disconnect Yutome from the remote MCP endpoint."""
    _disconnect_remote(
        config=config,
        worker_name=worker_name,
        remove_cloudflare=remove_cloudflare,
        keep_state=keep_state,
        dry_run=dry_run,
        yes=yes,
    )


@app.command()
def init(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite an existing config file with the default config.",
    ),
) -> None:
    """Create config, base artifact directories, and the SQLite catalog."""
    config_written = write_default_config(config, overwrite=force)
    if config.exists() and not config_written:
        typer.echo(f"Using existing config: {config}")
    else:
        typer.echo(f"Wrote config: {config}")

    paths = _load_paths(config)
    paths.ensure_base_dirs()
    bootstrap_catalog(paths.catalog_db)

    typer.echo(f"Initialized data directory: {paths.data_dir}")
    typer.echo(f"Initialized catalog: {paths.catalog_db}")


@app.command("status")
def status_command(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit corpus and remote status as JSON."),
) -> None:
    """Show corpus and remote connector status."""
    app_config, paths = _load_runtime(config)
    corpus_result = api_list(config=app_config, paths=paths, entity="status")
    corpus_payload = corpus_result.model_dump() if hasattr(corpus_result, "model_dump") else corpus_result
    corpus_rows = corpus_payload.get("rows", []) if isinstance(corpus_payload, dict) else []
    corpus = corpus_rows[0] if corpus_rows and isinstance(corpus_rows[0], dict) else {}
    remote = remote_status_payload(paths)
    if json_output:
        _echo_json({"corpus": corpus, "remote": remote})
        return

    typer.echo("Corpus:")
    typer.echo(
        "  "
        f"videos={corpus.get('videos', 0)} "
        f"chunks={corpus.get('chunks', 0)} "
        f"searchable_now={corpus.get('searchable_now', 0)} "
        f"needs_attention={corpus.get('needs_attention', 0)}"
    )
    typer.echo("Remote connector:")
    if remote["configured"]:
        typer.echo(f"  provider={remote['provider']} mode={remote['mode']}")
        typer.echo(f"  mcp_url={remote['mcp_url']}")
        typer.echo(f"  assistant_oauth={remote.get('assistant_oauth_status')}")
        typer.echo(f"  desktop={remote['desktop_connection']}")
        if remote.get("relay_status_error"):
            typer.echo(f"  live_status=unavailable ({remote['relay_status_error']})")
        typer.echo(f"  bridge_token={'configured' if remote.get('relay_token_configured') else 'missing'}")
        typer.echo(f"  pairing_code={'configured' if remote.get('pairing_code_configured') else 'missing'}")
        typer.echo("  oauth_storage=worker OAUTH_KV")
        typer.echo(f"  offline_search={remote['offline_search']}")
    else:
        typer.echo("  not configured")
        typer.echo("  run: yutome connect")


@app.command("usage")
def usage_command(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    ledger: Path | None = typer.Option(
        None,
        "--ledger",
        help="Override the hosted usage JSONL ledger path.",
    ),
    limit: int = typer.Option(20, "--limit", "-n", min=0, help="Maximum usage events to show."),
    summary: bool = typer.Option(False, "--summary", help="Summarize usage totals by provider/service operation."),
    append_demo: bool = typer.Option(False, "--append-demo", help="Append synthetic hosted usage rows for diagnostics."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON output."),
) -> None:
    """Inspect recent hosted provider/search-store usage events."""
    ledger_path = ledger or default_usage_ledger_path(config)
    if append_demo:
        append_demo_usage_events(ledger_path)
    events = JsonlUsageLedger(ledger_path).recent(limit=limit)
    if summary:
        summaries = summarize_usage_events(events)
        if json_output:
            typer.echo(
                json.dumps(
                    [
                        {
                            "operation_key": item.operation_key,
                            "subject": item.subject,
                            "operation": item.operation,
                            "event_count": item.event_count,
                            "status_counts": item.status_counts,
                            "unit_totals": item.unit_totals,
                        }
                        for item in summaries
                    ],
                    indent=2,
                    sort_keys=True,
                )
            )
            return
        if append_demo:
            typer.echo(f"Appended demo hosted usage events to {ledger_path}.")
        if not summaries:
            typer.echo(f"No hosted usage events recorded at {ledger_path}.")
            return
        typer.echo(f"Hosted usage summary from {ledger_path}:")
        for item in summaries:
            units = ", ".join(f"{key}={value:g}" for key, value in item.unit_totals.items()) or "-"
            statuses = ", ".join(f"{key}={value}" for key, value in item.status_counts.items()) or "-"
            typer.echo(f"{item.operation_key} events={item.event_count} statuses[{statuses}] units[{units}]")
        return
    if json_output:
        typer.echo(json.dumps([event.model_dump(mode="json") for event in events], indent=2, sort_keys=True))
        return
    if append_demo:
        typer.echo(f"Appended demo hosted usage events to {ledger_path}.")
    if not events:
        typer.echo(f"No hosted usage events recorded at {ledger_path}.")
        return
    typer.echo(f"Recent hosted usage events from {ledger_path}:")
    for event in events:
        units = ", ".join(f"{key}={value}" for key, value in sorted(event.actual_units.items())) or "-"
        typer.echo(
            f"{event.created_at.isoformat()} {event.workspace_id} {event.subject}.{event.operation} "
            f"{event.status} {event.event_type} units[{units}]"
        )


def _hosted_error(message: str, *, json_output: bool, code: int = 1) -> None:
    if json_output:
        _echo_json({"ok": False, "error": message})
    else:
        typer.echo(message, err=True)
    raise typer.Exit(code=code)


def _hosted_lease_owner(explicit: str | None) -> str:
    return explicit or os.environ.get("RAILWAY_REPLICA_ID") or f"hosted-cli-{os.getpid()}"


def _parse_hosted_phase(value: str, *, json_output: bool) -> str:
    phase = value.lower()
    if phase not in {"phase1", "phase4", "hosted"}:
        _hosted_error("phase must be one of: phase1, phase4, hosted", json_output=json_output, code=2)
    return phase


def _parse_cli_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _echo_hosted_result(value: object, *, json_output: bool, message: str | None = None) -> None:
    if json_output:
        _echo_json(value)
        return
    if message is not None:
        typer.echo(message)
        return
    typer.echo(str(value))


def _format_debug_mapping(values: Mapping[str, Any] | None) -> str:
    if not values:
        return "-"
    parts = []
    for key, value in sorted(values.items()):
        if isinstance(value, float):
            parts.append(f"{key}={value:g}")
        else:
            parts.append(f"{key}={value}")
    return ", ".join(parts)


def _decision_summary(decision: Mapping[str, Any] | None) -> str:
    if not decision:
        return "unknown"
    allowed = decision.get("allowed")
    status = "allowed" if allowed is True else "denied" if allowed is False else "unknown"
    reason = decision.get("reason") or "-"
    message = decision.get("message")
    return f"{status}:{reason}" + (f" ({message})" if message else "")


def _echo_billing_status(result: Mapping[str, Any]) -> None:
    workspace_id = result.get("workspace_id")
    operation = result.get("operation")
    rows = result.get("rows") or []
    scope = f" workspace={workspace_id}" + (f" operation={operation}" if operation else "")
    typer.echo(f"Hosted billing/usage status:{scope}")
    if not rows:
        typer.echo("  no reservations found")
        return
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decision = row.get("entitlement_decision") if isinstance(row.get("entitlement_decision"), Mapping) else {}
        estimated_units = row.get("estimated_units") if isinstance(row.get("estimated_units"), Mapping) else {}
        typer.echo(
            "  "
            f"{row.get('created_at') or '-'} "
            f"{row.get('operation_key')} "
            f"reservation={row.get('reservation_status')} "
            f"decision={_decision_summary(decision)}"
        )
        typer.echo(
            "    "
            f"job={row.get('job_id') or '-'}({row.get('job_status') or '-'}) "
            f"operation={row.get('operation_id') or '-'}({row.get('operation_status') or '-'}) "
            f"units[{_format_debug_mapping(estimated_units)}]"
        )
        if row.get("job_error_code") or row.get("job_error_message"):
            typer.echo(f"    job_error={row.get('job_error_code') or '-'} {row.get('job_error_message') or ''}".rstrip())
        usage_events = row.get("usage_events") or []
        if usage_events:
            typer.echo("    usage_events:")
            for event in usage_events:
                if not isinstance(event, Mapping):
                    continue
                actual_units = event.get("actual_units") if isinstance(event.get("actual_units"), Mapping) else {}
                error = f" error={event.get('error_code')}" if event.get("error_code") else ""
                typer.echo(
                    "      "
                    f"{event.get('id')} {event.get('status')} {event.get('event_type')} "
                    f"units[{_format_debug_mapping(actual_units)}]{error}"
                )
        else:
            typer.echo("    usage_events: none")
        billing_exports = row.get("billing_exports") or []
        if billing_exports:
            typer.echo("    billing_exports:")
            for export in billing_exports:
                if not isinstance(export, Mapping):
                    continue
                last_error = export.get("last_error") if isinstance(export.get("last_error"), Mapping) else {}
                error = f" error[{_format_debug_mapping(last_error)}]" if last_error else ""
                typer.echo(
                    "      "
                    f"{export.get('id')} status={export.get('replay_status')} "
                    f"external_event_id={export.get('external_event_id') or '-'} "
                    f"dedupe={export.get('source_event_dedupe_key')}"
                    f"{error}"
                )
        else:
            typer.echo("    billing_exports: none")


@hosted_app.command("api")
def hosted_api(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address for the hosted MCP query API."),
    port: int = typer.Option(8000, "--port", min=1, help="Bind port. On Railway, pass --port $PORT."),
    log_level: str = typer.Option("info", "--log-level", help="Uvicorn log level."),
) -> None:
    """Run the hosted MCP query API for Railway/API deployments."""
    try:
        api_app = _hosted_api_app(config)
        _run_hosted_api_app(api_app, host=host, port=port, log_level=log_level)
    except (HostedRuntimeError, RuntimeError) as exc:
        _hosted_error(str(exc), json_output=False)


@hosted_app.command("migrate")
def hosted_migrate(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    phase: str = typer.Option("hosted", "--phase", help="Migration phase: phase1, phase4, or hosted."),
    json_output: bool = typer.Option(False, "--json", help="Emit migration result as JSON."),
) -> None:
    """Apply hosted Postgres migrations."""
    phase_value = _parse_hosted_phase(phase, json_output=json_output)
    try:
        applied = _hosted_runner(config).migrate(phase=phase_value)  # type: ignore[arg-type]
    except HostedRuntimeError as exc:
        _hosted_error(str(exc), json_output=json_output)
    payload = {"ok": True, "phase": phase_value, "applied": applied}
    _echo_hosted_result(payload, json_output=json_output, message=f"Applied {applied} hosted migration statements ({phase_value}).")


@hosted_app.command("db-check")
def hosted_db_check(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit database check as JSON."),
) -> None:
    """Check hosted Postgres configuration and required extensions."""
    try:
        result = _hosted_runner(config).db_check()
    except HostedRuntimeError as exc:
        _hosted_error(str(exc), json_output=json_output)
    if json_output:
        _echo_json(result)
        return
    status = "ok" if result.ok else "not ready"
    typer.echo(f"Hosted database: {status}")
    typer.echo(f"  url_env={result.url_env} configured={result.url_configured}")
    typer.echo(f"  reachable={result.database_reachable}")
    if result.extensions:
        extensions = ", ".join(f"{name}={installed}" for name, installed in sorted(result.extensions.items()))
        typer.echo(f"  extensions: {extensions}")
    if result.error:
        typer.echo(f"  error={result.error}")


@hosted_app.command("billing-status")
def hosted_billing_status(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    workspace_id: str | None = typer.Option(None, "--workspace-id", help="Hosted workspace id."),
    operation: str | None = typer.Option(None, "--operation", help="Filter by operation key or operation name."),
    limit: int = typer.Option(20, "--limit", min=1, help="Maximum recent reservations to inspect."),
    json_output: bool = typer.Option(False, "--json", help="Emit billing/usage status as JSON."),
) -> None:
    """Inspect hosted usage reservations, decisions, events, and billing exports."""
    runner = _hosted_runner(config)
    workspace = workspace_id or runner.config.hosted.workspace_id
    if not workspace:
        _hosted_error("Set --workspace-id or [hosted].workspace_id before running billing-status.", json_output=json_output, code=2)
    try:
        result = runner.billing_status(workspace_id=workspace, limit=limit, operation=operation)
    except HostedRuntimeError as exc:
        _hosted_error(str(exc), json_output=json_output)
    if json_output:
        _echo_json(result)
        return
    _echo_billing_status(result)


@hosted_app.command("billing-export-worker")
def hosted_billing_export_worker(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    once: bool = typer.Option(False, "--once", help="Run one billing export claim tick and exit."),
    lease_owner: str | None = typer.Option(None, "--lease-owner", help="Lease owner id. Defaults to RAILWAY_REPLICA_ID or this process."),
    limit: int = typer.Option(100, "--limit", min=1, help="Maximum billing export rows to claim per tick."),
    poll_interval: float = typer.Option(10.0, "--poll-interval", min=0.1, help="Loop sleep when --once is not set."),
    json_output: bool = typer.Option(False, "--json", help="Emit billing export tick result as JSON."),
) -> None:
    """Export pending usage mirror events to Polar when POLAR_ACCESS_TOKEN is configured."""
    runner = _hosted_runner(config)
    owner = _hosted_lease_owner(lease_owner)
    while True:
        try:
            result = runner.billing_export_once(lease_owner=owner, limit=limit)
        except HostedRuntimeError as exc:
            _hosted_error(str(exc), json_output=json_output)
        _echo_hosted_result(
            result,
            json_output=json_output,
            message=f"Billing export tick claimed {result.affected_rows} rows; succeeded={result.succeeded} failed={result.failed}.",
        )
        if once:
            return
        time.sleep(poll_interval)


@hosted_app.command("reconcile-balance")
def hosted_reconcile_balance(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    workspace_id: str | None = typer.Option(None, "--workspace-id", help="Hosted workspace id."),
    entitlement_policy_id: str = typer.Option(..., "--entitlement-policy-id", help="Policy id to write on workspace_balances."),
    period_start: str = typer.Option(..., "--period-start", help="Inclusive billing period start timestamp."),
    period_end: str = typer.Option(..., "--period-end", help="Exclusive billing period end timestamp."),
    json_output: bool = typer.Option(False, "--json", help="Emit reconciled balance as JSON."),
) -> None:
    """Recompute a workspace balance from credit ledger entries and settled usage events."""
    runner = _hosted_runner(config)
    workspace = workspace_id or runner.config.hosted.workspace_id
    if not workspace:
        _hosted_error("Set --workspace-id or [hosted].workspace_id before running reconcile-balance.", json_output=json_output, code=2)
    try:
        result = runner.reconcile_balance(
            workspace_id=workspace,
            entitlement_policy_id=entitlement_policy_id,
            period_start_at=_parse_cli_datetime(period_start),
            period_end_at=_parse_cli_datetime(period_end),
        )
    except (HostedRuntimeError, ValueError) as exc:
        _hosted_error(str(exc), json_output=json_output)
    _echo_hosted_result(result, json_output=json_output, message=f"Reconciled balance for workspace {workspace}.")


@hosted_app.command("search-smoke")
def hosted_search_smoke(
    query: str = typer.Argument(..., help="Lexical search query to smoke test."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    workspace_id: str | None = typer.Option(None, "--workspace-id", help="Hosted workspace id."),
    limit: int = typer.Option(3, "--limit", min=1, help="Maximum rows to return."),
    json_output: bool = typer.Option(False, "--json", help="Emit search result as JSON."),
) -> None:
    """Run a hosted Postgres lexical search smoke query."""
    runner = _hosted_runner(config)
    workspace = workspace_id or runner.config.hosted.workspace_id
    if not workspace:
        _hosted_error("Set --workspace-id or [hosted].workspace_id before running search-smoke.", json_output=json_output, code=2)
    try:
        result = runner.search_smoke(workspace_id=workspace, query=query, limit=limit)
    except HostedRuntimeError as exc:
        _hosted_error(str(exc), json_output=json_output)
    _echo_hosted_result(result, json_output=json_output, message=f"Search smoke returned {len(result.get('rows', []))} rows.")


@hosted_app.command("login")
def hosted_login(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    app_url: str | None = typer.Option(None, "--app-url", help="Hosted Yutome app URL."),
    api_url: str | None = typer.Option(None, "--api-url", help="Hosted Yutome API URL."),
    port: int = typer.Option(0, "--port", min=0, max=65535, help="Local callback port. 0 chooses a free port."),
    open_browser: bool = typer.Option(True, "--open-browser/--print-url", help="Open the hosted login URL in a browser."),
    json_output: bool = typer.Option(False, "--json", help="Emit hosted auth state as JSON."),
) -> None:
    """Authorize this CLI against your hosted Yutome account."""
    try:
        result = _run_hosted_login_flow(
            config_path=config,
            app_url=app_url,
            api_url=api_url,
            port=port,
            open_browser=open_browser,
        )
    except (HostedCliLoginError, HostedCliApiError, ValueError) as exc:
        _hosted_error(str(exc), json_output=json_output)
    if json_output:
        typer.echo(json.dumps(result, indent=2, sort_keys=True))
        return
    typer.echo(f"[OK] Hosted CLI connected to workspace {result['workspace_id']}.")


@hosted_app.command("jobs")
def hosted_jobs(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    limit: int = typer.Option(25, "--limit", min=1, max=100, help="Maximum hosted jobs to return."),
    json_output: bool = typer.Option(False, "--json", help="Emit hosted jobs as JSON."),
) -> None:
    """Read recent hosted jobs for the logged-in workspace."""
    try:
        app_config = load_config(config)
        paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
        auth = _load_hosted_auth(paths)
        api_base = str(auth.get("api_url") or app_config.hosted.api_url or DEFAULT_HOSTED_API_URL)
        result = _hosted_api_request_json(
            api_base,
            f"/account/jobs?{urllib.parse.urlencode({'limit': limit})}",
            method="GET",
            access_token=str(auth["access_token"]),
        )
    except (HostedCliLoginError, HostedCliApiError, ValueError) as exc:
        _hosted_error(str(exc), json_output=json_output)
    _echo_hosted_result(result, json_output=json_output, message=f"Fetched {len(result.get('jobs', []))} hosted job(s).")


@hosted_app.command("source-add")
def hosted_source_add(
    source_url: str = typer.Argument(..., help="Public YouTube channel, handle, playlist, or video URL."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    workspace_id: str | None = typer.Option(None, "--workspace-id", help="Hosted workspace id."),
    display_name: str | None = typer.Option(None, "--display-name", help="Optional source display name."),
    cadence_seconds: int = typer.Option(900, "--cadence-seconds", min=1, help="Source refresh cadence."),
    max_new_videos: int = typer.Option(25, "--max-new-videos", min=1, help="Maximum discovered videos to enqueue per run."),
    refresh_enabled: bool = typer.Option(True, "--refresh/--no-refresh", help="Create or update the source refresh policy."),
    json_output: bool = typer.Option(False, "--json", help="Emit seed result as JSON."),
) -> None:
    """Create or update a hosted source and its refresh policy."""
    runner = _hosted_runner(config)
    workspace = workspace_id or runner.config.hosted.workspace_id
    if not workspace:
        _hosted_error("Set --workspace-id or [hosted].workspace_id before running source-add.", json_output=json_output, code=2)
    try:
        result = runner.source_add(
            workspace_id=workspace,
            source_url=source_url,
            display_name=display_name,
            cadence_seconds=cadence_seconds,
            max_new_videos_per_run=max_new_videos,
            refresh_enabled=refresh_enabled,
        )
    except (HostedRuntimeError, ValueError) as exc:
        _hosted_error(str(exc), json_output=json_output)
    source_id = result.get("source_id") if isinstance(result, dict) else result.source_id
    _echo_hosted_result(result, json_output=json_output, message=f"Seeded hosted source {source_id}.")


@hosted_app.command("enqueue-index-video")
def hosted_enqueue_index_video(
    source_url: str = typer.Argument(..., help="Public YouTube video URL or 11-character video id."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    workspace_id: str | None = typer.Option(None, "--workspace-id", help="Hosted workspace id."),
    display_name: str | None = typer.Option(None, "--display-name", help="Optional source display name."),
    priority: int = typer.Option(100, "--priority", min=1, help="Job priority; lower runs sooner."),
    json_output: bool = typer.Option(False, "--json", help="Emit seed result as JSON."),
) -> None:
    """Seed a real hosted index_video job for the worker."""
    runner = _hosted_runner(config)
    workspace = workspace_id or runner.config.hosted.workspace_id
    if not workspace:
        _hosted_error("Set --workspace-id or [hosted].workspace_id before running enqueue-index-video.", json_output=json_output, code=2)
    try:
        result = runner.enqueue_index_video(
            workspace_id=workspace,
            source_url=source_url,
            display_name=display_name,
            priority=priority,
        )
    except (HostedRuntimeError, ValueError) as exc:
        _hosted_error(str(exc), json_output=json_output)
    job_id = result.get("job_id") if isinstance(result, dict) else result.job_id
    _echo_hosted_result(result, json_output=json_output, message=f"Queued hosted index job {job_id}.")


@hosted_app.command("real-indexing-smoke")
def hosted_real_indexing_smoke(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    workspace_id: str | None = typer.Option(None, "--workspace-id", help="Hosted workspace id."),
    source_url: str = typer.Option(
        "https://www.youtube.com/watch?v=OEDoJyhQhXs",
        "--source-url",
        help="Public YouTube video URL or id to index through the real worker path.",
    ),
    migrate: bool = typer.Option(False, "--migrate", help="Apply hosted migrations before enqueueing."),
    phase: str = typer.Option("hosted", "--phase", help="Migration phase when --migrate is set: phase1, phase4, or hosted."),
    lease_owner: str | None = typer.Option(None, "--lease-owner", help="Worker lease owner for the smoke run."),
    json_output: bool = typer.Option(False, "--json", help="Emit smoke result as JSON."),
) -> None:
    """Enqueue a real video and run one real hosted worker tick."""
    phase_value = _parse_hosted_phase(phase, json_output=json_output)
    runner = _hosted_runner(config)
    workspace = workspace_id or runner.config.hosted.workspace_id
    if not workspace:
        _hosted_error("Set --workspace-id or [hosted].workspace_id before running real-indexing-smoke.", json_output=json_output, code=2)
    try:
        result = runner.real_indexing_smoke(
            workspace_id=workspace,
            source_url=source_url,
            migrate=migrate,
            migration_phase=phase_value,  # type: ignore[arg-type]
            lease_owner=_hosted_lease_owner(lease_owner),
        )
    except (HostedRuntimeError, ValueError) as exc:
        _hosted_error(str(exc), json_output=json_output)
    job_id = result.get("job_id") if isinstance(result, dict) else result.job_id
    ok = result.get("ok") if isinstance(result, dict) else result.ok
    _echo_hosted_result(result, json_output=json_output, message=f"Real indexing smoke job {job_id}: ok={ok}.")


@hosted_app.command("mock-indexing-smoke")
def hosted_mock_indexing_smoke(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    workspace_id: str | None = typer.Option(None, "--workspace-id", help="Hosted workspace id."),
    migrate: bool = typer.Option(False, "--migrate", help="Apply hosted migrations before writing smoke rows."),
    phase: str = typer.Option("hosted", "--phase", help="Migration phase when --migrate is set: phase1, phase4, or hosted."),
    query: str | None = typer.Option(None, "--query", help="Hybrid search query. Defaults to the mocked transcript text."),
    source_url: str = typer.Option(
        "https://www.youtube.com/watch?v=OEDoJyhQhXs",
        "--source-url",
        help="Public YouTube source URL or video id to mock.",
    ),
    limit: int = typer.Option(3, "--limit", min=1, help="Maximum rows to return."),
    json_output: bool = typer.Option(False, "--json", help="Emit smoke result as JSON."),
) -> None:
    """Write mocked hosted indexing rows and query them from Postgres."""
    phase_value = _parse_hosted_phase(phase, json_output=json_output)
    runner = _hosted_runner(config)
    workspace = workspace_id or runner.config.hosted.workspace_id
    if not workspace:
        _hosted_error("Set --workspace-id or [hosted].workspace_id before running mock-indexing-smoke.", json_output=json_output, code=2)
    try:
        result = runner.mock_indexing_smoke(
            workspace_id=workspace,
            migrate=migrate,
            migration_phase=phase_value,  # type: ignore[arg-type]
            query=query,
            limit=limit,
            source_url=source_url,
        )
    except HostedRuntimeError as exc:
        _hosted_error(str(exc), json_output=json_output)
    _echo_hosted_result(
        result,
        json_output=json_output,
        message=f"Mock indexing smoke wrote {result.operations_executed} operations and returned {len(result.rows)} rows.",
    )


@hosted_app.command("worker")
def hosted_worker(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    once: bool = typer.Option(False, "--once", help="Run one claim tick and exit."),
    lease_owner: str | None = typer.Option(None, "--lease-owner", help="Lease owner id. Defaults to RAILWAY_REPLICA_ID or this process."),
    workspace_id: str | None = typer.Option(None, "--workspace-id", help="Optional workspace scope."),
    limit: int = typer.Option(1, "--limit", min=1, help="Maximum jobs to claim per tick."),
    lease_seconds: int = typer.Option(900, "--lease-seconds", min=1, help="Job lease duration in seconds."),
    poll_interval: float = typer.Option(5.0, "--poll-interval", min=0.1, help="Loop sleep when --once is not set."),
    json_output: bool = typer.Option(False, "--json", help="Emit worker tick result as JSON."),
) -> None:
    """Run the hosted worker claim loop or a single worker tick."""
    runner = _hosted_runner(config)
    owner = _hosted_lease_owner(lease_owner)
    while True:
        try:
            result = runner.worker_once(
                lease_owner=owner,
                limit=limit,
                lease_seconds=lease_seconds,
                workspace_id=workspace_id,
            )
        except HostedRuntimeError as exc:
            _hosted_error(str(exc), json_output=json_output)
        _echo_hosted_result(result, json_output=json_output, message=f"Worker tick claimed {result.affected_rows or 0} jobs.")
        if once:
            return
        time.sleep(poll_interval)


@hosted_app.command("source-refresh-tick")
def hosted_source_refresh_tick(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    lease_owner: str | None = typer.Option(None, "--lease-owner", help="Refresh lock owner. Defaults to RAILWAY_REPLICA_ID or this process."),
    limit: int = typer.Option(25, "--limit", min=1, help="Maximum due source policies to lock."),
    lock_seconds: int = typer.Option(900, "--lock-seconds", min=1, help="Refresh policy lock duration."),
    json_output: bool = typer.Option(False, "--json", help="Emit tick result as JSON."),
) -> None:
    """Claim due hosted source refresh policies."""
    try:
        result = _hosted_runner(config).source_refresh_tick(
            lease_owner=_hosted_lease_owner(lease_owner),
            limit=limit,
            lock_seconds=lock_seconds,
        )
    except HostedRuntimeError as exc:
        _hosted_error(str(exc), json_output=json_output)
    _echo_hosted_result(result, json_output=json_output, message=f"Source refresh tick claimed {result.affected_rows or 0} policies.")


@hosted_app.command("maintenance-tick")
def hosted_maintenance_tick(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    limit: int = typer.Option(100, "--limit", min=1, help="Maximum expired rows per maintenance category."),
    json_output: bool = typer.Option(False, "--json", help="Emit tick result as JSON."),
) -> None:
    """Release expired hosted job and source-refresh leases."""
    try:
        result = _hosted_runner(config).maintenance_tick(limit=limit)
    except HostedRuntimeError as exc:
        _hosted_error(str(exc), json_output=json_output)
    _echo_hosted_result(result, json_output=json_output, message=f"Maintenance tick released {result.affected_rows or 0} rows.")


@app.command()
def doctor(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
) -> None:
    """Check local project readiness."""
    failures = 0

    python_ok = sys.version_info >= (3, 11)
    _status(
        python_ok,
        "Python runtime",
        f"{platform.python_version()} at {sys.executable}",
    )
    failures += 0 if python_ok else 1

    config_ok = config.exists()
    _status(config_ok, "Config file", str(config))
    if not config_ok:
        raise typer.Exit(code=1)

    try:
        paths = _load_paths(config)
        paths_ok = True
    except Exception as exc:  # noqa: BLE001 - doctor should report config errors cleanly.
        _status(False, "Config parse", str(exc))
        raise typer.Exit(code=1) from exc

    paths.ensure_base_dirs()
    _status(paths_ok, "Data directory", str(paths.data_dir))
    _status(paths.artifacts_dir.exists(), "Artifact root", str(paths.artifacts_dir))
    _status(paths.lancedb_dir.exists(), "LanceDB directory", str(paths.lancedb_dir))

    bootstrap_catalog(paths.catalog_db)
    catalog_ok = catalog_is_initialized(paths.catalog_db)
    _status(catalog_ok, "SQLite catalog", str(paths.catalog_db))
    failures += 0 if catalog_ok else 1

    fts_ok = fts5_available()
    _status(fts_ok, "SQLite FTS5")
    failures += 0 if fts_ok else 1

    ytdlp_module = _module_available("yt_dlp")
    ytdlp_command_ok, ytdlp_command_detail = _command_version("yt-dlp")
    _status(
        bool(ytdlp_module or ytdlp_command_ok),
        "yt-dlp availability",
        "python module" if ytdlp_module else ytdlp_command_detail,
    )
    if not (ytdlp_module or ytdlp_command_ok):
        typer.echo("      install with: uv sync")
    _status(
        _module_available("youtube_transcript_api"),
        "youtube-transcript-api availability",
        "install with: uv sync",
    )
    _status(
        _module_available("lancedb"),
        "LanceDB availability",
        "install with: uv sync",
    )
    _status(
        _module_available("faster_whisper"),
        "faster-whisper availability",
        "install with: uv sync",
    )
    _status(
        _module_available("voyageai"),
        "Voyage client availability",
        "install with: uv sync",
    )
    _status(
        _module_available("google.genai"),
        "Gemini client availability",
        "install with: uv sync",
    )

    if failures:
        raise typer.Exit(code=1)


@app.command("add")
def add_sources(
    targets: list[str] = typer.Argument(..., help="YouTube channel or video URL, handle, or id."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    title: str | None = typer.Option(None, "--title", help="Optional display title for one source."),
    selected: bool = typer.Option(True, "--selected/--unselected", help="Include source in default sync runs."),
    hosted: bool = typer.Option(False, "--hosted", help="Upload sources to hosted Yutome instead of local SQLite."),
) -> None:
    """Add YouTube sources to the local library."""
    app_config = load_config(config)
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    if hosted or app_config.hosted.enabled:
        descriptors: list[dict[str, Any]] = []
        for target in targets:
            source = source_from_input(target, title=title if len(targets) == 1 else None, import_source="cli")
            if source is not None:
                descriptors.append(_library_source_import_descriptor(source, selected=selected))
        if not descriptors:
            typer.echo("Uploaded 0 hosted sources; queued 0 jobs.")
            return
        try:
            result = _hosted_import_sources(app_config=app_config, paths=paths, descriptors=descriptors)
        except (HostedCliLoginError, HostedCliApiError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        imported = len(result.get("imported", []))
        jobs = len(result.get("jobs", []))
        typer.echo(f"Uploaded {imported} hosted source{'s' if imported != 1 else ''}; queued {jobs} job{'s' if jobs != 1 else ''}.")
        return
    bootstrap_catalog(paths.catalog_db)
    imported = 0
    with connect_catalog(paths.catalog_db) as connection:
        for target in targets:
            source = source_from_input(target, title=title if len(targets) == 1 else None, import_source="manual")
            if source is None:
                continue
            upsert_library_source(connection, source, selected=selected)
            imported += 1
        connection.commit()
    typer.echo(f"Added {imported} source{'s' if imported != 1 else ''}.")


@app.command("import")
def import_command(
    path: Path = typer.Argument(..., exists=True, readable=True, help="CSV, OPML/XML, or plain URL list."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    selected: bool = typer.Option(True, "--selected/--unselected", help="Include imported sources in default sync runs."),
    hosted: bool = typer.Option(False, "--hosted", help="Upload imported sources to hosted Yutome instead of local SQLite."),
) -> None:
    """Import sources from CSV, OPML/XML, or a plain list."""
    app_config = load_config(config)
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    sources = import_sources_from_file(path, selected=selected)
    if hosted or app_config.hosted.enabled:
        if not sources:
            typer.echo("Uploaded 0 hosted sources; queued 0 jobs.")
            return
        try:
            result = _hosted_import_sources(
                app_config=app_config,
                paths=paths,
                descriptors=[_library_source_import_descriptor(source, selected=selected) for source in sources],
            )
        except (HostedCliLoginError, HostedCliApiError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        imported = len(result.get("imported", []))
        jobs = len(result.get("jobs", []))
        typer.echo(f"Uploaded {imported} hosted source{'s' if imported != 1 else ''}; queued {jobs} job{'s' if jobs != 1 else ''}.")
        return
    bootstrap_catalog(paths.catalog_db)
    with connect_catalog(paths.catalog_db) as connection:
        for source in sources:
            upsert_library_source(connection, source, selected=selected)
        connection.commit()
    typer.echo(f"Imported {len(sources)} source{'s' if len(sources) != 1 else ''}.")


@app.command("import-youtube")
def import_youtube(
    target: str | None = typer.Argument(
        None,
        help=(
            "Optional channel URL, handle, or channel id. Omit to import the signed-in "
            "user's subscriptions; pass a channel to import its public subscriptions."
        ),
    ),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    port: int = typer.Option(0, "--port", min=0, max=65535, help="Local OAuth callback port. 0 chooses a free port."),
    open_browser: bool = typer.Option(True, "--open-browser/--print-url", help="Open the OAuth URL in a browser."),
    selected: bool = typer.Option(True, "--selected/--unselected", help="Include imported channels in default sync runs."),
    hosted: bool = typer.Option(False, "--hosted", help="Upload imported public sources to hosted Yutome instead of local SQLite."),
) -> None:
    """Import YouTube subscriptions from the signed-in user or a public channel."""
    project_root = _project_root(config)
    env_path = project_root / ".env"
    load_dotenv(env_path)
    app_config = apply_env_to_config(load_config(config))
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    bootstrap_catalog(paths.catalog_db)
    try:
        channels = _fetch_youtube_import_channels(
            target=target,
            app_config=app_config,
            paths=paths,
            project_root=project_root,
            env_path=env_path,
            port=port,
            open_browser=open_browser,
            status_callback=typer.echo,
        )
    except YouTubeImportError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if hosted:
        try:
            result = _hosted_import_sources(
                app_config=app_config,
                paths=paths,
                descriptors=[_channel_import_descriptor(channel, selected=selected) for channel in channels],
            )
        except (HostedCliLoginError, HostedCliApiError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        imported = len(result.get("imported", []))
        source = channels[0].import_source if channels else "youtube"
        jobs = len(result.get("jobs", []))
        policies = len(result.get("refresh_policies", []))
        typer.echo(
            f"Uploaded {imported} YouTube subscription channel{'s' if imported != 1 else ''} "
            f"from {source} to hosted Yutome ({jobs} video job{'s' if jobs != 1 else ''}, "
            f"{policies} refresh polic{'ies' if policies != 1 else 'y'})."
        )
        return
    imported = _save_imported_channels(paths, channels, selected=selected)
    source = channels[0].import_source if channels else "youtube"
    typer.echo(
        f"Imported {imported} YouTube subscription channel{'s' if imported != 1 else ''} "
        f"from {source}."
    )


@app.command("select")
def select_source(
    selector: str = typer.Argument(..., help="Source id, URL, handle, title, or 'all'."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
) -> None:
    """Include matching sources in default sync runs."""
    app_config = load_config(config)
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    bootstrap_catalog(paths.catalog_db)
    with connect_catalog(paths.catalog_db) as connection:
        count = set_library_source_selected(connection, selector=selector, selected=True)
        connection.commit()
    typer.echo(f"Selected {count} source{'s' if count != 1 else ''}.")


@app.command("unselect")
def unselect_source(
    selector: str = typer.Argument(..., help="Source id, URL, handle, title, or 'all'."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
) -> None:
    """Exclude matching sources from default sync runs."""
    app_config = load_config(config)
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    bootstrap_catalog(paths.catalog_db)
    with connect_catalog(paths.catalog_db) as connection:
        count = set_library_source_selected(connection, selector=selector, selected=False)
        connection.commit()
    typer.echo(f"Unselected {count} source{'s' if count != 1 else ''}.")


@quality_app.command("upgrade")
def quality_upgrade(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    video_id: str | None = typer.Option(None, "--video-id", help="Upgrade one video id."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Maximum active transcripts to upgrade."),
    video_workers: int | None = typer.Option(
        None,
        "--video-workers",
        min=1,
        help="Parallel videos to clean. Total Gemini request concurrency is roughly video-workers * concurrency.",
    ),
    batch_segments: int | None = typer.Option(
        None,
        "--batch-segments",
        min=1,
        help="Number of caption segments per LLM request.",
    ),
    concurrency: int | None = typer.Option(
        None,
        "--concurrency",
        min=1,
        help="Parallel LLM cleanup requests per video.",
    ),
    max_patch_retries: int | None = typer.Option(
        None,
        "--max-patch-retries",
        min=0,
        max=5,
        help="Retry invalid LLM correction patches before marking a video failed.",
    ),
    source_filter: list[str] | None = typer.Option(
        None,
        "--source-filter",
        help="Only upgrade active transcript sources matching this prefix.",
    ),
    all_transcripts: bool = typer.Option(
        False,
        "--all",
        help="Upgrade all matching transcripts instead of only heuristic cleanup candidates.",
    ),
    rebuild_vectors: bool = typer.Option(
        False,
        "--rebuild-vectors",
        help="Rebuild LanceDB vectors after transcript text changes.",
    ),
) -> None:
    """Create LLM-cleaned transcript versions from already-indexed active transcripts."""
    load_dotenv(_project_root(config) / ".env")
    app_config = apply_env_to_config(load_config(config))
    cleanup_updates = {}
    if video_workers is not None:
        cleanup_updates["video_workers"] = video_workers
    if batch_segments is not None:
        cleanup_updates["batch_segments"] = batch_segments
    if concurrency is not None:
        cleanup_updates["concurrency"] = concurrency
    if max_patch_retries is not None:
        cleanup_updates["max_patch_retries"] = max_patch_retries
    app_config_updates = {
        "gemini": app_config.gemini.model_copy(update={"enabled": True})
    }
    if cleanup_updates:
        app_config_updates["transcript_cleanup"] = app_config.transcript_cleanup.model_copy(update=cleanup_updates)
    app_config = app_config.model_copy(update=app_config_updates)
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    stats = upgrade_active_transcripts(
        config=app_config,
        paths=paths,
        video_id=video_id,
        limit=limit,
        source_filters=source_filter,
        quality_gate=not all_transcripts and video_id is None,
        progress=typer.echo,
    )
    typer.echo(f"Scanned transcripts: {stats.scanned}")
    typer.echo(f"Upgraded transcripts: {stats.upgraded}")
    typer.echo(f"Skipped unchanged: {stats.skipped_unchanged}")
    typer.echo(f"Skipped missing: {stats.skipped_missing}")
    typer.echo(f"Skipped by quality heuristic: {stats.skipped_quality}")
    typer.echo(f"Failed upgrades: {stats.failed}")
    typer.echo(f"Chunks saved: {stats.chunks_saved}")
    if rebuild_vectors and stats.upgraded:
        app_config = app_config.model_copy(
            update={"embeddings": app_config.embeddings.model_copy(update={"enabled": True})}
        )
        with connect_catalog(paths.catalog_db) as connection:
            vector_stats = rebuild_lancedb_chunks(
                connection=connection,
                config=app_config,
                lancedb_dir=paths.lancedb_dir,
            )
        typer.echo(f"Rebuilt vectors: {vector_stats.embedded_chunks}")
        if vector_stats.message:
            typer.echo(vector_stats.message)
    elif stats.upgraded:
        typer.echo("Vector index note: run `yutome rebuild-vectors` to refresh semantic/hybrid retrieval.")


@app.command()
def sync(
    target: str | None = typer.Argument(
        None,
        help=(
            "YouTube channel or video URL/id. Omit to sync selected sources."
        ),
    ),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    all_channels: bool = typer.Option(
        False,
        "--all",
        help="Sync every selected source in the local library.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Limit videos discovered per tab; omit for the full channel.",
    ),
    embed: bool | None = typer.Option(
        None,
        "--embed/--no-embed",
        help="Generate Voyage embeddings and index them in LanceDB. Defaults to embeddings.enabled in yutome.toml.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Reprocess videos even when an active transcript already exists.",
    ),
    max_process: int | None = typer.Option(
        None,
        "--max-process",
        min=1,
        help="Maximum non-indexed videos to process in this run after discovery. Defaults to backfill.max_videos_per_run from yutome.toml.",
    ),
    workers: int | None = typer.Option(
        None,
        "--workers",
        min=1,
        max=32,
        help="Number of videos to process concurrently. Defaults to backfill.workers from yutome.toml. Use --workers 1 for serial / safest mode on residential IP.",
    ),
    retry_failed: bool = typer.Option(
        False,
        "--retry-failed",
        help="Retry videos previously marked failed or deferred.",
    ),
    use_catalog: bool = typer.Option(
        False,
        "--use-catalog",
        help="Use already-discovered catalog videos instead of crawling channel tabs first.",
    ),
    verbose_skips: bool = typer.Option(
        False,
        "--verbose-skips/--quiet-skips",
        help="Print every skipped existing/failed video.",
    ),
    asr_fallback: bool = typer.Option(
        False,
        "--asr-fallback",
        help="Use local ASR when caption/subtitle fetch fails.",
    ),
    gemini_fallback: bool = typer.Option(
        False,
        "--gemini-fallback",
        help="Use Gemini video understanding when caption/subtitle fetch fails.",
    ),
    stop_on_rate_limit: bool = typer.Option(
        False,
        "--stop-on-rate-limit/--continue-on-rate-limit",
        help="Stop submitting new videos in the current stage when a likely YouTube rate limit/block is detected.",
    ),
    sleep_seconds: float = typer.Option(
        0.0,
        "--sleep",
        min=0.0,
        help="Delay between per-video transcript requests. Defaults to 0 since yt-dlp's internal --sleep-requests/--sleep-subtitles already throttles (and is reduced to 0 when a proxy is in use).",
    ),
    status_filter: list[str] | None = typer.Option(
        None,
        "--status-filter",
        help=(
            "Only process catalog videos whose ingest_status equals or starts with this value. "
            "Can be passed multiple times, e.g. --status-filter 'deferred: rate_limited'."
        ),
    ),
    source_filter: list[str] | None = typer.Option(
        None,
        "--source-filter",
        help=(
            "Only process videos whose active transcript source equals or starts with this value. "
            "Use with --force to refresh indexed fallback transcripts."
        ),
    ),
    max_duration_seconds: int | None = typer.Option(
        None,
        "--max-duration-seconds",
        min=1,
        help="Only process videos at or below this duration.",
    ),
    shortest_first: bool = typer.Option(
        False,
        "--shortest-first",
        help="Process shorter candidate videos first.",
    ),
    proxy_retries_when_blocked: int | None = typer.Option(
        None,
        "--proxy-retries-when-blocked",
        min=1,
        help="Override Webshare transcript retries for this run.",
    ),
) -> None:
    """Discover and index YouTube sources."""
    load_dotenv(_project_root(config) / ".env")
    app_config = apply_env_to_config(load_config(config))
    if proxy_retries_when_blocked is not None:
        app_config = app_config.model_copy(
            update={
                "proxy": app_config.proxy.model_copy(
                    update={"webshare_retries_when_blocked": proxy_retries_when_blocked}
                )
            }
        )
    effective_embed = app_config.embeddings.enabled if embed is None else embed
    if effective_embed:
        app_config = app_config.model_copy(
            update={"embeddings": app_config.embeddings.model_copy(update={"enabled": True})}
        )
    if gemini_fallback:
        app_config = app_config.model_copy(
            update={
                "gemini": app_config.gemini.model_copy(
                    update={"enabled": True, "fallback_enabled": True}
                )
            }
        )
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    if target and all_channels:
        raise typer.BadParameter("Pass either TARGET or --all, not both.")
    if use_catalog:
        sync_targets = [(target or "catalog", None, "catalog")]
    elif target:
        source = source_from_input(target)
        if source is None:
            typer.echo(f"Unsupported source: {target}", err=True)
            raise typer.Exit(code=1)
        bootstrap_catalog(paths.catalog_db)
        with connect_catalog(paths.catalog_db) as connection:
            upsert_library_source(connection, source, selected=True)
            connection.commit()
        sync_targets = [
            (
                source.source_url,
                source.title or source.handle or source.video_id or source.channel_id,
                source.source_type,
            )
        ]
    else:
        bootstrap_catalog(paths.catalog_db)
        with connect_catalog(paths.catalog_db) as connection:
            selected_sources = list_library_sources(connection, selected_only=True)
        if not selected_sources:
            typer.echo("No selected sources. Add one with `yutome add URL` or import subscriptions.", err=True)
            raise typer.Exit(code=1)
        sync_targets = [
            (
                source.source_url,
                source.title or source.handle or source.video_id or source.channel_id,
                source.source_type,
            )
            for source in selected_sources
        ]

    effective_workers = workers if workers is not None else app_config.backfill.workers
    effective_max_process = max_process if max_process is not None else app_config.backfill.max_videos_per_run
    _run_sync_targets(
        app_config=app_config,
        paths=paths,
        sync_targets=sync_targets,
        use_catalog=use_catalog,
        limit=limit,
        effective_embed=effective_embed,
        force=force,
        effective_max_process=effective_max_process,
        retry_failed=retry_failed,
        stop_on_rate_limit=stop_on_rate_limit,
        verbose_skips=verbose_skips,
        effective_workers=effective_workers,
        asr_fallback=asr_fallback,
        gemini_fallback=gemini_fallback,
        sleep_seconds=sleep_seconds,
        status_filter=status_filter,
        source_filter=source_filter,
        max_duration_seconds=max_duration_seconds,
        shortest_first=shortest_first,
    )


@app.command("find")
def find_command(
    text: str = typer.Argument(..., help="Search text."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    in_: str = typer.Option("chunks", "--in", help="Search corpus: chunks, titles, or descriptions."),
    mode: str | None = typer.Option(None, "--mode", help="Search mode: lexical, semantic, hybrid, or none."),
    channel: str | None = typer.Option(None, "--channel", help="Filter by channel id or handle."),
    since: str | None = typer.Option(None, "--since", help="Filter videos published on/after this date string."),
    until: str | None = typer.Option(None, "--until", help="Filter videos published on/before this date string."),
    source: str | None = typer.Option(None, "--source", help="Filter active transcript source prefix."),
    language: str | None = typer.Option(None, "--language", help="Filter active transcript language."),
    group_by: str | None = typer.Option(None, "--group-by", help="Group ranked chunk hits by video."),
    limit: int = typer.Option(10, "--limit", min=1, max=200, help="Maximum rows to return."),
    offset: int = typer.Option(0, "--offset", min=0, help="Rows to skip."),
    project: str | None = typer.Option(None, "--project", help="Projection name."),
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Pass the search text through to SQLite FTS5 verbatim. "
        "Lets you use FTS5 operators (AND/OR/NOT, prefix `*`, column filters, "
        "negation with `-`). Off by default: text is treated as a literal phrase.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the full QueryResult envelope."),
) -> None:
    """Rank transcript chunks or video metadata by relevance."""
    app_config, paths = _load_runtime(config)
    try:
        result = api_find(
            config=app_config,
            paths=paths,
            text=text,
            in_=in_,  # type: ignore[arg-type]
            mode=mode,  # type: ignore[arg-type]
            channel=channel,
            since=since,
            until=until,
            source=source,
            language=language,
            group_by=group_by,  # type: ignore[arg-type]
            limit=limit,
            offset=offset,
            project=project,
            raw=raw,
        )
    except (RuntimeError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _echo_query_result(result, json_output=json_output)


@list_app.command("videos")
def list_videos(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    channel: str | None = typer.Option(None, "--channel", help="Filter by channel id or handle."),
    since: str | None = typer.Option(None, "--since", help="Filter videos published on/after this date string."),
    until: str | None = typer.Option(None, "--until", help="Filter videos published on/before this date string."),
    status: str | None = typer.Option(None, "--status", help="Filter ingest status. Suffix with * for prefix match."),
    source: str | None = typer.Option(None, "--source", help="Filter active transcript source prefix."),
    language: str | None = typer.Option(None, "--language", help="Filter active transcript language."),
    selected: bool | None = typer.Option(None, "--selected/--any-selection", help="Only selected library channels."),
    order_by: str | None = typer.Option(None, "--order-by", help="Sort field, optionally field:asc."),
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Maximum rows to return."),
    offset: int = typer.Option(0, "--offset", min=0, help="Rows to skip."),
    project: str | None = typer.Option(None, "--project", help="Projection name."),
    json_output: bool = typer.Option(False, "--json", help="Emit the full QueryResult envelope."),
) -> None:
    """Enumerate indexed videos."""
    app_config, paths = _load_runtime(config)
    result = api_list(
        config=app_config,
        paths=paths,
        entity="videos",
        channel=channel,
        since=since,
        until=until,
        status=status,
        source=source,
        language=language,
        selected=selected,
        order_by=order_by,
        limit=limit,
        offset=offset,
        project=project,
    )
    _echo_query_result(result, json_output=json_output)


@list_app.command("channels")
def list_channels(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    channel: str | None = typer.Option(None, "--channel", help="Filter by channel id or handle."),
    selected: bool | None = typer.Option(None, "--selected/--any-selection", help="Only selected library channels."),
    limit: int = typer.Option(50, "--limit", min=1, max=200, help="Maximum rows to return."),
    offset: int = typer.Option(0, "--offset", min=0, help="Rows to skip."),
    json_output: bool = typer.Option(False, "--json", help="Emit the full QueryResult envelope."),
) -> None:
    """Enumerate local library channels."""
    app_config, paths = _load_runtime(config)
    result = api_list(
        config=app_config,
        paths=paths,
        entity="channels",
        channel=channel,
        selected=selected,
        limit=limit,
        offset=offset,
    )
    _echo_query_result(result, json_output=json_output)


@list_app.command("attention")
def list_attention(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    channel: str | None = typer.Option(None, "--channel", help="Filter by channel id or handle."),
    status: str | None = typer.Option(None, "--status", help="Filter ingest status. Suffix with * for prefix match."),
    source: str | None = typer.Option(None, "--source", help="Filter active transcript source prefix."),
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Maximum rows to return."),
    offset: int = typer.Option(0, "--offset", min=0, help="Rows to skip."),
    json_output: bool = typer.Option(False, "--json", help="Emit the full QueryResult envelope."),
) -> None:
    """List failed or deferred videos with their latest transcript attempt."""
    app_config, paths = _load_runtime(config)
    result = api_list(
        config=app_config,
        paths=paths,
        entity="attention",
        channel=channel,
        status=status,
        source=source,
        limit=limit,
        offset=offset,
    )
    _echo_query_result(result, json_output=json_output)


@list_app.command("status")
def list_status(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the full QueryResult envelope."),
) -> None:
    """Show corpus status and backlog breakdowns."""
    app_config, paths = _load_runtime(config)
    result = api_list(config=app_config, paths=paths, entity="status")
    _echo_query_result(result, json_output=json_output)


@show_app.command("chunk")
def show_chunk(
    chunk_id: str = typer.Argument(..., help="Chunk id."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
) -> None:
    """Fetch one chunk by id."""
    app_config, paths = _load_runtime(config)
    try:
        _echo_json(api_show(config=app_config, paths=paths, kind="chunk", id_=chunk_id))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@show_app.command("video")
def show_video(
    video_id: str = typer.Argument(..., help="Video id."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
) -> None:
    """Fetch one video by id."""
    app_config, paths = _load_runtime(config)
    try:
        _echo_json(api_show(config=app_config, paths=paths, kind="video", id_=video_id))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@show_app.command("channel")
def show_channel(
    selector: str = typer.Argument(..., help="Channel id or handle."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
) -> None:
    """Fetch one channel by id or handle."""
    app_config, paths = _load_runtime(config)
    try:
        _echo_json(api_show(config=app_config, paths=paths, kind="channel", id_=selector))
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@show_app.command("transcript")
def show_transcript(
    transcript_id_or_video_id: str = typer.Argument(..., help="Transcript version id or video id."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    offset: int = typer.Option(0, "--offset", min=0, help="Segment offset for long transcript paging."),
    limit: int | None = typer.Option(None, "--limit", min=1, max=5000, help="Maximum transcript segments to return."),
) -> None:
    """Fetch one transcript by transcript id or active video id."""
    app_config, paths = _load_runtime(config)
    try:
        _echo_json(
            api_show(
                config=app_config,
                paths=paths,
                kind="transcript",
                id_=transcript_id_or_video_id,
                transcript_offset=offset,
                transcript_limit=limit,
            )
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@show_app.command("context")
def show_context(
    anchor: str | None = typer.Argument(None, help="Chunk id or timestamped YouTube URL."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    id_: str | None = typer.Option(None, "--id", help="Chunk id; equivalent to positional ANCHOR."),
    video_id: str | None = typer.Option(None, "--video-id", help="Video id for timestamp lookup."),
    time_seconds: int | None = typer.Option(None, "--time", min=0, help="Timestamp in seconds for video lookup."),
    youtube_url: str | None = typer.Option(None, "--youtube-url", help="Timestamped YouTube URL."),
    token_budget: int = typer.Option(3000, "--token-budget", min=200, max=8000, help="Context token budget."),
) -> None:
    """Expand neighboring transcript text around a citation anchor."""
    if anchor and id_ and anchor != id_:
        raise typer.BadParameter("Pass either positional ANCHOR or --id, not both.")
    anchor_id = anchor or id_
    app_config, paths = _load_runtime(config)
    try:
        _echo_json(
            api_show(
                config=app_config,
                paths=paths,
                kind="context",
                id_=anchor_id,
                video_id=video_id,
                time_seconds=time_seconds,
                youtube_url=youtube_url,
                token_budget=token_budget,
            )
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@show_app.command("source")
def show_source(
    anchor: str | None = typer.Argument(None, help="Chunk id or timestamped YouTube URL."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    id_: str | None = typer.Option(None, "--id", help="Chunk id; equivalent to positional ANCHOR."),
    video_id: str | None = typer.Option(None, "--video-id", help="Video id for timestamp lookup."),
    time_seconds: int | None = typer.Option(None, "--time", min=0, help="Timestamp in seconds for video lookup."),
    youtube_url: str | None = typer.Option(None, "--youtube-url", help="Timestamped YouTube URL."),
) -> None:
    """Resolve a citation anchor to the canonical source URL and provenance."""
    if anchor and id_ and anchor != id_:
        raise typer.BadParameter("Pass either positional ANCHOR or --id, not both.")
    anchor_id = anchor or id_
    app_config, paths = _load_runtime(config)
    try:
        _echo_json(
            api_show(
                config=app_config,
                paths=paths,
                kind="source",
                id_=anchor_id,
                video_id=video_id,
                time_seconds=time_seconds,
                youtube_url=youtube_url,
            )
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@app.command("q")
def q_command(
    request: str | None = typer.Argument(None, help="JSON QueryRequest, or '-' to read from stdin."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    file: Path | None = typer.Option(None, "--file", "-f", exists=True, readable=True, help="Read QueryRequest JSON."),
) -> None:
    """Execute a raw QueryRequest JSON object."""
    app_config, paths = _load_runtime(config)
    payload = _read_query_request(request, file)
    try:
        result = api_q(config=app_config, paths=paths, request=QueryRequest.model_validate(payload))
    except (RuntimeError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    _echo_json(result.model_dump())


@eval_app.command("run")
def eval_run(
    suite: Path = typer.Argument(..., exists=True, readable=True, help="JSON eval suite file."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit full machine-readable eval results."),
) -> None:
    """Run local retrieval evals against the current corpus."""
    app_config, paths = _load_runtime(config)
    try:
        result = run_eval_suite(config=app_config, paths=paths, suite=load_eval_suite(suite))
    except (RuntimeError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    if json_output:
        _echo_json(result)
    else:
        typer.echo(f"Eval cases: {result['total']}")
        typer.echo(f"Passed: {result['passed']}")
        typer.echo(f"Failed: {result['failed']}")
        for case in result["cases"]:
            marker = "PASS" if case["passed"] else "FAIL"
            typer.echo(f"[{marker}] {case['name']} - returned {case['returned']} row(s)")
            if not case["passed"]:
                if case["missing_video_ids"]:
                    typer.echo(f"  missing videos: {', '.join(case['missing_video_ids'])}")
                if case["missing_chunk_ids"]:
                    typer.echo(f"  missing chunks: {', '.join(case['missing_chunk_ids'])}")
                if case["missing_terms"]:
                    typer.echo(f"  missing terms: {', '.join(case['missing_terms'])}")
    if result["failed"]:
        raise typer.Exit(code=1)


@app.command("rebuild-vectors")
def rebuild_vectors(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        help="Embed only pending chunks without dropping the existing LanceDB table.",
    ),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Maximum pending chunks to embed."),
    batch_size: int | None = typer.Option(None, "--batch-size", min=1, help="Embedding batch size override."),
    concurrency: int | None = typer.Option(None, "--concurrency", min=1, help="Embedding concurrency override."),
) -> None:
    """Rebuild the LanceDB vector table from canonical SQLite chunks."""
    load_dotenv(_project_root(config) / ".env")
    app_config = apply_env_to_config(load_config(config))
    app_config = app_config.model_copy(
        update={"embeddings": app_config.embeddings.model_copy(update={"enabled": True})}
    )
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    from yutome.db import connect_catalog

    with connect_catalog(paths.catalog_db) as connection:
        if resume:
            stats = embed_pending_chunks(
                connection=connection,
                config=app_config,
                lancedb_dir=paths.lancedb_dir,
                limit=limit,
                batch_size=batch_size,
                concurrency=concurrency,
            )
        else:
            stats = rebuild_lancedb_chunks(connection=connection, config=app_config, lancedb_dir=paths.lancedb_dir)
    label = "Embedded pending vectors" if resume else "Rebuilt vectors"
    typer.echo(f"{label}: {stats.embedded_chunks}")
    if stats.message:
        typer.echo(stats.message)


@app.command("rebuild-chunks")
def rebuild_chunks(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
) -> None:
    """Rebuild SQLite chunks and chunk artifacts from active normalized transcripts."""
    load_dotenv(_project_root(config) / ".env")
    app_config = apply_env_to_config(load_config(config))
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    stats = rebuild_active_chunks(paths=paths)
    typer.echo(f"Rebuilt videos: {stats.rebuilt_videos}")
    typer.echo(f"Rebuilt chunks: {stats.rebuilt_chunks}")
    typer.echo(f"Skipped videos: {stats.skipped}")


@export_app.command("portable-md")
def export_portable_markdown(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
) -> None:
    """Export indexed videos to portable Markdown."""
    load_dotenv(_project_root(config) / ".env")
    app_config = apply_env_to_config(load_config(config))
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    stats = export_markdown(paths=paths, mode="portable-md")
    typer.echo(f"Exported {stats.exported} Markdown files to {stats.output_dir}")


@export_app.command("obsidian")
def export_obsidian(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
) -> None:
    """Export indexed videos to Obsidian-friendly Markdown."""
    load_dotenv(_project_root(config) / ".env")
    app_config = apply_env_to_config(load_config(config))
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    stats = export_markdown(paths=paths, mode="obsidian")
    typer.echo(f"Exported {stats.exported} Obsidian Markdown files to {stats.output_dir}")


@app.command("proxy-info")
def proxy_info() -> None:
    """Show practical proxy guidance for transcript fetching."""
    typer.echo("Default: use no proxy, local residential IP, low concurrency, and cached resumes.")
    typer.echo("Do not use free proxy lists for real runs; they are unstable, abused, and unsafe.")
    typer.echo("First paid option: Webshare rotating residential, because youtube-transcript-api supports it directly.")
    typer.echo("Generic proxy pools can be set with YUTOME_PROXY_URLS in .env.")
    typer.echo("Single generic proxies can be set with YUTOME_HTTP_PROXY / YUTOME_HTTPS_PROXY in .env.")
    typer.echo("Webshare can be set with YUTOME_WEBSHARE_USERNAME / YUTOME_WEBSHARE_PASSWORD in .env.")
    typer.echo("yt-dlp fallback receives configured proxies through --proxy.")


@app.command("proxy-test")
def proxy_test(
    video_id: str = typer.Option(
        "lwH29W1M57A",
        "--video-id",
        help="Video ID to test against.",
    ),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    transcript_api: bool = typer.Option(
        True,
        "--transcript-api/--no-transcript-api",
        help="Test youtube-transcript-api through the configured proxy.",
    ),
    ytdlp_subtitles: bool = typer.Option(
        True,
        "--yt-dlp/--no-yt-dlp",
        help="Test yt-dlp json3 subtitle fetching through the configured proxy.",
    ),
) -> None:
    """Test the configured proxy against transcript fetch paths."""
    load_dotenv(_project_root(config) / ".env")
    app_config = apply_env_to_config(load_config(config))
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    typer.echo(f"Proxy mode: {describe_proxy(app_config.proxy)}")
    typer.echo(f"yt-dlp proxy: {redact_proxy_url(proxy_url_for_ytdlp(app_config.proxy, key=video_id))}")

    failures = 0
    if transcript_api:
        try:
            result = fetch_transcript(
                video_id=video_id,
                languages=app_config.transcripts.preferred_languages,
                proxy=app_config.proxy,
                timeout_seconds=app_config.transcripts.request_timeout_seconds,
            )
            _status(True, "youtube-transcript-api", f"{len(result.raw_snippets)} segments from {result.source}")
        except Exception as exc:  # noqa: BLE001 - diagnostics command.
            failures += 1
            _status(
                False,
                "youtube-transcript-api",
                _proxy_diagnostic_detail(app_config, exc, video_id=video_id),
            )

    if ytdlp_subtitles:
        try:
            result = fetch_subtitle_transcript_with_ytdlp(
                video_id=video_id,
                cwd=paths.root,
                language=app_config.transcripts.preferred_languages[0],
                proxy=app_config.proxy,
                ytdlp_config=app_config.yt_dlp,
                allow_translated_captions=app_config.transcripts.allow_translated_captions,
            )
            _status(True, "yt-dlp subtitles", f"{len(result.raw_snippets)} segments from {result.source}")
        except Exception as exc:  # noqa: BLE001 - diagnostics command.
            failures += 1
            _status(
                False,
                "yt-dlp subtitles",
                _proxy_diagnostic_detail(app_config, exc, video_id=video_id),
            )

    if failures:
        raise typer.Exit(code=1)


@app.command("gemini-test")
def gemini_test(
    video_id: str = typer.Option(
        "lwH29W1M57A",
        "--video-id",
        help="Video ID to test against.",
    ),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
) -> None:
    """Test Gemini YouTube URL transcript fallback on a single video."""
    load_dotenv(_project_root(config) / ".env")
    app_config = apply_env_to_config(load_config(config))
    result = transcribe_youtube_url_with_gemini(video_id=video_id, config=app_config.gemini)
    _status(True, "Gemini video understanding", f"{len(result.raw_snippets)} segments from {result.source}")


@mcp_app.command("serve")
def mcp_serve(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
) -> None:
    """Run the local MCP server over stdio for agent clients (Claude Desktop, Claude Code, etc.)."""
    from yutome.mcp_server import run_stdio_server

    run_stdio_server(config_path=config)


@http_app.command("serve")
def http_serve(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind address. Stays on loopback by default; only change after thinking about auth.",
    ),
    port: int = typer.Option(
        8765,
        "--port",
        help="Bind port.",
    ),
    cors_origin: list[str] | None = typer.Option(
        None,
        "--cors-origin",
        help="Allowed browser origin. Can be passed multiple times. Prefer exact HTTPS origins.",
    ),
    allow_unauthenticated_remote: bool = typer.Option(
        False,
        "--allow-unauthenticated-remote",
        help="Permit non-loopback HTTP binding without YUTOME_HTTP_TOKEN. Not recommended.",
    ),
) -> None:
    """Run the local HTTP API for scripts and non-MCP clients.

    Set YUTOME_HTTP_TOKEN in the environment to require a bearer token on every
    request. Unset, the server is open on the bound interface (which is loopback
    by default).
    """
    from yutome.http_server import run_http_server

    try:
        run_http_server(
            config_path=config,
            host=host,
            port=port,
            require_token_for_non_loopback=not allow_unauthenticated_remote,
            cors_origins=cors_origin,
        )
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@remote_app.command("prepare")
def remote_prepare(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    rotate: bool = typer.Option(False, "--rotate", help="Replace an existing YUTOME_HTTP_TOKEN."),
    show_token: bool = typer.Option(False, "--show-token", help="Print the token once after writing it."),
) -> None:
    """Prepare authenticated remote/API access."""
    config_written = write_default_config(config)
    project_root = _project_root(config)
    env_path = project_root / ".env"
    env_written = _write_env_template(env_path)
    paths = _load_paths(config)
    paths.ensure_base_dirs()
    bootstrap_catalog(paths.catalog_db)

    existing = _read_env_values(env_path).get("YUTOME_HTTP_TOKEN")
    token = existing if existing and not rotate else _generate_http_token()
    _merge_env_values(env_path, {"YUTOME_HTTP_TOKEN": token}, overwrite=rotate)

    typer.echo(f"[OK] {'Wrote' if config_written else 'Using existing'} config: {config}")
    typer.echo(f"[OK] {'Wrote' if env_written else 'Using existing'} local secrets file: {env_path}")
    typer.echo(f"[OK] Initialized catalog: {paths.catalog_db}")
    if existing and not rotate:
        typer.echo("[OK] Remote API token already configured in .env")
    else:
        typer.echo("[OK] Remote API token generated and saved to .env")
    if show_token:
        typer.echo(f"YUTOME_HTTP_TOKEN={token}")
    else:
        typer.echo("Token not printed. Re-run with --show-token if you need to copy it to a client.")
    typer.echo("")
    typer.echo("Serve locally for a reverse proxy:")
    typer.echo("  yutome remote serve --host 127.0.0.1 --port 8765")
    typer.echo("Serve on a private network/VPN interface:")
    typer.echo("  yutome remote serve --host 0.0.0.0 --port 8765")
    typer.echo("Serve remote MCP for agent clients:")
    typer.echo("  yutome remote mcp --host 0.0.0.0 --port 8766")


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
        "bridge",
        "start",
        "--config",
        str(config_path),
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
    typer.echo("Survives this terminal session but not reboots. For persistence: yutome bridge install")


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


@bridge_app.command("start")
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
                typer.echo("     Remove auto-start: yutome bridge uninstall")
                return
            typer.echo("Bridge auto-start is installed but the service isn't running. Starting via launchd…")
            result = _start_launchd_bridge_service()
            new_pid = _launchd_bridge_pid()
            if new_pid:
                typer.secho(f"[OK] Bridge running under launchd (PID {new_pid}).", fg="green")
            else:
                detail = (result.stderr or result.stdout or "").strip()
                typer.echo(
                    "[WARN] launchctl returned but no PID was reported. Check `yutome bridge status`."
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
                typer.echo("     Remove auto-start: yutome bridge uninstall")
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
                typer.echo("[WARN] systemctl start returned but no MainPID was reported. Check `yutome bridge status`.")
            return
    _, paths = _load_runtime(config)
    pid, log_path = _bridge_start_detached(config, paths)
    typer.echo(f"[OK] Bridge started (PID {pid}).")
    typer.echo(f"     Logs: {log_path}")
    typer.echo("     Stop with: yutome bridge stop")
    typer.echo("     Want it to survive reboots? Run: yutome bridge install")


@bridge_app.command("stop")
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
                typer.echo("[OK] Stopped launchd bridge service. Start it again with: yutome bridge start")
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
                typer.echo("[OK] Stopped systemd bridge service. Start it again with: yutome bridge start")
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


@bridge_app.command("status")
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
        typer.echo("Bridge auto-start: not installed (run `yutome bridge install` to persist across reboots)")
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
                "Stop it with `yutome bridge stop` to avoid two bridges fighting for the worker.",
                fg="yellow",
            )
        return
    if _launchd_installed() or _systemd_installed():
        typer.echo("Bridge process: auto-start is configured but the service isn't running.")
        typer.echo("  Start it with: yutome bridge start")
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
        typer.echo("  Run `yutome bridge start` to restart it.")


def _install_bridge_service(config_path: Path) -> tuple[bool, Path | None, str | None]:
    """Install the bridge as a launchd / systemd user service.

    Returns ``(installed, service_path, error_message)``. Used by both
    ``yutome bridge install`` and the post-deploy setup step that offers
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
            f"`yutome bridge install` is not supported on platform {sys.platform!r} yet. "
            "Use `yutome bridge start` to run the bridge manually.",
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
        f"`yutome bridge install` is not supported on platform {sys.platform!r}.",
    )


def _bridge_persistence_supported() -> bool:
    return sys.platform == "darwin" or sys.platform.startswith("linux")


def _bridge_install_command(config_path: Path) -> str:
    return f"yutome bridge install --config {shlex.quote(str(config_path.resolve()))}"


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
        typer.echo("     Uninstall any time with: yutome bridge uninstall")
    else:
        typer.secho(
            f"[WARN] Bridge auto-start didn't install: {error_message or 'unknown error'}",
            fg="yellow",
        )
        typer.echo("       You can retry later with: yutome bridge install")


@bridge_app.command("install")
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
    typer.echo("     Uninstall with: yutome bridge uninstall")


@bridge_app.command("uninstall")
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


@remote_app.command("sync")
def remote_sync(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview the replica sync manifest without uploading."),
    json_output: bool = typer.Option(False, "--json", help="Emit the dry-run manifest as JSON."),
) -> None:
    """Preview or run read-only replica sync."""
    if not dry_run:
        typer.echo("Remote replica upload is not implemented in this slice. Re-run with --dry-run.", err=True)
        raise typer.Exit(code=1)
    _, paths = _load_runtime(config)
    manifest = build_sync_dry_run_manifest(paths)
    if json_output:
        _echo_json(manifest)
        return
    typer.echo("Remote replica sync dry run")
    typer.echo("No upload performed.")
    typer.echo("Would sync:")
    for key, value in manifest["would_sync"].items():
        typer.echo(f"  {key}: {value}")
    typer.echo("Excluded from sync:")
    for label in manifest["excluded_secret_classes"]:
        typer.echo(f"  - {label}")


@remote_app.command("serve")
def remote_serve(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address for authenticated remote access."),
    port: int = typer.Option(8765, "--port", help="Bind port."),
    cors_origin: list[str] | None = typer.Option(
        None,
        "--cors-origin",
        help="Allowed browser origin. Can be passed multiple times. Prefer exact HTTPS origins.",
    ),
) -> None:
    """Run the authenticated HTTP API for remote clients."""
    from yutome.http_server import run_http_server

    try:
        run_http_server(
            config_path=config,
            host=host,
            port=port,
            require_token_for_non_loopback=True,
            cors_origins=cors_origin,
        )
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@remote_app.command("mcp")
def remote_mcp(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address for authenticated remote MCP."),
    port: int = typer.Option(8766, "--port", help="Bind port."),
    path: str = typer.Option("/mcp", "--path", help="MCP streamable HTTP path."),
    server_url: str | None = typer.Option(
        None,
        "--server-url",
        help="External base URL for MCP auth metadata, e.g. https://yutome.example.com.",
    ),
) -> None:
    """Run the authenticated MCP server over streamable HTTP."""
    from yutome.mcp_server import run_streamable_http_server

    try:
        run_streamable_http_server(
            config_path=config,
            host=host,
            port=port,
            path=path,
            require_token_for_non_loopback=True,
            server_url=server_url,
        )
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@remote_app.command("check")
def remote_check(
    base_url: str = typer.Argument(..., help="Base URL, e.g. https://yutome.example.com or http://127.0.0.1:8765."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config used to read .env token fallback.",
    ),
    token: str | None = typer.Option(None, "--token", help="Bearer token. Defaults to YUTOME_HTTP_TOKEN from env/.env."),
    timeout: float = typer.Option(10.0, "--timeout", min=1.0, help="Request timeout in seconds."),
) -> None:
    """Check a remote yutome HTTP API from this machine."""
    env_path = _project_root(config) / ".env"
    load_dotenv(env_path)
    effective_token = token or _env_value(env_path, "YUTOME_HTTP_TOKEN")
    base = base_url.rstrip("/")

    try:
        health = _http_json("GET", f"{base}/healthz", timeout=timeout)
        typer.echo(f"[OK] healthz: auth_required={health.get('auth_required')} cors_enabled={health.get('cors_enabled')}")
        ready = _http_json("GET", f"{base}/readyz", timeout=timeout, headers=_header_token(effective_token))
        typer.echo(
            "[OK] readyz: "
            f"videos={ready.get('videos')} chunks={ready.get('chunks')} "
            f"searchable_now={ready.get('searchable_now')} needs_attention={ready.get('needs_attention')}"
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        typer.echo(f"remote check failed: HTTP {exc.code} {detail}", err=True)
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # noqa: BLE001 - command should present connection failures cleanly.
        typer.echo(f"remote check failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _http_json(method: str, url: str, *, timeout: float, headers: dict[str, str] | None = None) -> dict[str, object]:
    request = urllib.request.Request(url, method=method, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


@contract_app.command("emit")
def contract_emit(
    output: Path = typer.Option(
        Path("cloudflare/yutome-capsule/src/contract.json"),
        "--output",
        "-o",
        help="Destination JSON path. Defaults to the TS Worker's expected location.",
    ),
    show: bool = typer.Option(False, "--show", help="Print the emitted JSON to stdout."),
) -> None:
    """Serialize the contract registry to JSON for the TypeScript Worker."""
    from yutome.contract_export import emit_contract_json

    path = emit_contract_json(output)
    typer.echo(f"[OK] Wrote contract JSON: {path}")
    if show:
        typer.echo(path.read_text(encoding="utf-8"))
