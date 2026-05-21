from __future__ import annotations

import importlib.util
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import typer

from ytkb.config import DEFAULT_CONFIG_FILENAME, load_config, write_default_config
from ytkb.api import find as api_find
from ytkb.api import list_ as api_list
from ytkb.api import q as api_q
from ytkb.api import show as api_show
from ytkb.channels import (
    channel_from_input,
    import_channels_from_file,
    list_library_channels,
    set_library_channel_selected,
    upsert_library_channel,
)
from ytkb.db import bootstrap_catalog, catalog_is_initialized, connect_catalog, fts5_available
from ytkb.embeddings import embed_pending_chunks, rebuild_lancedb_chunks
from ytkb.env import apply_env_to_config, load_dotenv
from ytkb.exports import export_markdown
from ytkb.gemini import transcribe_youtube_url_with_gemini
from ytkb.indexer import sync_channel
from ytkb.paths import ProjectPaths
from ytkb.maintenance import rebuild_active_chunks
from ytkb.quality_upgrade import upgrade_active_transcripts
from ytkb.query import QueryRequest
from ytkb.youtube_oauth import fetch_subscription_channels, load_oauth_client, load_or_authorize_token
from ytkb.youtube import (
    describe_proxy,
    fetch_subtitle_transcript_with_ytdlp,
    fetch_transcript,
    proxy_url_for_ytdlp,
    redact_proxy_secrets,
    redact_proxy_url,
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Local-first YouTube channel knowledge base indexer.",
)
export_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Export indexed artifacts.")
channels_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Manage the local channel library.")
list_app = typer.Typer(add_completion=False, no_args_is_help=True, help="List indexed corpus objects.")
show_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Show indexed corpus objects.")
quality_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Transcript quality tools.")
mcp_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Local MCP server for agent clients.")
http_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Local HTTP API server.")
app.add_typer(export_app, name="export")
app.add_typer(channels_app, name="channels")
app.add_typer(list_app, name="list")
app.add_typer(show_app, name="show")
app.add_typer(quality_app, name="quality")
app.add_typer(mcp_app, name="mcp")
app.add_typer(http_app, name="http")


def _project_root(config_path: Path) -> Path:
    if config_path.is_absolute():
        return config_path.parent
    return (Path.cwd() / config_path).parent


def _load_paths(config_path: Path) -> ProjectPaths:
    config = load_config(config_path)
    return ProjectPaths.from_config(config, project_root=_project_root(config_path))


def _load_runtime(config_path: Path) -> tuple[object, ProjectPaths]:
    load_dotenv(_project_root(config_path) / ".env")
    app_config = apply_env_to_config(load_config(config_path))
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


@app.command()
def init(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the ytkb TOML config.",
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


@app.command()
def doctor(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the ytkb TOML config.",
    ),
) -> None:
    """Check local project readiness."""
    failures = 0

    python_ok = sys.version_info >= (3, 12)
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
        typer.echo("      install with: uv sync --extra ingest")
    _status(
        _module_available("youtube_transcript_api"),
        "youtube-transcript-api availability",
        "install with: uv sync --extra ingest",
    )
    _status(
        _module_available("lancedb"),
        "LanceDB availability",
        "install with: uv sync --extra vectors",
    )
    _status(
        _module_available("faster_whisper"),
        "faster-whisper availability",
        "install with: uv sync --extra asr",
    )
    _status(
        _module_available("voyageai"),
        "Voyage client availability",
        "install with: uv sync --extra embeddings",
    )
    _status(
        _module_available("google.genai"),
        "Gemini client availability",
        "install with: uv sync --extra gemini",
    )

    if failures:
        raise typer.Exit(code=1)


@channels_app.command("add")
def channels_add(
    targets: list[str] = typer.Argument(..., help="YouTube channel URL, handle, or channel id."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the ytkb TOML config.",
    ),
    title: str | None = typer.Option(None, "--title", help="Optional display title for one channel."),
    selected: bool = typer.Option(True, "--selected/--unselected", help="Include channel in default sync runs."),
) -> None:
    """Add channel URLs, handles, or ids to the local channel library."""
    app_config = load_config(config)
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    bootstrap_catalog(paths.catalog_db)
    imported = 0
    with connect_catalog(paths.catalog_db) as connection:
        for target in targets:
            channel = channel_from_input(target, title=title if len(targets) == 1 else None, import_source="manual")
            if channel is None:
                continue
            upsert_library_channel(connection, channel, selected=selected)
            imported += 1
        connection.commit()
    typer.echo(f"Added {imported} channel{'s' if imported != 1 else ''}.")


