from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import Counter
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
from yutome.indexer import sync_channel, sync_video
from yutome.paths import ProjectPaths
from yutome.maintenance import rebuild_active_chunks
from yutome.quality_upgrade import upgrade_active_transcripts
from yutome.query import QueryRequest
from yutome import setup_prompts
from yutome.remote_connection import (
    RemoteMode,
    build_remote_state,
    build_sync_dry_run_manifest,
    load_remote_state,
    mark_desktop_seen,
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
remote_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Prepare and serve authenticated remote access.")
contract_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Inspect and export the MCP contract.")
app.add_typer(export_app, name="export")
app.add_typer(list_app, name="list")
app.add_typer(show_app, name="show")
app.add_typer(quality_app, name="quality")
app.add_typer(mcp_app, name="mcp")
app.add_typer(http_app, name="http")
app.add_typer(eval_app, name="eval")
app.add_typer(remote_app, name="remote")
app.add_typer(contract_app, name="contract")


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


def _echo_json(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


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
    marker = "OK" if ok else "WARN"
    suffix = f" - {detail}" if detail else ""
    typer.echo(f"[{marker}] {label}{suffix}")


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

    typer.echo("")
    typer.echo(
        "Semantic/hybrid search lets yutome find paraphrases and concepts, not just exact "
        "words. It uses Voyage embeddings during sync. If you skip, lexical (keyword) "
        "search still works fully — you can enable semantic later with `yutome setup`."
    )
    typer.echo("")
    typer.echo("  Sign up:  https://www.voyageai.com/")
    typer.echo("  Cost:     free tier covers small/medium libraries; pay-as-you-go past that")
    typer.echo("  Docs:     https://docs.voyageai.com/docs/embeddings")
    typer.echo("")
    if not deps_ready:
        _status(False, "Semantic search dependencies", "run `uv sync` or reinstall yutome")
    if not setup_prompts.confirm("Enable semantic/hybrid search now?", default=False):
        _status(False, "Semantic/hybrid search", "skipped; lexical search still works")
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
    typer.echo("")
    typer.echo(
        "Webshare is a paid residential-proxy service that helps large YouTube imports "
        "avoid local IP blocks. Skip it for small tests; configure it before importing "
        "hundreds of videos. If you skip, yutome will ask again the first time YouTube "
        "blocks a transcript fetch."
    )
    typer.echo("")
    typer.echo("  Sign up:  https://www.webshare.io/residential-proxy")
    typer.echo("  Cost:     ~$3.50/month for ~1 GB residential traffic (usually plenty)")
    typer.echo("  Plan:     'Residential Proxy' (rotating)")
    typer.echo("            NOT the cheaper 'Proxy Server' (datacenter) — YouTube blocks those")
    typer.echo("  Why:      https://github.com/maskys/yutome/blob/main/docs/proxy-strategy.md")
    typer.echo("")
    if not setup_prompts.confirm("Configure Webshare residential proxy now?", default=False):
        _status(False, "Webshare residential proxy", "skipped; yutome can ask again if YouTube blocks transcript fetching")
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

    typer.echo("")
    typer.echo(
        "Gemini does two jobs for yutome: it repairs noisy auto-captions into clean "
        "readable transcripts after each sync, and it transcribes videos directly when "
        "captions and ASR fail. If you skip, yutome still indexes raw auto-captions and "
        "you can enable Gemini later with `yutome setup`."
    )
    typer.echo("")
    typer.echo("  Sign up:  https://aistudio.google.com/apikey  (Google account required)")
    typer.echo("  Cost:     free tier covers casual use; pay-as-you-go past the daily quota")
    typer.echo("  Docs:     https://ai.google.dev/gemini-api/docs")
    typer.echo("")
    if not deps_ready:
        _status(False, "Gemini dependency", "run `uv sync` or reinstall yutome")
    if not setup_prompts.confirm("Enable Gemini transcript repair and fallback now?", default=False):
        _status(False, "Gemini (transcript repair + fallback)", "skipped; yutome can ask again before transcript repair/fallback")
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
        typer.echo("Type to filter; use space to select; enter to continue.")
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
        selected_labels = setup_prompts.checkbox(
            title,
            choices=choices,
            defaults=default_labels,
            instruction=(
                "Use arrows to move, space to select, type to search, enter to continue. "
                "Choose 'All channels' to select everything."
            ),
            use_search_filter=True,
            erase_when_done=True,
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


def _prompt_oauth_client_secrets(env_path: Path) -> bool:
    """Walk the user through creating a Google OAuth Desktop client and storing its JSON path.

    Returns True when the YUTOME_YOUTUBE_OAUTH_CLIENT_SECRETS env value is set.
    """
    typer.echo("")
    typer.echo(
        "You picked OAuth, so we need to register yutome as a Desktop OAuth client "
        "inside your Google Cloud project. This is a one-time thing per Google account "
        "— yutome reuses the client every time. Expect 5–10 minutes the first time you "
        "use Google Cloud Console; faster if you've done it before."
    )
    typer.echo("")
    typer.echo("Friendlier walk-through with screenshots:")
    typer.echo("  https://github.com/MaskyS/yutome/blob/main/docs/oauth-testing.md")
    typer.echo("")
    typer.echo("The six steps (open each URL in your browser):")
    typer.echo("  1. Console:        https://console.cloud.google.com/")
    typer.echo("     Create a new project or pick an existing one.")
    typer.echo("  2. Enable the API: https://console.cloud.google.com/apis/library/youtube.googleapis.com")
    typer.echo("     Click ENABLE on the YouTube Data API v3 page.")
    typer.echo("  3. Consent screen: https://console.cloud.google.com/apis/credentials/consent")
    typer.echo("     - User type: External")
    typer.echo("     - Add the scope: .../auth/youtube.readonly")
    typer.echo("     - Add your own Gmail under 'Test users' (required while the app is in Testing).")
    typer.echo("  4. Credentials:    https://console.cloud.google.com/apis/credentials")
    typer.echo("     Create Credentials -> OAuth client ID -> Application type: Desktop app.")
    typer.echo("  5. Click DOWNLOAD JSON on the new client; save it somewhere private (e.g. ~/.yutome/).")
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
    if setup_prompts.is_interactive():
        add_choice = "Yes - add another channel's public subscriptions"
        skip_choice = "No - continue with this subscription list"
        back_choice = "Back - choose a different subscription import method"
        choice = setup_prompts.select(
            "Import the public subscriptions of another channel (someone else's library)?",
            choices=[skip_choice, add_choice, back_choice],
            default=skip_choice,
        )
        if choice == back_choice:
            return BACK_CHOICE
        if choice == skip_choice:
            return None
        raw = setup_prompts.text("Public channel URL, handle, or channel id (blank to skip, b to go back)")
    else:
        if not setup_prompts.confirm(
            "Import the public subscriptions of another channel (someone else's library)?",
            default=False,
        ):
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

        public_target = _prompt_public_subscription_target()
        if public_target == BACK_CHOICE:
            continue
        if public_target:
            try:
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
    typer.echo("")
    typer.echo("How many recent videos per channel should Yutome index first?")
    typer.echo("  10    quick try")
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
        typer.echo("Start or restart the laptop bridge when you want Claude/ChatGPT to reach the local corpus:")
        typer.echo("  yutome remote bridge")
        typer.echo("  If you just reran `yutome connect --deploy`, restart any old bridge process")
        typer.echo("  because deploy refreshes the Worker's bridge token.")
        typer.echo("  (The bridge holds a long-lived WebSocket to the Worker. If you're behind a")
        typer.echo("  corporate proxy that blocks WS, requests fall back to the offline response.)")
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
    typer.echo("    - Catch: this computer has to be on (with `yutome remote bridge`")
    typer.echo("      running) for the assistant to actually get answers. When it's off,")
    typer.echo("      the assistant just says 'Yutome Desktop offline' and the rest of")
    typer.echo("      the chat keeps working.")
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


def _setup_local_mcp(config_path: Path) -> None:
    """Help a user wire yutome into Claude Desktop / Code / Cursor on this machine.

    Doesn't modify any files. Claude Desktop's config is often touched by
    hand or by other installers (Smithery, DXT/MCPB bundles, other MCP
    servers), and JSON-merging into it from a CLI is a foot-gun: comments
    don't round-trip, key ordering changes, and a botched write loses other
    server entries. We show the snippet, offer to put it on the clipboard,
    and offer to open the config file's location.
    """
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


def _run_command_streamed(command: list[str], *, cwd: Path) -> tuple[int, str]:
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
    wrangler_config = _ensure_oauth_kv_namespace(capsule, paths)

    typer.echo(f"Deploying Cloudflare Worker from {capsule}")
    command = ["npx", "--yes", "wrangler", "deploy", "--config", str(wrangler_config)]
    returncode, output = _run_command_streamed(command, cwd=capsule)
    if returncode != 0:
        typer.echo(
            "Cloudflare Worker deploy failed. Fix the Wrangler error above and rerun `yutome connect --deploy`.",
            err=True,
        )
        raise typer.Exit(code=returncode)

    # Worker is up. Push secrets so OAuth pairing + bridge auth work.
    _push_wrangler_secret(capsule, "YUTOME_RELAY_TOKEN", effective_relay_token, wrangler_config=wrangler_config)
    _push_wrangler_secret(capsule, "YUTOME_PAIRING_CODE", effective_pairing_code, wrangler_config=wrangler_config)

    deployed_url = _extract_worker_url(output)
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
) -> None:
    """Guided first-run setup for a local yutome project."""
    typer.echo("yutome guided setup")
    typer.echo("")

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
    _setup_webshare(env_path, yes=yes)
    _setup_gemini(config, env_path, yes=yes)
    semantic_enabled = _setup_semantic_search(config, env_path, yes=yes)
    _set_toml_string(config, "find", "default_mode", "hybrid" if semantic_enabled else "lexical")
    load_dotenv(env_path)
    app_config = apply_env_to_config(load_config(config))

    setup_library_channels: list[LibraryChannel] = []
    if not yes:
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
    _print_setup_mcp_section(yes=yes)
    if not yes:
        node_ready = _can_run_cloudflare_deploy()
        local_label = (
            "Local — Claude Desktop / Cursor / Cherry Studio on this Mac "
            "(recommended: free, no signup, ~30 seconds)"
        )
        deploy_label = (
            "Web + mobile — works from claude.ai, ChatGPT, your phone "
            "(Cloudflare sign-in needed; free plan is fine)"
            if node_ready
            else "Web + mobile — needs Node.js 22+ installed first; opens Cloudflare to get started"
        )
        paste_label = "Web + mobile — I already have a Yutome URL someone gave me"
        skip_label = "Skip for now — I'll run `yutome connect` later"
        choices = [local_label, deploy_label, paste_label, skip_label]
        while True:
            connect_choice = setup_prompts.select(
                "How do you want to connect Yutome to your assistant?",
                choices=choices,
                default=local_label,
            )
            if connect_choice in {paste_label, deploy_label}:
                assistant_apps = _prompt_assistant_apps(allow_back=True)
                if assistant_apps is None:
                    continue
            else:
                assistant_apps = None
            break
        if connect_choice == skip_label:
            pass
        elif connect_choice == local_label:
            _setup_local_mcp(config)
        elif connect_choice == paste_label:
            typer.echo("")
            typer.echo(
                "Paste the connector URL printed by whoever set up the Worker. The URL can be "
                "the base Worker URL or the full /mcp URL — yutome handles both."
            )
            endpoint = setup_prompts.text("Connector URL")
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
) -> None:
    """Add YouTube sources to the local library."""
    app_config = load_config(config)
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
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
) -> None:
    """Import sources from CSV, OPML/XML, or a plain list."""
    app_config = load_config(config)
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    bootstrap_catalog(paths.catalog_db)
    sources = import_sources_from_file(path, selected=selected)
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

    ws_url = _bridge_ws_url(endpoint_url)
    typer.echo(f"Yutome bridge connecting to {ws_url}")
    typer.echo("Keep this running while using Claude/ChatGPT remote MCP. Press Ctrl-C to stop.")

    backoff = 1.0
    while True:
        try:
            async with websockets.connect(
                ws_url,
                additional_headers=[("Authorization", f"Bearer {token}")],
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
        except (OSError, websockets.exceptions.WebSocketException) as exc:
            typer.echo(f"[WARN] Bridge disconnected: {exc}", err=True)
            if once:
                raise typer.Exit(code=1) from exc
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


@remote_app.command("bridge")
def remote_bridge(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    once: bool = typer.Option(False, "--once", help="Process one job and exit."),
) -> None:
    """Connect this laptop to the Cloudflare remote MCP Worker via WebSocket."""
    app_config, paths = _load_runtime(config)
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


@remote_app.command("status")
def remote_status(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit full remote status JSON."),
) -> None:
    """Show detailed remote connector state."""
    _, paths = _load_runtime(config)
    payload = remote_status_payload(paths)
    if json_output:
        _echo_json(payload)
        return
    if not payload["configured"]:
        typer.echo("Remote connector is not configured.")
        typer.echo("Run: yutome connect")
        return
    typer.echo("Remote connector:")
    typer.echo(f"  provider: {payload['provider']}")
    typer.echo(f"  mode: {payload['mode']}")
    typer.echo(f"  endpoint: {payload['endpoint_url']}")
    typer.echo(f"  mcp url: {payload['mcp_url']}")
    typer.echo(f"  assistant oauth: {payload.get('assistant_oauth_status')}")
    typer.echo(f"  desktop: {payload['desktop_connection']}")
    if payload.get("relay_status_error"):
        typer.echo(f"  live status: unavailable ({payload['relay_status_error']})")
    typer.echo(f"  bridge token: {'configured' if payload.get('relay_token_configured') else 'missing'}")
    typer.echo(f"  pairing code: {'configured' if payload.get('pairing_code_configured') else 'missing'}")
    typer.echo("  oauth storage: worker OAUTH_KV")
    typer.echo(f"  offline search: {payload['offline_search']}")
    typer.echo(f"  last sync: {payload.get('last_sync_at') or 'never'}")


@remote_app.command("disconnect")
def remote_disconnect(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    worker_name: str | None = typer.Option(
        None,
        "--worker-name",
        help="Cloudflare Worker name to remove. Defaults to the saved/generated worker name.",
    ),
    remove_cloudflare: bool = typer.Option(
        True,
        "--remove-cloudflare/--keep-cloudflare",
        help="Remove the Yutome-managed Cloudflare Worker when one is recorded.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
    keep_state: bool = typer.Option(False, "--keep-state", help="Keep local remote connector state."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be disconnected without changing anything."),
) -> None:
    """Detailed remote disconnect command."""
    _disconnect_remote(
        config=config,
        worker_name=worker_name,
        remove_cloudflare=remove_cloudflare,
        keep_state=keep_state,
        dry_run=dry_run,
        yes=yes,
    )


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
