from __future__ import annotations

import http.server
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Mapping

import typer

from yutome import runtime, setup_prompts
from yutome.config import DEFAULT_CONFIG_FILENAME, AppConfig, load_config, write_default_config
from yutome.env import apply_env_to_config, load_dotenv
from yutome.exports import export_markdown
from yutome.hosted.account_cli import (
    DEFAULT_CLI_CLIENT_ID,
    DEFAULT_CLI_SCOPES,
    code_challenge_for_verifier,
    new_code_verifier,
)
from yutome.hosted.cli_helpers import summarize_usage_events
from yutome.hosted.ledger import PostgresUsageLedger
from yutome.hosted.runtime import (
    HostedCommandRunner,
    HostedRuntimeError,
    build_hosted_api_app,
    connect_postgres,
    postgres_url_from_env,
    redact_postgres_url,
)
from yutome.http_server import run_http_server
from yutome.maintenance import rebuild_active_chunks
from yutome.mcp_server import run_stdio_server, run_streamable_http_server
from yutome.paths import ProjectPaths
from yutome.quality_upgrade import upgrade_active_transcripts
from yutome.remote_connection import (
    build_remote_state,
    load_remote_state,
    remote_state_path,
    remote_status_payload,
    save_remote_state,
)
from yutome.sources import (
    import_sources_from_file,
    set_library_source_selected,
    source_from_input,
    upsert_library_source,
)
from yutome.youtube_import import (
    YouTubeImportError,
    fetch_public_subscription_channels_from_api,
    fetch_public_subscription_channels_from_scrape,
    fetch_user_subscription_channels_from_browser,
)
from yutome.youtube_oauth import fetch_subscription_channels, load_oauth_client, load_or_authorize_token


DEFAULT_HOSTED_APP_URL = "https://app.getyutome.com"
DEFAULT_HOSTED_API_URL = "https://api-production-e072.up.railway.app"
HOSTED_AUTH_FILENAME = "yutome-hosted-cli.json"


def _version_callback(value: bool) -> None:
    if not value:
        return
    try:
        typer.echo(version("yutome"))
    except PackageNotFoundError:
        typer.echo("0.0.0+editable")
    raise typer.Exit()


def _project_root(config_path: Path) -> Path:
    return config_path.parent if config_path.is_absolute() else (Path.cwd() / config_path).parent


def _load_config_or_exit(config_path: Path) -> AppConfig:
    try:
        load_dotenv(_project_root(config_path) / ".env")
        return apply_env_to_config(load_config(config_path))
    except FileNotFoundError as exc:
        typer.echo(f"yutome config not found at {config_path}. Run: yutome setup", err=True)
        raise typer.Exit(code=2) from exc
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc


def _load_runtime(config_path: Path) -> runtime.Runtime:
    try:
        rt = runtime.configure(config_path)
    except FileNotFoundError as exc:
        typer.echo(f"yutome config not found at {config_path}. Run: yutome setup", err=True)
        raise typer.Exit(code=2) from exc
    rt.paths.ensure_base_dirs()
    return rt


def _runner(config_path: Path) -> HostedCommandRunner:
    return HostedCommandRunner(_load_config_or_exit(config_path))


def _workspace_id(app_config: AppConfig, explicit: str | None = None) -> str:
    value = (explicit or app_config.hosted.workspace_id or app_config.hosted.local_workspace_id).strip()
    if not value:
        raise HostedRuntimeError("No workspace configured. Run: yutome setup")
    return value


