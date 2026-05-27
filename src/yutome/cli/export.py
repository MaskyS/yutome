from __future__ import annotations

import typer

from . import _legacy
from .context import config_path

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Export indexed artifacts.")


@app.command("markdown")
def markdown_command(ctx: typer.Context) -> None:
    """Export indexed videos to portable Markdown."""
    _legacy.export_portable_markdown(config=config_path(ctx))


@app.command("obsidian")
def obsidian_command(ctx: typer.Context) -> None:
    """Export indexed videos to Obsidian-friendly Markdown."""
    _legacy.export_obsidian(config=config_path(ctx))
