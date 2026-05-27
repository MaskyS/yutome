from __future__ import annotations


import typer

from . import _bridge
from . import actions
from .context import config_path

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Run local and remote service adapters.")
bridge_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Manage the laptop bridge process.")
remote_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Authenticated remote HTTP/MCP surfaces.")
app.add_typer(bridge_app, name="bridge")
app.add_typer(remote_app, name="remote")


@app.command("mcp")
def mcp_command(ctx: typer.Context) -> None:
    """Run the local stdio MCP server."""
    actions.mcp_serve(config=config_path(ctx))


@app.command("http")
def http_command(
    ctx: typer.Context,
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(8765, "--port", help="Bind port."),
    cors_origin: list[str] | None = typer.Option(None, "--cors-origin", help="Allowed browser origin."),
    allow_unauthenticated_remote: bool = typer.Option(
        False,
        "--allow-unauthenticated-remote",
        help="Permit non-loopback HTTP binding without YUTOME_HTTP_TOKEN.",
    ),
) -> None:
    """Run the local HTTP API."""
    actions.http_serve(
        config=config_path(ctx),
        host=host,
        port=port,
        cors_origin=cors_origin,
        allow_unauthenticated_remote=allow_unauthenticated_remote,
    )


@bridge_app.command("start")
def bridge_start(
    ctx: typer.Context,
    foreground: bool = typer.Option(False, "--foreground", help="Run in foreground."),
) -> None:
    """Start the bridge."""
    _bridge.bridge_start_command(config=config_path(ctx), foreground=foreground)


@bridge_app.command("stop")
def bridge_stop(ctx: typer.Context) -> None:
    """Stop the bridge."""
    _bridge.bridge_stop_command(config=config_path(ctx))


@bridge_app.command("status")
def bridge_status(ctx: typer.Context) -> None:
    """Show bridge process status."""
    _bridge.bridge_status_command(config=config_path(ctx))


@bridge_app.command("install")
def bridge_install(ctx: typer.Context) -> None:
    """Install bridge auto-start."""
    _bridge.bridge_install_command(config=config_path(ctx))


@bridge_app.command("uninstall")
def bridge_uninstall() -> None:
    """Remove bridge auto-start."""
    _bridge.bridge_uninstall_command()


@remote_app.command("prepare")
def remote_prepare(
    ctx: typer.Context,
    rotate: bool = typer.Option(False, "--rotate", help="Replace an existing YUTOME_HTTP_TOKEN."),
    show_token: bool = typer.Option(False, "--show-token", help="Print the token once after writing it."),
) -> None:
    """Prepare authenticated remote/API access."""
    actions.remote_prepare(config=config_path(ctx), rotate=rotate, show_token=show_token)


@remote_app.command("http")
def remote_http(
    ctx: typer.Context,
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address for authenticated remote access."),
    port: int = typer.Option(8765, "--port", help="Bind port."),
    cors_origin: list[str] | None = typer.Option(None, "--cors-origin", help="Allowed browser origin."),
) -> None:
    """Run authenticated HTTP API for remote clients."""
    actions.remote_serve(config=config_path(ctx), host=host, port=port, cors_origin=cors_origin)


@remote_app.command("mcp")
def remote_mcp(
    ctx: typer.Context,
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address for authenticated remote MCP."),
    port: int = typer.Option(8766, "--port", help="Bind port."),
    path: str = typer.Option("/mcp", "--path", help="MCP streamable HTTP path."),
    server_url: str | None = typer.Option(None, "--server-url", help="External base URL for MCP auth metadata."),
) -> None:
    """Run authenticated MCP over streamable HTTP."""
    actions.remote_mcp(config=config_path(ctx), host=host, port=port, path=path, server_url=server_url)
