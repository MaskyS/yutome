from __future__ import annotations

from pathlib import Path

import typer

from yutome import search_presets
from yutome.api import find as api_find
from yutome.api import list_ as api_list
from yutome.api import q as api_q
from yutome.api import show as api_show
from yutome.query import QueryRequest

from . import actions
from .context import get_context
from .render import echo_json, render_query_result

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Search and read the indexed corpus.")


@app.command("find")
def find_command(
    ctx: typer.Context,
    text: str = typer.Argument(..., help="Search text."),
    mode: str | None = typer.Option(None, "--mode", help="Search mode: lexical, semantic, hybrid, or none."),
    channel: str | None = typer.Option(None, "--channel", help="Filter by channel id or handle."),
    since: str | None = typer.Option(None, "--since", help="Filter videos published on/after this date string."),
    until: str | None = typer.Option(None, "--until", help="Filter videos published on/before this date string."),
    source: str | None = typer.Option(None, "--source", help="Filter active transcript source prefix."),
    language: str | None = typer.Option(None, "--language", help="Filter active transcript language."),
    group_by: str | None = typer.Option(None, "--group-by", help="Group ranked chunk hits by video."),
    limit: int = typer.Option(
        search_presets.FIND_LIMIT_DEFAULT,
        "--limit",
        min=search_presets.LIMIT_MIN,
        max=search_presets.LIMIT_MAX,
        help="Maximum rows to return.",
    ),
    offset: int = typer.Option(search_presets.OFFSET_MIN, "--offset", min=search_presets.OFFSET_MIN, help="Rows to skip."),
    project: str | None = typer.Option(None, "--project", help="Projection name."),
    json_output: bool = typer.Option(False, "--json", help="Emit the full QueryResult envelope."),
) -> None:
    """Rank transcript chunks by relevance."""
    runtime = get_context(ctx).runtime()
    try:
        result = api_find(
            config=runtime.config,
            paths=runtime.paths,
            text=text,
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
    render_query_result(result, json_output=json_output)


@app.command("list")
def list_command(
    ctx: typer.Context,
    entity: str = typer.Argument(..., help="videos, channels, or status."),
    channel: str | None = typer.Option(None, "--channel", help="Filter by channel id or handle."),
    since: str | None = typer.Option(None, "--since", help="Filter videos published on/after this date string."),
    until: str | None = typer.Option(None, "--until", help="Filter videos published on/before this date string."),
    status: str | None = typer.Option(None, "--status", help="Filter ingest status. Suffix with * for prefix match."),
    source: str | None = typer.Option(None, "--source", help="Filter active transcript source prefix."),
    language: str | None = typer.Option(None, "--language", help="Filter active transcript language."),
    selected: bool | None = typer.Option(None, "--selected/--any-selection", help="Only selected library channels."),
    order_by: str | None = typer.Option(None, "--order-by", help="Sort field, optionally field:asc."),
    limit: int = typer.Option(
        search_presets.LIST_LIMIT_DEFAULT,
        "--limit",
        min=search_presets.LIMIT_MIN,
        max=search_presets.LIMIT_MAX,
        help="Maximum rows to return.",
    ),
    offset: int = typer.Option(search_presets.OFFSET_MIN, "--offset", min=search_presets.OFFSET_MIN, help="Rows to skip."),
    project: str | None = typer.Option(None, "--project", help="Projection name."),
    json_output: bool = typer.Option(False, "--json", help="Emit the full QueryResult envelope."),
) -> None:
    """Enumerate corpus objects."""
    normalized = entity.lower()
    if normalized not in search_presets.LIST_ENTITIES:
        raise typer.BadParameter("entity must be one of: videos, channels, status")
    runtime = get_context(ctx).runtime()
    result = api_list(
        config=runtime.config,
        paths=runtime.paths,
        entity=normalized,  # type: ignore[arg-type]
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
    render_query_result(result, json_output=json_output)


@app.command("show")
def show_command(
    ctx: typer.Context,
    kind: str = typer.Argument(..., help="chunk, video, channel, transcript, context, or source."),
    id_: str | None = typer.Argument(None, metavar="ID", help="Resource id, selector, or timestamped URL."),
    token_budget: int = typer.Option(
        search_presets.TOKEN_BUDGET_DEFAULT,
        "--token-budget",
        min=search_presets.TOKEN_BUDGET_MIN,
        max=search_presets.TOKEN_BUDGET_MAX,
        help="Context token budget.",
    ),
    video_id: str | None = typer.Option(None, "--video-id", help="Video id for timestamp lookup."),
    time_seconds: int | None = typer.Option(None, "--time", min=0, help="Timestamp in seconds for video lookup."),
    youtube_url: str | None = typer.Option(None, "--youtube-url", help="Timestamped YouTube URL."),
    transcript_offset: int = typer.Option(
        search_presets.OFFSET_MIN,
        "--offset",
        min=search_presets.OFFSET_MIN,
        help="Segment offset for long transcript paging.",
    ),
    transcript_limit: int | None = typer.Option(
        None,
        "--limit",
        min=search_presets.TRANSCRIPT_LIMIT_MIN,
        max=search_presets.TRANSCRIPT_LIMIT_MAX,
        help="Maximum transcript segments.",
    ),
) -> None:
    """Fetch resources or resolve citations."""
    normalized = kind.lower()
    if normalized not in search_presets.SHOW_KINDS:
        raise typer.BadParameter("kind must be one of: chunk, video, channel, transcript, context, source")
    runtime = get_context(ctx).runtime()
    try:
        payload = api_show(
            config=runtime.config,
            paths=runtime.paths,
            kind=normalized,  # type: ignore[arg-type]
            id_=id_,
            token_budget=token_budget,
            video_id=video_id,
            time_seconds=time_seconds,
            youtube_url=youtube_url,
            transcript_offset=transcript_offset,
            transcript_limit=transcript_limit,
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    echo_json(payload)


@app.command("q")
def q_command(
    ctx: typer.Context,
    request: str | None = typer.Argument(None, help="JSON QueryRequest, or '-' to read from stdin."),
    file: Path | None = typer.Option(None, "--file", "-f", exists=True, readable=True, help="Read QueryRequest JSON."),
) -> None:
    """Execute a raw QueryRequest JSON object."""
    runtime = get_context(ctx).runtime()
    payload = actions._read_query_request(request, file)
    try:
        result = api_q(config=runtime.config, paths=runtime.paths, request=QueryRequest.model_validate(payload))
    except (RuntimeError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    echo_json(result)
