from __future__ import annotations

from pathlib import Path

import typer

from . import actions
from .context import config_path

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Manage sources and indexing.")


@app.command("add")
def add_command(
    ctx: typer.Context,
    sources: list[str] = typer.Argument(..., metavar="SOURCE", help="YouTube channel/video URL, handle, or id."),
    title: str | None = typer.Option(None, "--title", help="Optional display title for one source."),
    selected: bool = typer.Option(True, "--selected/--unselected", help="Include source in default sync runs."),
) -> None:
    """Add sources to the corpus."""
    actions.add_sources(targets=sources, config=config_path(ctx), title=title, selected=selected)


@app.command("import")
def import_command(
    ctx: typer.Context,
    path: Path = typer.Argument(..., exists=True, readable=True, help="CSV, OPML/XML, or plain URL list."),
    selected: bool = typer.Option(True, "--selected/--unselected", help="Include imported sources in default sync runs."),
) -> None:
    """Import sources from a file."""
    actions.import_command(path=path, config=config_path(ctx), selected=selected)


@app.command("import-youtube")
def import_youtube_command(
    ctx: typer.Context,
    target: str | None = typer.Argument(
        None,
        help="Optional channel URL, handle, or channel id. Omit to import signed-in subscriptions.",
    ),
    port: int = typer.Option(0, "--port", min=0, max=65535, help="Local OAuth callback port."),
    open_browser: bool = typer.Option(True, "--open-browser/--print-url", help="Open the OAuth URL in a browser."),
    selected: bool = typer.Option(True, "--selected/--unselected", help="Include imported channels in sync runs."),
) -> None:
    """Import YouTube subscriptions."""
    actions.import_youtube(
        target=target,
        config=config_path(ctx),
        port=port,
        open_browser=open_browser,
        selected=selected,
    )


@app.command("select")
def select_command(
    ctx: typer.Context,
    selector: str = typer.Argument(..., help="Source id, URL, handle, title, or 'all'."),
    off: bool = typer.Option(False, "--off", help="Exclude matching sources from default sync runs."),
) -> None:
    """Toggle whether matching sources are included in default sync runs."""
    if off:
        actions.unselect_source(selector=selector, config=config_path(ctx))
    else:
        actions.select_source(selector=selector, config=config_path(ctx))


@app.command("sync")
def sync_command(
    ctx: typer.Context,
    source: str | None = typer.Argument(
        None,
        metavar="SOURCE",
        help="YouTube channel or video URL/id. Omit to sync selected sources.",
    ),
    limit: int | None = typer.Option(None, "--limit", help="Limit source refresh rows."),
    max_process: int | None = typer.Option(None, "--max-process", min=1, help="Maximum videos to process."),
) -> None:
    """Discover and index sources."""
    actions.sync(
        target=source,
        config=config_path(ctx),
        limit=limit,
        max_process=max_process,
    )


@app.command("rebuild")
def rebuild_command(
    ctx: typer.Context,
    target: str = typer.Argument(..., help="vectors, chunks, or all."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="For vectors: maximum indexing jobs."),
) -> None:
    """Re-run chunk or vector indexing stages."""
    normalized = target.lower()
    if normalized not in {"vectors", "chunks", "all"}:
        raise typer.BadParameter("target must be one of: vectors, chunks, all")
    if normalized in {"chunks", "all"}:
        actions.rebuild_chunks(config=config_path(ctx))
    if normalized in {"vectors", "all"}:
        actions.rebuild_vectors(
            config=config_path(ctx),
            limit=limit,
        )


@app.command("quality")
def quality_command(
    ctx: typer.Context,
    video_id: str | None = typer.Option(None, "--video-id", help="Upgrade one video id."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Maximum active transcripts to upgrade."),
    video_workers: int | None = typer.Option(None, "--video-workers", min=1, help="Parallel videos to clean."),
    batch_segments: int | None = typer.Option(None, "--batch-segments", min=1, help="Segments per LLM request."),
    concurrency: int | None = typer.Option(None, "--concurrency", min=1, help="Parallel LLM cleanup requests."),
    max_patch_retries: int | None = typer.Option(
        None,
        "--max-patch-retries",
        min=0,
        max=5,
        help="Retry invalid LLM correction patches.",
    ),
    source_filter: list[str] | None = typer.Option(None, "--source-filter", help="Only upgrade matching sources."),
    all_transcripts: bool = typer.Option(False, "--all", help="Upgrade all matching transcripts."),
    rebuild_vectors: bool = typer.Option(False, "--rebuild-vectors", help="Rebuild vectors after text changes."),
) -> None:
    """Create LLM-cleaned transcript versions."""
    actions.quality_upgrade(
        config=config_path(ctx),
        video_id=video_id,
        limit=limit,
        video_workers=video_workers,
        batch_segments=batch_segments,
        concurrency=concurrency,
        max_patch_retries=max_patch_retries,
        source_filter=source_filter,
        all_transcripts=all_transcripts,
        rebuild_vectors=rebuild_vectors,
    )