def _jsonable(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")  # type: ignore[no-any-return, attr-defined]
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _echo_json(value: object) -> None:
    typer.echo(json.dumps(_jsonable(value), indent=2, sort_keys=True))


def _echo_result(value: object, *, json_output: bool, message: str | None = None) -> None:
    if json_output:
        _echo_json(value)
    elif message:
        typer.echo(message)


def _die(message: str, *, json_output: bool = False, code: int = 1) -> None:
    if json_output:
        _echo_json({"ok": False, "error": message})
    else:
        typer.echo(message, err=True)
    raise typer.Exit(code=code)


def _read_query_request(request: str | None, file: Path | None) -> dict[str, object]:
    if file is not None:
        return json.loads(file.read_text(encoding="utf-8"))
    if request == "-":
        return json.loads(sys.stdin.read())
    if request:
        return json.loads(request)
    raise typer.BadParameter("Pass a JSON request argument, '-' for stdin, or --file.")


def _read_rows(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if hasattr(result, "mappings"):
        return [dict(row) for row in result.mappings()]
    if hasattr(result, "fetchall"):
        rows = result.fetchall()
    else:
        rows = list(result)
    return [dict(row) for row in rows]


def _hosted_auth_path(paths: ProjectPaths) -> Path:
    return paths.data_dir / "hosted" / HOSTED_AUTH_FILENAME


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _load_hosted_auth(paths: ProjectPaths) -> dict[str, Any]:
    auth_path = _hosted_auth_path(paths)
    if not auth_path.exists():
        raise HostedRuntimeError("Run `yutome hosted login` before using hosted account API commands.")
    payload = json.loads(auth_path.read_text(encoding="utf-8"))
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise HostedRuntimeError("Hosted CLI auth file is missing an access token. Run `yutome hosted login` again.")
    return payload


def _hosted_api_url(app_config: AppConfig, override: str | None = None) -> str:
    return (override or app_config.hosted.api_url or DEFAULT_HOSTED_API_URL).rstrip("/")


def _hosted_app_url(app_config: AppConfig, override: str | None = None) -> str:
    return (override or app_config.hosted.app_url or DEFAULT_HOSTED_APP_URL).rstrip("/")


def _hosted_api_request_json(
    *,
    api_base: str,
    path: str,
    method: str = "GET",
    token: str | None = None,
    payload: Mapping[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json", "User-Agent": "yutome-cli/0.1"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{api_base.rstrip('/')}/{path.lstrip('/')}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HostedRuntimeError(_hosted_api_error_message(detail, fallback=f"Hosted API returned HTTP {exc.code}.")) from exc
    except urllib.error.URLError as exc:
        raise HostedRuntimeError(f"Could not reach hosted API at {api_base}: {exc.reason}") from exc
    parsed = json.loads(raw or "{}")
    if not isinstance(parsed, dict):
        raise HostedRuntimeError("Hosted API returned non-object JSON.")
    if parsed.get("ok") is False:
        raise HostedRuntimeError(_hosted_api_error_message(json.dumps(parsed), fallback="Hosted API request failed."))
    return parsed


def _hosted_api_error_message(raw: str, *, fallback: str) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip() or fallback
    detail = payload.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("code") or fallback)
    if isinstance(detail, str):
        return detail
    return str(payload.get("message") or payload.get("error") or fallback)


def _lease_owner(explicit: str | None) -> str:
    return explicit or os.environ.get("RAILWAY_REPLICA_ID") or f"yutome-cli-{os.getpid()}"


def _parse_phase(value: str, *, json_output: bool) -> str:
    phase = value.strip().lower()
    if phase not in {"phase1", "phase4", "hosted"}:
        _die("phase must be one of: phase1, phase4, hosted", json_output=json_output, code=2)
    return phase


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def setup(
    *,
    channel: str | None,
    config: Path = Path(DEFAULT_CONFIG_FILENAME),
    yes: bool = False,
) -> None:
    """Create a greenfield Postgres-backed Yutome project config."""
    project_root = _project_root(config)
    wrote = write_default_config(config, overwrite=False)
    env_path = project_root / ".env"
    load_dotenv(env_path)
    app_config = apply_env_to_config(load_config(config))

    typer.echo(f"[OK] {'Wrote' if wrote else 'Using existing'} config: {config}")

    # Ensure a local workspace identity so corpus/search commands don't hard-fail. A
    # personal workspace_id (written by `yutome hosted login`) takes precedence; otherwise
    # generate and persist a local fallback once. Beginners never have to learn the concept.
    if not app_config.hosted.workspace_id and not app_config.hosted.local_workspace_id:
        local_workspace_id = _generate_local_workspace_id()
        _set_toml_string(config, "hosted", "local_workspace_id", local_workspace_id)
        app_config = apply_env_to_config(load_config(config))
        typer.echo(f"[OK] Local workspace: {local_workspace_id}")

    # Interactive first-run wizard: capture the Postgres DSN and an optional Voyage key
    # into ./.env. Skipped entirely under -y; on a non-TTY the setup_prompts wrappers fall
    # back to typer.prompt so scripted `yutome setup < input` still works.
    if not yes and setup_prompts.is_interactive():
        dsn_env = app_config.database.postgres_url_env
        current_dsn = os.environ.get(dsn_env, "")
        dsn = setup_prompts.text(
            f"Postgres DSN (VectorChord Suite) [{dsn_env}]",
            default=current_dsn or "postgresql://yutome:yutome@localhost:5432/yutome",
        )
        if dsn and dsn != current_dsn:
            _set_env_var(env_path, dsn_env, dsn)
            os.environ[dsn_env] = dsn
        if not os.environ.get("VOYAGE_API_KEY"):
            voyage_key = setup_prompts.password(
                "Voyage API key (optional — enables semantic/hybrid search; press Enter to skip)"
            )
            if voyage_key:
                _set_env_var(env_path, "VOYAGE_API_KEY", voyage_key)
                os.environ["VOYAGE_API_KEY"] = voyage_key

    paths = ProjectPaths.from_config(app_config, project_root=project_root)
    paths.ensure_base_dirs()
    typer.echo(f"[OK] Data directory: {paths.data_dir}")

    url = postgres_url_from_env(url_env=app_config.database.postgres_url_env)
    if url:
        applied = HostedCommandRunner(app_config).migrate(phase="hosted")
        typer.echo(f"[OK] Postgres reachable: {redact_postgres_url(url)}")
        typer.echo(f"[OK] Applied {applied} migration statements.")
    else:
        typer.echo(f"[WARN] Set {app_config.database.postgres_url_env} to a VectorChord Postgres DSN before indexing.")

    if channel:
        add_sources(targets=[channel], config=config, title=None, selected=True)
    elif not yes:
        typer.echo("Next: yutome corpus add <youtube-url-or-handle> && yutome corpus sync")


def add_sources(*, targets: list[str], config: Path, title: str | None = None, selected: bool = True) -> None:
    rt = _load_runtime(config)
    workspace_id = _workspace_id(rt.config)
    connection = connect_postgres(url_env=rt.config.database.postgres_url_env)
    added = 0
    for target in targets:
        source = source_from_input(target, title=title if len(targets) == 1 else None, import_source="cli")
        if source is None:
            typer.echo(f"[WARN] Skipped empty source: {target!r}", err=True)
            continue
        upsert_library_source(connection, source, workspace_id=workspace_id, selected=selected)
        added += 1
    typer.echo(f"Added {added} source{'s' if added != 1 else ''} to workspace {workspace_id}.")


def import_command(*, path: Path, config: Path, selected: bool = True) -> None:
    rt = _load_runtime(config)
    workspace_id = _workspace_id(rt.config)
    connection = connect_postgres(url_env=rt.config.database.postgres_url_env)
    sources = import_sources_from_file(path, selected=selected)
    for source in sources:
        upsert_library_source(connection, source, workspace_id=workspace_id, selected=selected)
    typer.echo(f"Imported {len(sources)} source{'s' if len(sources) != 1 else ''} into workspace {workspace_id}.")


def import_youtube(
    *,
    target: str | None,
    config: Path,
    port: int = 0,
    open_browser: bool = True,
    selected: bool = True,
) -> None:
    rt = _load_runtime(config)
    workspace_id = _workspace_id(rt.config)
    project_root = _project_root(config)
    env_path = project_root / ".env"
    connection = connect_postgres(url_env=rt.config.database.postgres_url_env)
    if target:
        api_key = os.environ.get(rt.config.youtube.api_key_env, "").strip()
        try:
            channels = fetch_public_subscription_channels_from_api(target, api_key=api_key) if api_key else fetch_public_subscription_channels_from_scrape(target)
        except YouTubeImportError as exc:
            raise typer.BadParameter(str(exc)) from exc
    elif rt.config.youtube.oauth_client_secrets:
        client = load_oauth_client(rt.config.youtube.oauth_client_secrets)
        token = load_or_authorize_token(
            client=client,
            token_path=rt.paths.data_dir / "youtube-oauth-token.json",
            port=port,
            open_browser=open_browser,
            status_callback=typer.echo,
        )
        channels = fetch_subscription_channels(str(token["access_token"]))
    else:
        try:
            channels = fetch_user_subscription_channels_from_browser(
                browsers=rt.config.youtube.browser_cookie_browsers,
                status_callback=typer.echo,
            )
        except YouTubeImportError as exc:
            raise typer.BadParameter(
                f"{exc} Set YUTOME_YOUTUBE_OAUTH_CLIENT_SECRETS in {env_path} for OAuth import."
            ) from exc
    for channel in channels:
        source = source_from_input(channel.source_url, title=channel.title, import_source=channel.import_source)
        if source is not None:
            upsert_library_source(connection, source, workspace_id=workspace_id, selected=selected)
    typer.echo(f"Imported {len(channels)} YouTube subscription source{'s' if len(channels) != 1 else ''}.")


def select_source(*, selector: str, config: Path) -> None:
    _set_source_selected(selector=selector, config=config, selected=True)


def unselect_source(*, selector: str, config: Path) -> None:
    _set_source_selected(selector=selector, config=config, selected=False)


def _set_source_selected(*, selector: str, config: Path, selected: bool) -> None:
    rt = _load_runtime(config)
    workspace_id = _workspace_id(rt.config)
    connection = connect_postgres(url_env=rt.config.database.postgres_url_env)
    count = set_library_source_selected(connection, workspace_id=workspace_id, selector=selector, selected=selected)
    typer.echo(f"Updated {count} source{'s' if count != 1 else ''}.")


def sync(
    *,
    target: str | None,
    config: Path,
    limit: int | None = None,
    max_process: int | None = None,
) -> None:
    runner = _runner(config)
    workspace_id = _workspace_id(runner.config)
    if target:
        seeded = runner.source_add(
            workspace_id=workspace_id,
            source_url=target,
            max_new_videos_per_run=limit or runner.config.backfill.max_videos_per_run,
        )
        typer.echo(f"Seeded source {seeded.source_id}.")
    refresh = runner.source_refresh_tick(lease_owner=_lease_owner(None), limit=limit or 25)
    worker = runner.worker_once(lease_owner=_lease_owner(None), limit=max_process or runner.config.backfill.batch_size, workspace_id=workspace_id)
    typer.echo(f"Source refresh claimed {refresh.affected_rows or 0}; worker claimed {worker.affected_rows or 0}.")


def rebuild_chunks(*, config: Path) -> None:
    rt = _load_runtime(config)
    connection = connect_postgres(url_env=rt.config.database.postgres_url_env)
    stats = rebuild_active_chunks(connection=connection, workspace_id=_workspace_id(rt.config), paths=rt.paths)
    typer.echo(f"Rebuilt {stats.rebuilt_chunks} chunks across {stats.rebuilt_videos} videos; skipped {stats.skipped}.")


def rebuild_vectors(
    *,
    config: Path,
    limit: int | None = None,
) -> None:
    runner = _runner(config)
    workspace_id = _workspace_id(runner.config)
    result = runner.worker_once(lease_owner=_lease_owner(None), limit=limit or runner.config.backfill.batch_size, workspace_id=workspace_id)
    typer.echo(f"Worker claimed {result.affected_rows or 0} indexing job{'s' if (result.affected_rows or 0) != 1 else ''}.")


def quality_upgrade(
    *,
    config: Path,
    video_id: str | None,
    limit: int | None,
    video_workers: int | None,
    batch_segments: int | None,
    concurrency: int | None,
    max_patch_retries: int | None,
    source_filter: list[str] | None,
    all_transcripts: bool,
    rebuild_vectors: bool,
) -> None:
    rt = _load_runtime(config)
    app_config = rt.config.model_copy(deep=True)
    if video_workers is not None:
        app_config.transcript_cleanup.video_workers = video_workers
    if batch_segments is not None:
        app_config.transcript_cleanup.batch_segments = batch_segments
    if concurrency is not None:
        app_config.transcript_cleanup.concurrency = concurrency
    if max_patch_retries is not None:
        app_config.transcript_cleanup.max_patch_retries = max_patch_retries
    effective_limit = None if all_transcripts else limit
    connection = connect_postgres(url_env=app_config.database.postgres_url_env)
    stats = upgrade_active_transcripts(
        connection=connection,
        workspace_id=_workspace_id(app_config),
        config=app_config,
        paths=rt.paths,
        limit=effective_limit,
        video_id=video_id,
        source_filters=source_filter,
        progress=typer.echo,
    )
    typer.echo(f"Upgraded {stats.upgraded}/{stats.scanned} transcripts; saved {stats.chunks_saved} chunks.")
    if rebuild_vectors and stats.upgraded:
        runner = HostedCommandRunner(app_config, connection=connection)
        result = runner.worker_once(lease_owner=_lease_owner(None), limit=stats.upgraded, workspace_id=_workspace_id(app_config))
        typer.echo(f"Worker claimed {result.affected_rows or 0} vector refresh job{'s' if (result.affected_rows or 0) != 1 else ''}.")


def export_portable_markdown(*, config: Path) -> None:
    _export_markdown(config=config, mode="portable-md")


def export_obsidian(*, config: Path) -> None:
    _export_markdown(config=config, mode="obsidian")


def _export_markdown(*, config: Path, mode: str) -> None:
    rt = _load_runtime(config)
    connection = connect_postgres(url_env=rt.config.database.postgres_url_env)
    stats = export_markdown(connection=connection, workspace_id=_workspace_id(rt.config), paths=rt.paths, mode=mode)  # type: ignore[arg-type]
    typer.echo(f"Exported {stats.exported} markdown file{'s' if stats.exported != 1 else ''} to {stats.output_dir}.")


def doctor(*, config: Path) -> None:
    rt = _load_runtime(config)
    url = postgres_url_from_env(url_env=rt.config.database.postgres_url_env)
    if not url:
        typer.echo(f"[FAIL] {rt.config.database.postgres_url_env} is not set.", err=True)
        raise typer.Exit(code=1)
    try:
        workspace_id = _workspace_id(rt.config)
    except HostedRuntimeError:
        typer.echo("[FAIL] No workspace configured. Run: yutome setup", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"[OK] Workspace: {workspace_id}")
    result = HostedCommandRunner(rt.config).db_check()
    _echo_json(result)
    if not result.ok:
        raise typer.Exit(code=1)


def hosted_api(*, config: Path, host: str, port: int, log_level: str = "info") -> None:
    api_app = build_hosted_api_app(_runner(config))
    import uvicorn

    uvicorn.run(api_app, host=host, port=port, log_level=log_level)


def hosted_migrate(*, config: Path, phase: str, json_output: bool = False) -> None:
    phase_value = _parse_phase(phase, json_output=json_output)
    try:
        applied = _runner(config).migrate(phase=phase_value)  # type: ignore[arg-type]
    except HostedRuntimeError as exc:
        _die(str(exc), json_output=json_output)
    payload = {"ok": True, "phase": phase_value, "applied": applied}
    _echo_result(payload, json_output=json_output, message=f"Applied {applied} migration statements ({phase_value}).")


def hosted_db_check(*, config: Path, json_output: bool = False) -> None:
    try:
        result = _runner(config).db_check()
    except HostedRuntimeError as exc:
        _die(str(exc), json_output=json_output)
    _echo_result(result, json_output=json_output, message=json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
    if not result.ok:
        raise typer.Exit(code=1)


def hosted_login(
    *,
    config: Path,
    app_url: str | None,
    api_url: str | None,
    port: int,
    open_browser: bool,
    json_output: bool,
) -> None:
    rt = _load_runtime(config)
    verifier = new_code_verifier()
    challenge = code_challenge_for_verifier(verifier)
    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if params.get("state", [None])[0] != state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Hosted CLI login state mismatch.")
                return
            if error := params.get("error", [None])[0]:
                result["error"] = error
            if code := params.get("code", [None])[0]:
                result["code"] = code
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Yutome CLI login complete. You can close this window.")

        def log_message(self, _format: str, *args: object) -> None:
            return

    with http.server.HTTPServer(("127.0.0.1", port), Handler) as server:
        redirect_uri = f"http://127.0.0.1:{server.server_port}/"
        authorize_url = _hosted_app_url(rt.config, app_url).rstrip("/") + "/cli/authorize?" + urllib.parse.urlencode(
            {
                "client_id": DEFAULT_CLI_CLIENT_ID,
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": " ".join(DEFAULT_CLI_SCOPES),
            }
        )
        if open_browser:
            webbrowser.open(authorize_url)
        else:
            typer.echo(authorize_url)
        server.handle_request()
    if result.get("error"):
        _die(f"Hosted CLI login failed: {result['error']}", json_output=json_output)
    code = result.get("code")
    if not code:
        _die("Hosted CLI login did not return an authorization code.", json_output=json_output)
    api_base = _hosted_api_url(rt.config, api_url)
    token_response = _hosted_api_request_json(
        api_base=api_base,
        path="/account/cli/token",
        method="POST",
        payload={"code": code, "code_verifier": verifier, "redirect_uri": redirect_uri},
    )
    auth_payload = {
        **token_response,
        "api_url": api_base,
        "app_url": _hosted_app_url(rt.config, app_url),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_private_json(_hosted_auth_path(rt.paths), auth_payload)
    if workspace_id := token_response.get("workspace_id"):
        _set_toml_string(config, "hosted", "workspace_id", str(workspace_id))
    _set_toml_string(config, "hosted", "api_url", api_base)
    _set_toml_string(config, "hosted", "app_url", _hosted_app_url(rt.config, app_url))
    _echo_result(auth_payload, json_output=json_output, message=f"Logged in to workspace {token_response.get('workspace_id')}.")


def hosted_jobs(*, config: Path, limit: int, json_output: bool = False) -> None:
    rt = _load_runtime(config)
    auth = _load_hosted_auth(rt.paths)
    result = _hosted_api_request_json(
        api_base=str(auth.get("api_url") or rt.config.hosted.api_url),
        path=f"/account/jobs?limit={max(1, min(limit, 100))}",
        token=str(auth["access_token"]),
    )
    _echo_result(result, json_output=json_output, message=f"Fetched {len(result.get('jobs', []))} job(s).")


def hosted_source_add(
    *,
    source_url: str,
    config: Path,
    workspace_id: str | None,
    display_name: str | None,
    cadence_seconds: int,
    max_new_videos: int,
    refresh_enabled: bool,
    json_output: bool,
) -> None:
    try:
        runner = _runner(config)
        result = runner.source_add(
            workspace_id=_workspace_id(runner.config, workspace_id),
            source_url=source_url,
            display_name=display_name,
            cadence_seconds=cadence_seconds,
            max_new_videos_per_run=max_new_videos,
            refresh_enabled=refresh_enabled,
        )
    except HostedRuntimeError as exc:
        _die(str(exc), json_output=json_output)
    _echo_result(result, json_output=json_output, message=f"Seeded source {result.source_id}.")


def hosted_worker(
    *,
    config: Path,
    once: bool,
    lease_owner: str | None,
    workspace_id: str | None,
    limit: int,
    lease_seconds: int,
    poll_interval: float,
    json_output: bool,
) -> None:
    try:
        runner = _runner(config)
        while True:
            result = runner.worker_once(
                lease_owner=_lease_owner(lease_owner),
                workspace_id=workspace_id,
                limit=limit,
                lease_seconds=lease_seconds,
            )
            _echo_result(result, json_output=json_output, message=f"Worker tick claimed {result.affected_rows or 0} jobs.")
            if once:
                return
            time.sleep(poll_interval)
    except HostedRuntimeError as exc:
        _die(str(exc), json_output=json_output)


def hosted_source_refresh_tick(
    *,
    config: Path,
    once: bool,
    lease_owner: str | None,
    limit: int,
    lock_seconds: int,
    poll_interval: float,
    json_output: bool,
) -> None:
    try:
        runner = _runner(config)
        while True:
            result = runner.source_refresh_tick(lease_owner=_lease_owner(lease_owner), limit=limit, lock_seconds=lock_seconds)
            _echo_result(result, json_output=json_output, message=f"Source refresh tick claimed {result.affected_rows or 0} policies.")
            if once:
                return
            time.sleep(poll_interval)
    except HostedRuntimeError as exc:
        _die(str(exc), json_output=json_output)


def hosted_maintenance_tick(*, config: Path, once: bool, limit: int, poll_interval: float, json_output: bool) -> None:
    try:
        runner = _runner(config)
        while True:
            result = runner.maintenance_tick(limit=limit)
            _echo_result(result, json_output=json_output, message=f"Maintenance tick released {result.affected_rows or 0} rows.")
            if once:
                return
            time.sleep(poll_interval)
    except HostedRuntimeError as exc:
        _die(str(exc), json_output=json_output)


def hosted_balance_rollover(*, config: Path, once: bool, limit: int, poll_interval: float, json_output: bool) -> None:
    try:
        runner = _runner(config)
        while True:
            result = runner.balance_rollover_once(limit=limit)
            _echo_result(
                result,
                json_output=json_output,
                message=f"Balance rollover opened {result.affected_rows or 0} periods.",
            )
            if once:
                return
            time.sleep(poll_interval)
    except HostedRuntimeError as exc:
        _die(str(exc), json_output=json_output)


def hosted_stripe_meter_export_worker(
    *,
    config: Path,
    once: bool,
    lease_owner: str | None,
    limit: int,
    poll_interval: float,
    json_output: bool,
) -> None:
    try:
        runner = _runner(config)
        while True:
            result = runner.stripe_meter_export_once(lease_owner=_lease_owner(lease_owner), limit=limit)
            _echo_result(
                result,
                json_output=json_output,
                message=f"Stripe meter export claimed {result.affected_rows} rows.",
            )
            if once:
                return
            time.sleep(poll_interval)
    except HostedRuntimeError as exc:
        _die(str(exc), json_output=json_output)


def usage_command(
    *,
    config: Path,
    limit: int,
    summary: bool,
    json_output: bool,
) -> None:
    rt = _load_runtime(config)
    connection = connect_postgres(url_env=rt.config.database.postgres_url_env)
    events = PostgresUsageLedger(connection).recent(workspace_id=_workspace_id(rt.config), limit=limit)  # type: ignore[attr-defined]
    if summary:
        rows = summarize_usage_events(events)
    else:
        rows = [event.model_dump(mode="json") for event in events]
    _echo_result(rows, json_output=json_output, message=json.dumps(_jsonable(rows), indent=2, sort_keys=True))


def mcp_serve(*, config: Path) -> None:
    run_stdio_server(config)


def http_serve(
    *,
    config: Path,
    host: str,
    port: int,
    cors_origin: list[str] | None,
    insecure: bool,
) -> None:
    run_http_server(
        config,
        host=host,
        port=port,
        cors_origins=cors_origin,
        require_token_for_non_loopback=True,
        allow_no_auth=insecure,
    )


def remote_prepare(*, config: Path, rotate: bool, show_token: bool) -> None:
    token = secrets.token_urlsafe(32) if rotate else os.environ.get("YUTOME_HTTP_TOKEN") or secrets.token_urlsafe(32)
    typer.echo(f"YUTOME_HTTP_TOKEN={token}" if show_token else "Set YUTOME_HTTP_TOKEN to the generated token.")
    if not show_token:
        typer.echo("Pass --show-token to print it once.")
    _load_runtime(config).paths.ensure_base_dirs()


def remote_serve(*, config: Path, host: str, port: int, cors_origin: list[str] | None) -> None:
    run_http_server(config, host=host, port=port, cors_origins=cors_origin, require_token_for_non_loopback=True)


def remote_mcp(*, config: Path, host: str, port: int, path: str, server_url: str | None) -> None:
    run_streamable_http_server(config, host=host, port=port, path=path, require_token_for_non_loopback=True, server_url=server_url)


def connect_command(
    *,
    config: Path,
    endpoint: str | None,
    deploy: bool,
    worker_name: str | None,
    relay_token: str | None,
    pairing_code: str | None,
) -> None:
    from yutome.cli import _bridge
    from yutome.cli._worker_deploy import _deploy_tracked_worker

    rt = _load_runtime(config)
    effective_endpoint = endpoint
    effective_worker_name = worker_name
    effective_relay_token = relay_token
    effective_pairing_code = pairing_code
    if deploy:
        deployed_url, deployed_worker, generated_relay, generated_pairing = _deploy_tracked_worker(
            paths=rt.paths,
            relay_token=relay_token,
            pairing_code=pairing_code,
        )
        effective_endpoint = deployed_url or endpoint
        effective_worker_name = worker_name or deployed_worker
        effective_relay_token = generated_relay
        effective_pairing_code = generated_pairing
    if not effective_endpoint:
        raise typer.BadParameter("Pass --endpoint or --deploy.")
    state = build_remote_state(
        endpoint=effective_endpoint,
        mode="connector_only",
        worker_name=effective_worker_name,
        relay_token=effective_relay_token,
        pairing_code=effective_pairing_code,
        existing=load_remote_state(rt.paths),
    )
    save_remote_state(rt.paths, state)
    _bridge._finalize_remote_bridge_setup(config_path=config, paths=rt.paths)
    typer.echo(f"[OK] Remote MCP endpoint: {state.mcp_url}")
    if state.pairing_code:
        typer.echo(f"Pairing code: {state.pairing_code}")


def disconnect_command(
    *,
    config: Path,
    worker_name: str | None,
    remove_cloudflare: bool,
    keep_state: bool,
    dry_run: bool,
) -> None:
    from yutome.cli._worker_deploy import _delete_tracked_worker

    rt = _load_runtime(config)
    state = load_remote_state(rt.paths)
    target_worker = worker_name or ((state.cloud_resources or {}).get("cloudflare_worker_name") if state else None)
    if dry_run:
        _echo_json({"state_path": remote_state_path(rt.paths), "cloudflare_worker": target_worker, "remove_cloudflare": remove_cloudflare})
        return
    if remove_cloudflare and target_worker:
        _delete_tracked_worker(str(target_worker))
    if not keep_state:
        remote_state_path(rt.paths).unlink(missing_ok=True)
    typer.echo("[OK] Disconnected remote connector state.")


def status_command(*, config: Path, json_output: bool = False) -> None:
    rt = _load_runtime(config)
    status = {
        "postgres_url": redact_postgres_url(postgres_url_from_env(url_env=rt.config.database.postgres_url_env)),
        "workspace_id": rt.config.hosted.workspace_id,
        "remote": remote_status_payload(rt.paths, live=False),
    }
    if rt.config.hosted.workspace_id and status["postgres_url"]:
        try:
            status["database"] = HostedCommandRunner(rt.config).db_check().model_dump(mode="json")
        except HostedRuntimeError as exc:
            status["database"] = {"ok": False, "error": str(exc)}
    _echo_result(status, json_output=json_output, message=json.dumps(status, indent=2, sort_keys=True))


def proxy_info() -> None:
    typer.echo("Proxy configuration lives in [proxy] and environment variables YUTOME_PROXY_URLS / YUTOME_WEBSHARE_*.")


def proxy_test(*, video_id: str, config: Path, transcript_api: bool, ytdlp_subtitles: bool) -> None:
    from yutome.youtube import fetch_subtitle_transcript_with_ytdlp, fetch_transcript

    rt = _load_runtime(config)
    if transcript_api:
        result = fetch_transcript(
            video_id=video_id,
            languages=rt.config.transcript.preferred_languages,
            proxy=rt.config.proxy,
            timeout_seconds=rt.config.transcript.request_timeout_seconds,
        )
        typer.echo(f"[OK] transcript-api returned {len(result.raw_snippets)} snippets via {result.source}.")
    if ytdlp_subtitles:
        result = fetch_subtitle_transcript_with_ytdlp(
            video_id=video_id,
            cwd=rt.paths.cache_dir,
            language=rt.config.transcript.preferred_languages[0] if rt.config.transcript.preferred_languages else "en",
            proxy=rt.config.proxy,
            ytdlp_config=rt.config.ytdlp,
            allow_translated_captions=rt.config.transcript.allow_translated_captions,
        )
        typer.echo(f"[OK] yt-dlp returned {len(result.raw_snippets)} snippets via {result.source}.")


def gemini_test(*, video_id: str, config: Path) -> None:
    from yutome.gemini import transcribe_youtube_url_with_gemini

    rt = _load_runtime(config)
    if not rt.config.gemini.enabled:
        raise typer.BadParameter("Gemini is disabled in config.")
    result = transcribe_youtube_url_with_gemini(video_id=video_id, config=rt.config.gemini)
    typer.echo(f"[OK] Gemini returned {len(result.raw_snippets)} snippets via {result.source}.")


def eval_run(*, suite: Path, config: Path, json_output: bool) -> None:
    from yutome.evals import load_eval_suite, run_eval_suite

    rt = _load_runtime(config)
    result = run_eval_suite(config=rt.config, paths=rt.paths, suite=load_eval_suite(suite))
    _echo_result(result, json_output=json_output, message=json.dumps(result, indent=2, sort_keys=True))
    if result["failed"]:
        raise typer.Exit(code=1)


def remote_check(*, base_url: str, token: str | None, timeout: float) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    payload = _http_json("GET", f"{base_url.rstrip('/')}/healthz", headers=headers, timeout=timeout)
    _echo_json(payload)


def _http_json(method: str, url: str, *, timeout: float, headers: Mapping[str, str] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(url, method=method, headers=dict(headers or {}))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {"payload": payload}


def _set_toml_string(config_path: Path, section: str, key: str, value: str) -> None:
    if not config_path.exists():
        return
    lines = config_path.read_text(encoding="utf-8").splitlines()
    target = f"[{section}]"
    in_section = False
    wrote = False
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section and not wrote:
                output.append(f'{key} = "{value}"')
                wrote = True
            in_section = stripped == target
        if in_section and stripped.startswith(f"{key} "):
            output.append(f'{key} = "{value}"')
            wrote = True
            continue
        output.append(line)
    if not wrote:
        if not any(line.strip() == target for line in output):
            output.extend(["", target])
        output.append(f'{key} = "{value}"')
    config_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def _generate_local_workspace_id() -> str:
    # Matches the ws_<24hex> shape of deterministic_personal_workspace_id (account.py) so
    # every workspace_id-keyed row and SQL path stays uniform. Generated (not a constant)
    # so two local installs — or a later hosted sign-in — never share one tenant partition.
    return "ws_" + secrets.token_hex(12)


def _set_env_var(env_path: Path, key: str, value: str) -> None:
    # Persist a bare KEY=VALUE line to .env, replacing an existing line for the same key in
    # place. Matches the .env.example convention; load_dotenv (env.py) splits on the first
    # '=' and strips quotes, and uses os.environ.setdefault so an already-exported shell var
    # still wins over the file.
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    output: list[str] = []
    wrote = False
    for line in lines:
        if not wrote and line.split("=", 1)[0].strip() == key:
            output.append(f"{key}={value}")
            wrote = True
            continue
        output.append(line)
    if not wrote:
        output.append(f"{key}={value}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