@channels_app.command("import")
def channels_import(
    path: Path = typer.Argument(..., exists=True, readable=True, help="CSV, OPML/XML, or plain URL list."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the ytkb TOML config.",
    ),
    selected: bool = typer.Option(True, "--selected/--unselected", help="Include imported channels in default sync runs."),
) -> None:
    """Import channels from Google Takeout CSV, OPML/XML, or a plain list."""
    app_config = load_config(config)
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    bootstrap_catalog(paths.catalog_db)
    channels = import_channels_from_file(path, selected=selected)
    with connect_catalog(paths.catalog_db) as connection:
        for channel in channels:
            upsert_library_channel(connection, channel, selected=selected)
        connection.commit()
    typer.echo(f"Imported {len(channels)} channel{'s' if len(channels) != 1 else ''}.")


@channels_app.command("import-youtube")
def channels_import_youtube(
    client_secrets: Path = typer.Option(
        ...,
        "--client-secrets",
        exists=True,
        readable=True,
        help="Google OAuth client secrets JSON for a desktop/local app.",
    ),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the ytkb TOML config.",
    ),
    token: Path | None = typer.Option(
        None,
        "--token",
        help="OAuth token cache path. Defaults to data/auth/youtube-oauth-token.json.",
    ),
    port: int = typer.Option(0, "--port", min=0, max=65535, help="Local OAuth callback port. 0 chooses a free port."),
    open_browser: bool = typer.Option(True, "--open-browser/--print-url", help="Open the OAuth URL in a browser."),
    selected: bool = typer.Option(True, "--selected/--unselected", help="Include imported channels in default sync runs."),
) -> None:
    """Import the signed-in user's YouTube subscriptions with local OAuth."""
    app_config = load_config(config)
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    bootstrap_catalog(paths.catalog_db)
    token_path = token or (paths.data_dir / "auth" / "youtube-oauth-token.json")
    client = load_oauth_client(client_secrets)
    typer.echo("Opening YouTube OAuth consent for read-only subscription access...")
    oauth_token = load_or_authorize_token(
        client=client,
        token_path=token_path,
        port=port,
        open_browser=open_browser,
    )
    channels = fetch_subscription_channels(str(oauth_token["access_token"]))
    with connect_catalog(paths.catalog_db) as connection:
        for channel in channels:
            upsert_library_channel(connection, channel, selected=selected)
        connection.commit()
    typer.echo(f"Imported {len(channels)} YouTube subscription channel{'s' if len(channels) != 1 else ''}.")


@channels_app.command("select")
def channels_select(
    selector: str = typer.Argument(..., help="Channel id, URL, handle, title, or 'all'."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the ytkb TOML config.",
    ),
) -> None:
    """Include matching channel library entries in default sync runs."""
    app_config = load_config(config)
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    bootstrap_catalog(paths.catalog_db)
    with connect_catalog(paths.catalog_db) as connection:
        count = set_library_channel_selected(connection, selector=selector, selected=True)
        connection.commit()
    typer.echo(f"Selected {count} channel{'s' if count != 1 else ''}.")


@channels_app.command("unselect")
def channels_unselect(
    selector: str = typer.Argument(..., help="Channel id, URL, handle, title, or 'all'."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the ytkb TOML config.",
    ),
) -> None:
    """Exclude matching channel library entries from default sync runs."""
    app_config = load_config(config)
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    bootstrap_catalog(paths.catalog_db)
    with connect_catalog(paths.catalog_db) as connection:
        count = set_library_channel_selected(connection, selector=selector, selected=False)
        connection.commit()
    typer.echo(f"Unselected {count} channel{'s' if count != 1 else ''}.")


@quality_app.command("upgrade")
def quality_upgrade(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the ytkb TOML config.",
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
        progress=typer.echo,
    )
    typer.echo(f"Scanned transcripts: {stats.scanned}")
    typer.echo(f"Upgraded transcripts: {stats.upgraded}")
    typer.echo(f"Skipped unchanged: {stats.skipped_unchanged}")
    typer.echo(f"Skipped missing: {stats.skipped_missing}")
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
        typer.echo("Vector index note: run `ytkb rebuild-vectors` to refresh semantic/hybrid retrieval.")


