"""Yutome command-line interface.

The public CLI is a small set of job-oriented namespaces. The legacy module is
kept as private plumbing while handlers are moved behind the new surface.
"""

from __future__ import annotations

from pathlib import Path

import typer

from yutome.config import DEFAULT_CONFIG_FILENAME

from . import _legacy
from . import corpus as corpus_cli
from . import doctor as doctor_cli
from . import export as export_cli
from . import hosted as hosted_cli
from . import search as search_cli
from . import serve as serve_cli
from .context import config_path, install_context

app = typer.Typer(
    help="Index and search a local YouTube transcript library.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)

app.add_typer(search_cli.app, name="search")
app.add_typer(corpus_cli.app, name="corpus")
app.add_typer(serve_cli.app, name="serve")
app.add_typer(hosted_cli.app, name="hosted")
app.add_typer(doctor_cli.app, name="doctor")
app.add_typer(export_cli.app, name="export")


@app.callback()
def root(
    ctx: typer.Context,
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to the yutome TOML config.",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        callback=_legacy._version_callback,
        is_eager=True,
        help="Show the installed yutome version and exit.",
    ),
) -> None:
    """Index and search a local YouTube transcript library."""
    install_context(ctx, config_path=config)


@app.command()
def setup(
    ctx: typer.Context,
    source: str | None = typer.Argument(
        None,
        help="Optional channel or video URL/id to add during setup.",
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
    _legacy.setup(channel=source, config=config_path(ctx), yes=yes, hosted=hosted)


@app.command("connect")
def connect_command(
    ctx: typer.Context,
    endpoint: str | None = typer.Option(
        None,
        "--endpoint",
        help="Cloudflare Worker endpoint URL. Pass either the base URL or the full /mcp URL.",
    ),
    deploy: bool = typer.Option(
        False,
        "--deploy",
        help="Deploy the tracked Cloudflare Worker with Wrangler through npx.",
    ),
    open_cloudflare: bool = typer.Option(
        False,
        "--open-cloudflare",
        help="Open the Cloudflare Workers dashboard after preparing the Worker project.",
    ),
    worker_name: str | None = typer.Option(
        None,
        "--worker-name",
        help="Cloudflare Worker name for generated deployments or later cleanup.",
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
        help="Remote mode: connector-only or replica.",
    ),
) -> None:
    """Set up remote access for assistant apps."""
    _legacy.connect_command(
        config=config_path(ctx),
        endpoint=endpoint,
        deploy=deploy,
        open_cloudflare=open_cloudflare,
        worker_name=worker_name,
        relay_token=relay_token,
        pairing_code=pairing_code,
        assistant_app=assistant_app,
        mode=mode,
    )


@app.command("disconnect")
def disconnect_command(
    ctx: typer.Context,
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
    _legacy.disconnect_command(
        config=config_path(ctx),
        worker_name=worker_name,
        remove_cloudflare=remove_cloudflare,
        keep_state=keep_state,
        dry_run=dry_run,
        yes=yes,
    )


@app.command("status")
def status_command(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON."),
) -> None:
    """Print local library status."""
    _legacy.status_command(config=config_path(ctx), json_output=json_output)


def __getattr__(name: str) -> object:
    """Expose legacy helpers for imports while the CLI internals are split."""
    return getattr(_legacy, name)


__all__ = ["app"]
