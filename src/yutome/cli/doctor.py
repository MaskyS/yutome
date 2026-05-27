from __future__ import annotations

from pathlib import Path

import typer

from yutome.contract_export import build_contract_payload

from . import _legacy
from .context import config_path
from .render import echo_json

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Diagnose local, remote, and hosted capabilities.")


@app.command("local")
def local_command(ctx: typer.Context) -> None:
    """Check local project readiness."""
    _legacy.doctor(config=config_path(ctx))


@app.command("proxy")
def proxy_command(
    ctx: typer.Context,
    video_id: str = typer.Option("lwH29W1M57A", "--video-id", help="Video ID to test against."),
    info_only: bool = typer.Option(False, "--info-only", help="Only print proxy guidance."),
    transcript_api: bool = typer.Option(True, "--transcript-api/--no-transcript-api", help="Test transcript API."),
    ytdlp_subtitles: bool = typer.Option(True, "--yt-dlp/--no-yt-dlp", help="Test yt-dlp subtitle fetching."),
) -> None:
    """Show proxy guidance and optionally test transcript fetch paths."""
    _legacy.proxy_info()
    if info_only:
        return
    _legacy.proxy_test(
        video_id=video_id,
        config=config_path(ctx),
        transcript_api=transcript_api,
        ytdlp_subtitles=ytdlp_subtitles,
    )


@app.command("gemini")
def gemini_command(
    ctx: typer.Context,
    video_id: str = typer.Option("lwH29W1M57A", "--video-id", help="Video ID to test against."),
) -> None:
    """Test Gemini YouTube URL transcript fallback."""
    _legacy.gemini_test(video_id=video_id, config=config_path(ctx))


@app.command("eval")
def eval_command(
    ctx: typer.Context,
    suite: Path = typer.Argument(..., exists=True, readable=True, help="JSON eval suite file."),
    json_output: bool = typer.Option(False, "--json", help="Emit full machine-readable eval results."),
) -> None:
    """Run local retrieval evals."""
    _legacy.eval_run(suite=suite, config=config_path(ctx), json_output=json_output)


@app.command("contract")
def contract_command(
    output: Path = typer.Option(
        Path("cloudflare/yutome-capsule/src/contract.json"),
        "--output",
        "-o",
        help="Committed contract JSON path to check.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit check result as JSON."),
) -> None:
    """Check that committed contract JSON matches the Python registry."""
    expected = build_contract_payload()
    if not output.exists():
        if json_output:
            echo_json({"ok": False, "error": f"contract file not found: {output}"})
        else:
            typer.echo(f"contract file not found: {output}", err=True)
        raise typer.Exit(code=1)
    import json

    actual = json.loads(output.read_text(encoding="utf-8"))
    ok = actual == expected
    if json_output:
        echo_json({"ok": ok, "path": str(output)})
    elif ok:
        typer.echo(f"[OK] Contract JSON is current: {output}")
    else:
        typer.echo(f"Contract JSON is stale: {output}", err=True)
    if not ok:
        raise typer.Exit(code=1)


@app.command("remote")
def remote_command(
    ctx: typer.Context,
    base_url: str = typer.Argument(..., help="Base URL, e.g. https://yutome.example.com."),
    token: str | None = typer.Option(None, "--token", help="Bearer token."),
    timeout: float = typer.Option(10.0, "--timeout", min=1.0, help="Request timeout in seconds."),
) -> None:
    """Check a remote yutome HTTP API."""
    _legacy.remote_check(base_url=base_url, config=config_path(ctx), token=token, timeout=timeout)


@app.command("hosted-db")
def hosted_db_command(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Emit database check as JSON."),
) -> None:
    """Check hosted Postgres configuration and required extensions."""
    _legacy.hosted_db_check(config=config_path(ctx), json_output=json_output)
