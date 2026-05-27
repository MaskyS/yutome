from __future__ import annotations

from pathlib import Path

import typer

from . import _legacy
from .context import config_path

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Manage sources and indexing.")


@app.command("add")
def add_command(
    ctx: typer.Context,
    sources: list[str] = typer.Argument(..., metavar="SOURCE", help="YouTube channel/video URL, handle, or id."),
    title: str | None = typer.Option(None, "--title", help="Optional display title for one source."),
    selected: bool = typer.Option(True, "--selected/--unselected", help="Include source in default sync runs."),
    hosted: bool = typer.Option(False, "--hosted", help="Upload sources to hosted Yutome instead of local SQLite."),
) -> None:
    """Add sources to the library."""
    _legacy.add_sources(targets=sources, config=config_path(ctx), title=title, selected=selected, hosted=hosted)


@app.command("import")
def import_command(
    ctx: typer.Context,
    path: Path = typer.Argument(..., exists=True, readable=True, help="CSV, OPML/XML, or plain URL list."),
    selected: bool = typer.Option(True, "--selected/--unselected", help="Include imported sources in default sync runs."),
    hosted: bool = typer.Option(False, "--hosted", help="Upload imported sources to hosted Yutome instead of local SQLite."),
) -> None:
    """Import sources from a file."""
    _legacy.import_command(path=path, config=config_path(ctx), selected=selected, hosted=hosted)


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
    hosted: bool = typer.Option(False, "--hosted", help="Upload imported public sources to hosted Yutome."),
) -> None:
    """Import YouTube subscriptions."""
    _legacy.import_youtube(
        target=target,
        config=config_path(ctx),
        port=port,
        open_browser=open_browser,
        selected=selected,
        hosted=hosted,
    )


@app.command("select")
def select_command(
    ctx: typer.Context,
    selector: str = typer.Argument(..., help="Source id, URL, handle, title, or 'all'."),
    off: bool = typer.Option(False, "--off", help="Exclude matching sources from default sync runs."),
) -> None:
    """Toggle whether matching sources are included in default sync runs."""
    if off:
        _legacy.unselect_source(selector=selector, config=config_path(ctx))
    else:
        _legacy.select_source(selector=selector, config=config_path(ctx))


@app.command("sync")
def sync_command(
    ctx: typer.Context,
    source: str | None = typer.Argument(
        None,
        metavar="SOURCE",
        help="YouTube channel or video URL/id. Omit to sync selected sources.",
    ),
    all_channels: bool = typer.Option(False, "--all", help="Sync every selected source in the local library."),
    limit: int | None = typer.Option(None, "--limit", help="Limit videos discovered per tab."),
    embed: bool | None = typer.Option(None, "--embed/--no-embed", help="Generate Voyage embeddings."),
    force: bool = typer.Option(False, "--force", help="Reprocess videos even when indexed."),
    max_process: int | None = typer.Option(None, "--max-process", min=1, help="Maximum videos to process."),
    workers: int | None = typer.Option(None, "--workers", min=1, max=32, help="Video processing concurrency."),
    retry_failed: bool = typer.Option(False, "--retry-failed", help="Retry failed or deferred videos."),
    use_catalog: bool = typer.Option(False, "--use-catalog", help="Use already-discovered catalog videos."),
    verbose_skips: bool = typer.Option(False, "--verbose-skips/--quiet-skips", help="Print skipped videos."),
    asr_fallback: bool = typer.Option(False, "--asr-fallback", help="Use local ASR when captions fail."),
    gemini_fallback: bool = typer.Option(False, "--gemini-fallback", help="Use Gemini when captions fail."),
    stop_on_rate_limit: bool = typer.Option(
        False,
        "--stop-on-rate-limit/--continue-on-rate-limit",
        help="Stop submitting new videos when a likely rate limit is detected.",
    ),
    sleep_seconds: float = typer.Option(0.0, "--sleep", min=0.0, help="Delay between transcript requests."),
    status_filter: list[str] | None = typer.Option(None, "--status-filter", help="Only process matching statuses."),
    source_filter: list[str] | None = typer.Option(None, "--source-filter", help="Only process matching sources."),
    max_duration_seconds: int | None = typer.Option(
        None,
        "--max-duration-seconds",
        min=1,
        help="Only process videos at or below this duration.",
    ),
    shortest_first: bool = typer.Option(False, "--shortest-first", help="Process shorter candidates first."),
    proxy_retries_when_blocked: int | None = typer.Option(
        None,
        "--proxy-retries-when-blocked",
        min=1,
        help="Override Webshare transcript retries for this run.",
    ),
) -> None:
    """Discover and index sources."""
    _legacy.sync(
        target=source,
        config=config_path(ctx),
        all_channels=all_channels,
        limit=limit,
        embed=embed,
        force=force,
        max_process=max_process,
        workers=workers,
        retry_failed=retry_failed,
        use_catalog=use_catalog,
        verbose_skips=verbose_skips,
        asr_fallback=asr_fallback,
        gemini_fallback=gemini_fallback,
        stop_on_rate_limit=stop_on_rate_limit,
        sleep_seconds=sleep_seconds,
        status_filter=status_filter,
        source_filter=source_filter,
        max_duration_seconds=max_duration_seconds,
        shortest_first=shortest_first,
        proxy_retries_when_blocked=proxy_retries_when_blocked,
    )


@app.command("rebuild")
def rebuild_command(
    ctx: typer.Context,
    target: str = typer.Argument(..., help="vectors, chunks, or all."),
    resume: bool = typer.Option(False, "--resume", help="For vectors: embed only pending chunks."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="For vectors: maximum pending chunks."),
    batch_size: int | None = typer.Option(None, "--batch-size", min=1, help="For vectors: embedding batch size."),
    concurrency: int | None = typer.Option(None, "--concurrency", min=1, help="For vectors: embedding concurrency."),
) -> None:
    """Re-run chunk or vector indexing stages."""
    normalized = target.lower()
    if normalized not in {"vectors", "chunks", "all"}:
        raise typer.BadParameter("target must be one of: vectors, chunks, all")
    if normalized in {"chunks", "all"}:
        _legacy.rebuild_chunks(config=config_path(ctx))
    if normalized in {"vectors", "all"}:
        _legacy.rebuild_vectors(
            config=config_path(ctx),
            resume=resume,
            limit=limit,
            batch_size=batch_size,
            concurrency=concurrency,
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
    _legacy.quality_upgrade(
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
