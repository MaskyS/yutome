from __future__ import annotations

import typer

from . import actions
from .context import config_path

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Hosted Postgres runtime commands.")
source_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Hosted source operations.")
app.add_typer(source_app, name="source")


@app.command("api")
def api_command(
    ctx: typer.Context,
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address for the hosted MCP query API."),
    port: int = typer.Option(8000, "--port", min=1, help="Bind port."),
    log_level: str = typer.Option("info", "--log-level", help="Uvicorn log level."),
) -> None:
    """Run the hosted MCP query API."""
    actions.hosted_api(config=config_path(ctx), host=host, port=port, log_level=log_level)


@app.command("migrate")
def migrate_command(
    ctx: typer.Context,
    phase: str = typer.Option("hosted", "--phase", help="Migration phase: phase1, phase4, or hosted."),
    json_output: bool = typer.Option(False, "--json", help="Emit migration result as JSON."),
) -> None:
    """Apply hosted Postgres migrations."""
    actions.hosted_migrate(config=config_path(ctx), phase=phase, json_output=json_output)


@app.command("login")
def login_command(
    ctx: typer.Context,
    app_url: str | None = typer.Option(None, "--app-url", help="Hosted Yutome app URL."),
    api_url: str | None = typer.Option(None, "--api-url", help="Hosted Yutome API URL."),
    port: int = typer.Option(0, "--port", min=0, max=65535, help="Local callback port."),
    open_browser: bool = typer.Option(True, "--open-browser/--print-url", help="Open the login URL in a browser."),
    json_output: bool = typer.Option(False, "--json", help="Emit hosted auth state as JSON."),
) -> None:
    """Authorize this CLI against a hosted Yutome account."""
    actions.hosted_login(
        config=config_path(ctx),
        app_url=app_url,
        api_url=api_url,
        port=port,
        open_browser=open_browser,
        json_output=json_output,
    )


@app.command("jobs")
def jobs_command(
    ctx: typer.Context,
    limit: int = typer.Option(25, "--limit", min=1, max=100, help="Maximum hosted jobs to return."),
    json_output: bool = typer.Option(False, "--json", help="Emit hosted jobs as JSON."),
) -> None:
    """Read recent hosted jobs for the logged-in workspace."""
    actions.hosted_jobs(config=config_path(ctx), limit=limit, json_output=json_output)


@app.command("usage")
def usage_command(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit", "-n", min=0, help="Maximum usage events to show."),
    summary: bool = typer.Option(False, "--summary", help="Summarize usage totals."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON output."),
) -> None:
    """Inspect hosted provider/search-store usage events."""
    actions.usage_command(
        config=config_path(ctx),
        limit=limit,
        summary=summary,
        json_output=json_output,
    )


@source_app.command("add")
def source_add_command(
    ctx: typer.Context,
    source_url: str = typer.Argument(..., help="Public YouTube channel, handle, playlist, or video URL."),
    workspace_id: str | None = typer.Option(None, "--workspace-id", help="Hosted workspace id."),
    display_name: str | None = typer.Option(None, "--display-name", help="Optional source display name."),
    cadence_seconds: int = typer.Option(900, "--cadence-seconds", min=1, help="Source refresh cadence."),
    max_new_videos: int = typer.Option(25, "--max-new-videos", min=1, help="Maximum discovered videos per run."),
    refresh_enabled: bool = typer.Option(True, "--refresh/--no-refresh", help="Create or update refresh policy."),
    json_output: bool = typer.Option(False, "--json", help="Emit seed result as JSON."),
) -> None:
    """Create or update a hosted source and refresh policy."""
    actions.hosted_source_add(
        source_url=source_url,
        config=config_path(ctx),
        workspace_id=workspace_id,
        display_name=display_name,
        cadence_seconds=cadence_seconds,
        max_new_videos=max_new_videos,
        refresh_enabled=refresh_enabled,
        json_output=json_output,
    )


@app.command("run")
def run_command(
    ctx: typer.Context,
    job: str = typer.Argument(
        ...,
        help="worker, stripe-meter-export, source-refresh, maintenance, or balance-rollover.",
    ),
    once: bool = typer.Option(False, "--once", help="Run one tick and exit where supported."),
    lease_owner: str | None = typer.Option(None, "--lease-owner", help="Lease owner id."),
    workspace_id: str | None = typer.Option(None, "--workspace-id", help="Optional workspace scope for worker."),
    limit: int = typer.Option(1, "--limit", min=1, help="Maximum rows/jobs to process."),
    lease_seconds: int = typer.Option(900, "--lease-seconds", min=1, help="Job lease duration in seconds."),
    lock_seconds: int = typer.Option(900, "--lock-seconds", min=1, help="Refresh policy lock duration."),
    poll_interval: float = typer.Option(5.0, "--poll-interval", min=0.1, help="Loop sleep when --once is not set."),
    json_output: bool = typer.Option(False, "--json", help="Emit tick result as JSON."),
) -> None:
    """Run a hosted daemon job."""
    normalized = job.lower()
    if normalized == "worker":
        actions.hosted_worker(
            config=config_path(ctx),
            once=once,
            lease_owner=lease_owner,
            workspace_id=workspace_id,
            limit=limit,
            lease_seconds=lease_seconds,
            poll_interval=poll_interval,
            json_output=json_output,
        )
        return
    if normalized == "stripe-meter-export":
        actions.hosted_stripe_meter_export_worker(
            config=config_path(ctx),
            once=once,
            lease_owner=lease_owner,
            limit=limit,
            poll_interval=poll_interval,
            json_output=json_output,
        )
        return
    if normalized == "source-refresh":
        actions.hosted_source_refresh_tick(
            config=config_path(ctx),
            once=once,
            lease_owner=lease_owner,
            limit=limit,
            lock_seconds=lock_seconds,
            poll_interval=poll_interval,
            json_output=json_output,
        )
        return
    if normalized == "maintenance":
        actions.hosted_maintenance_tick(
            config=config_path(ctx),
            once=once,
            limit=limit,
            poll_interval=poll_interval,
            json_output=json_output,
        )
        return
    if normalized == "balance-rollover":
        actions.hosted_balance_rollover(
            config=config_path(ctx),
            once=once,
            limit=limit,
            poll_interval=poll_interval,
            json_output=json_output,
        )
        return
    raise typer.BadParameter(
        "job must be one of: worker, stripe-meter-export, source-refresh, maintenance, balance-rollover"
    )