@app.command()
def sync(
    target: str | None = typer.Argument(
        None,
        help=(
            "YouTube channel URL or handle URL. Omit to sync selected channels "
            "from `ytkb list channels`."
        ),
    ),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the ytkb TOML config.",
    ),
    all_channels: bool = typer.Option(
        False,
        "--all",
        help="Sync every selected channel in the local channel library.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Limit videos discovered per tab; omit for the full channel.",
    ),
    embed: bool = typer.Option(
        False,
        "--embed/--no-embed",
        help="Generate Voyage embeddings and index them in LanceDB.",
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
        help="Maximum non-indexed videos to process in this run after discovery.",
    ),
    workers: int | None = typer.Option(
        None,
        "--workers",
        min=1,
        max=32,
        help="Number of videos to process concurrently. Defaults to backfill.workers from ytkb.toml (currently 8). Use --workers 1 for serial / safest mode on residential IP.",
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
    fetch_metadata: bool = typer.Option(
        True,
        "--fetch-metadata/--defer-metadata",
        help="Fetch full per-video metadata during transcript backfill. Use --defer-metadata for faster historical imports.",
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
    fallback_only: bool = typer.Option(
        False,
        "--fallback-only",
        help="For known fallback rows, skip caption providers and go straight to Gemini/ASR.",
    ),
    yt_dlp_first: bool = typer.Option(
        False,
        "--yt-dlp-first/--transcript-api-first",
        help="Try yt-dlp subtitle files before youtube-transcript-api. Default is transcript API first.",
    ),
    ytdlp_fallback: bool = typer.Option(
        True,
        "--yt-dlp-fallback/--no-yt-dlp-fallback",
        help="After transcript API fails, try yt-dlp subtitle files. Disable for a fastest transcript-API-only pass.",
    ),
    staged_fallback: bool = typer.Option(
        False,
        "--staged-fallback/--inline-fallback",
        help="Run transcript API across all candidates first, then retry unresolved videos with yt-dlp fallback in the same command.",
    ),
    stop_on_rate_limit: bool = typer.Option(
        True,
        "--stop-on-rate-limit/--continue-on-rate-limit",
        help="Stop the run when a likely YouTube rate limit/block is detected.",
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
    """Discover and index a YouTube channel."""
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
    if embed:
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
    if yt_dlp_first:
        if staged_fallback:
            raise typer.BadParameter("--staged-fallback is transcript-API-first; do not combine it with --yt-dlp-first.")
        app_config = app_config.model_copy(
            update={
                "transcripts": app_config.transcripts.model_copy(
                    update={"prefer_ytdlp_subtitles": True}
                )
            }
        )
    if staged_fallback and not ytdlp_fallback:
        raise typer.BadParameter("--staged-fallback needs yt-dlp fallback enabled for its retry stage.")
    paths = ProjectPaths.from_config(app_config, project_root=_project_root(config))
    if target and all_channels:
        raise typer.BadParameter("Pass either TARGET or --all, not both.")
    if use_catalog:
        sync_targets = [("catalog", None)]
    elif target:
        sync_targets = [(target, None)]
    else:
        bootstrap_catalog(paths.catalog_db)
        with connect_catalog(paths.catalog_db) as connection:
            selected_channels = list_library_channels(connection, selected_only=True)
        if not selected_channels:
            typer.echo("No selected channels. Add one with `ytkb channels add URL` or import subscriptions.", err=True)
            raise typer.Exit(code=1)
        sync_targets = [
            (channel.source_url, channel.title or channel.handle or channel.channel_id)
            for channel in selected_channels
        ]

    totals = {
        "discovered": 0,
        "processed": 0,
        "metadata_saved": 0,
        "transcripts_saved": 0,
        "chunks_saved": 0,
        "skipped_existing": 0,
        "skipped_failed": 0,
        "deferred": 0,
        "failed": 0,
        "embedded_chunks": 0,
        "elapsed_seconds": 0.0,
        "stopped_early": False,
        "embedding_messages": [],
    }
    for sync_target, label in sync_targets:
        if len(sync_targets) > 1:
            typer.echo("")
            typer.echo(f"Syncing {label or sync_target}")
        stats = sync_channel(
            target=sync_target,
            config=app_config,
            paths=paths,
            limit=limit,
            embed=embed,
            sleep_seconds=sleep_seconds,
            force=force,
            asr_fallback=asr_fallback,
            gemini_fallback=gemini_fallback,
            max_process=max_process,
            retry_failed=retry_failed,
            stop_on_rate_limit=stop_on_rate_limit,
            refresh_discovery=not use_catalog,
            verbose_skips=verbose_skips,
            workers=workers if workers is not None else app_config.backfill.workers,
            fetch_metadata=fetch_metadata,
            status_filters=status_filter,
            source_filters=source_filter,
            max_duration_seconds=max_duration_seconds,
            shortest_first=shortest_first,
            fallback_only=fallback_only,
            ytdlp_fallback=ytdlp_fallback,
            staged_fallback=staged_fallback,
            progress=typer.echo,
        )
        for field in (
            "discovered",
            "processed",
            "metadata_saved",
            "transcripts_saved",
            "chunks_saved",
            "skipped_existing",
            "skipped_failed",
            "deferred",
            "failed",
            "embedded_chunks",
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
    typer.echo(f"Transcripts saved: {totals['transcripts_saved']}")
    typer.echo(f"Chunks saved: {totals['chunks_saved']}")
    typer.echo(f"Skipped existing: {totals['skipped_existing']}")
    typer.echo(f"Skipped failed/deferred: {totals['skipped_failed']}")
    typer.echo(f"Deferred videos: {totals['deferred']}")
    typer.echo(f"Failed videos: {totals['failed']}")
    typer.echo(f"Embedded chunks: {totals['embedded_chunks']}")
    for message in totals["embedding_messages"]:
        typer.echo(f"Embedding note: {message}")
    typer.echo(f"Elapsed seconds: {totals['elapsed_seconds']:.1f}")
    throughput = 0.0
    if totals["elapsed_seconds"] > 0:
        throughput = totals["transcripts_saved"] / (totals["elapsed_seconds"] / 60)
    typer.echo(f"Transcript throughput: {throughput:.2f} videos/min")
    typer.echo(f"Stopped early: {totals['stopped_early']}")


@app.command("find")
def find_command(
    text: str = typer.Argument(..., help="Search text."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the ytkb TOML config.",
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
        help="Path to the ytkb TOML config.",
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
        help="Path to the ytkb TOML config.",
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
        help="Path to the ytkb TOML config.",
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
        help="Path to the ytkb TOML config.",
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
        help="Path to the ytkb TOML config.",
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
        help="Path to the ytkb TOML config.",
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
        help="Path to the ytkb TOML config.",
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
    transcript_version_id: str = typer.Argument(..., help="Transcript version id."),
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the ytkb TOML config.",
    ),
) -> None:
    """Fetch one transcript by id."""
    app_config, paths = _load_runtime(config)
    try:
        _echo_json(api_show(config=app_config, paths=paths, kind="transcript", id_=transcript_version_id))
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
        help="Path to the ytkb TOML config.",
    ),
    video_id: str | None = typer.Option(None, "--video-id", help="Video id for timestamp lookup."),
    time_seconds: int | None = typer.Option(None, "--time", min=0, help="Timestamp in seconds for video lookup."),
    youtube_url: str | None = typer.Option(None, "--youtube-url", help="Timestamped YouTube URL."),
    token_budget: int = typer.Option(3000, "--token-budget", min=200, max=8000, help="Context token budget."),
) -> None:
    """Expand neighboring transcript text around a citation anchor."""
    app_config, paths = _load_runtime(config)
    try:
        _echo_json(
            api_show(
                config=app_config,
                paths=paths,
                kind="context",
                id_=anchor,
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
        help="Path to the ytkb TOML config.",
    ),
    video_id: str | None = typer.Option(None, "--video-id", help="Video id for timestamp lookup."),
    time_seconds: int | None = typer.Option(None, "--time", min=0, help="Timestamp in seconds for video lookup."),
    youtube_url: str | None = typer.Option(None, "--youtube-url", help="Timestamped YouTube URL."),
) -> None:
    """Resolve a citation anchor to the canonical source URL and provenance."""
    app_config, paths = _load_runtime(config)
    try:
        _echo_json(
            api_show(
                config=app_config,
                paths=paths,
                kind="source",
                id_=anchor,
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
        help="Path to the ytkb TOML config.",
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


@app.command("rebuild-vectors")
def rebuild_vectors(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the ytkb TOML config.",
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
    from ytkb.db import connect_catalog

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
        help="Path to the ytkb TOML config.",
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
        help="Path to the ytkb TOML config.",
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
        help="Path to the ytkb TOML config.",
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
    typer.echo("Generic proxy pools can be set with YTKB_PROXY_URLS in .env.")
    typer.echo("Single generic proxies can be set with YTKB_HTTP_PROXY / YTKB_HTTPS_PROXY in .env.")
    typer.echo("Webshare can be set with YTKB_WEBSHARE_USERNAME / YTKB_WEBSHARE_PASSWORD in .env.")
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
        help="Path to the ytkb TOML config.",
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
                redact_proxy_secrets(app_config.proxy, str(exc), key=video_id)[:500],
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
                redact_proxy_secrets(app_config.proxy, str(exc), key=video_id)[:500],
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
        help="Path to the ytkb TOML config.",
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
        help="Path to the ytkb TOML config.",
    ),
) -> None:
    """Run the local MCP server over stdio for agent clients (Claude Desktop, Claude Code, etc.)."""
    from ytkb.mcp_server import run_stdio_server

    run_stdio_server(config_path=config)


@http_app.command("serve")
def http_serve(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the ytkb TOML config.",
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
) -> None:
    """Run the local HTTP API for scripts and non-MCP clients.

    Set YTKB_HTTP_TOKEN in the environment to require a bearer token on every
    request. Unset, the server is open on the bound interface (which is loopback
    by default).
    """
    from ytkb.http_server import run_http_server

    run_http_server(config_path=config, host=host, port=port)
