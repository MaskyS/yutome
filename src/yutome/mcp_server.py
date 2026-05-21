"""Local MCP server exposing yutome query verbs over stdio."""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any, Literal

from yutome.api import (
    find as api_find,
    list_ as api_list,
    q as api_q,
    resource_channel as api_resource_channel,
    resource_chunk as api_resource_chunk,
    resource_transcript as api_resource_transcript,
    resource_video as api_resource_video,
    show as api_show,
)
from yutome.config import DEFAULT_CONFIG_FILENAME, AppConfig, load_config
from yutome.env import apply_env_to_config, load_dotenv
from yutome.paths import ProjectPaths


SERVER_NAME = "yutome"
REMOTE_TOKEN_ENV_VAR = "YUTOME_HTTP_TOKEN"
REMOTE_READ_SCOPE = "yutome:read"
SERVER_INSTRUCTIONS = (
    "yutome is a local-first YouTube channel knowledge base. Use `find` for ranked "
    "relevance, `list` for enumeration by filter, `show` for resource-by-id or "
    "citation/context expansion, and `q` for the raw QueryRequest primitive. "
    "Use show(kind='source') for citation URL/provenance only; use "
    "show(kind='context') for neighboring transcript text within a token budget."
)

_RUNTIME: "Runtime | None" = None


class _StaticBearerVerifier:
    """Validate the shared remote bearer token for streamable HTTP MCP."""

    def __init__(self, expected_token: str) -> None:
        self.expected_token = expected_token

    async def verify_token(self, token: str) -> Any:
        from mcp.server.auth.provider import AccessToken

        if not secrets.compare_digest(token, self.expected_token):
            return None
        return AccessToken(
            token=token,
            client_id="yutome-remote",
            scopes=[REMOTE_READ_SCOPE],
        )


class Runtime:
    """Cached config + paths so each tool call does not re-parse TOML."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        project_root = (
            config_path.parent if config_path.is_absolute() else (Path.cwd() / config_path).parent
        )
        load_dotenv(project_root / ".env")
        self.config: AppConfig = apply_env_to_config(load_config(config_path))
        self.paths: ProjectPaths = ProjectPaths.from_config(self.config, project_root=project_root)


def configure(config_path: Path) -> Runtime:
    """Initialise runtime state. Call once at server startup, before serving."""
    global _RUNTIME
    _RUNTIME = Runtime(config_path)
    return _RUNTIME


def _runtime() -> Runtime:
    if _RUNTIME is None:
        env_root = os.environ.get("CLAUDE_PROJECT_DIR")
        if env_root:
            candidate = Path(env_root) / DEFAULT_CONFIG_FILENAME
            if candidate.exists():
                return configure(candidate)
        return configure(Path(DEFAULT_CONFIG_FILENAME))
    return _RUNTIME


def tool_find(
    text: str,
    in_: Literal["chunks", "titles", "descriptions"] = "chunks",
    mode: Literal["lexical", "semantic", "hybrid", "none"] | None = None,
    channel: str | None = None,
    since: str | None = None,
    until: str | None = None,
    source: str | None = None,
    language: str | None = None,
    group_by: Literal["video", "channel", "transcript_source"] | None = None,
    limit: int = 10,
    offset: int = 0,
    project: str | None = None,
) -> dict[str, Any]:
    runtime = _runtime()
    return api_find(
        config=runtime.config,
        paths=runtime.paths,
        text=text,
        in_=in_,
        mode=mode,
        channel=channel,
        since=since,
        until=until,
        source=source,
        language=language,
        group_by=group_by,
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
        project=project,
    ).model_dump()


def tool_list(
    entity: Literal["video", "videos", "channel", "channels", "attention", "status"],
    channel: str | None = None,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    source: str | None = None,
    language: str | None = None,
    selected: bool | None = None,
    order_by: str | None = None,
    limit: int = 20,
    offset: int = 0,
    project: str | None = None,
) -> dict[str, Any]:
    runtime = _runtime()
    return api_list(
        config=runtime.config,
        paths=runtime.paths,
        entity=entity,
        channel=channel,
        since=since,
        until=until,
        status=status,
        source=source,
        language=language,
        selected=selected,
        order_by=order_by,
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
        project=project,
    ).model_dump()


def tool_show(
    kind: Literal["chunk", "video", "channel", "transcript", "context", "source"],
    id_: str | None = None,
    token_budget: int = 3000,
    video_id: str | None = None,
    time_seconds: int | None = None,
    youtube_url: str | None = None,
) -> dict[str, Any]:
    runtime = _runtime()
    return api_show(
        config=runtime.config,
        paths=runtime.paths,
        kind=kind,
        id_=id_,
        token_budget=max(200, min(token_budget, 8000)),
        video_id=video_id,
        time_seconds=time_seconds,
        youtube_url=youtube_url,
    )


def tool_q(request: dict[str, Any]) -> dict[str, Any]:
    runtime = _runtime()
    return api_q(config=runtime.config, paths=runtime.paths, request=request).model_dump()


def resource_chunk(chunk_id: str) -> dict[str, Any]:
    runtime = _runtime()
    return api_resource_chunk(config=runtime.config, paths=runtime.paths, chunk_id=chunk_id)


def resource_video(video_id: str) -> dict[str, Any]:
    runtime = _runtime()
    return api_resource_video(config=runtime.config, paths=runtime.paths, video_id=video_id)


def resource_channel(channel_id: str) -> dict[str, Any]:
    runtime = _runtime()
    return api_resource_channel(config=runtime.config, paths=runtime.paths, selector=channel_id)


def resource_transcript(transcript_version_id: str) -> dict[str, Any]:
    runtime = _runtime()
    return api_resource_transcript(
        config=runtime.config,
        paths=runtime.paths,
        transcript_version_id=transcript_version_id,
    )


def build_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    streamable_http_path: str = "/mcp",
    auth_token: str | None = None,
    auth_base_url: str | None = None,
) -> Any:
    """Construct the FastMCP server with tools and resources registered."""
    from mcp.server.fastmcp import FastMCP

    server_kwargs: dict[str, Any] = {
        "name": SERVER_NAME,
        "instructions": SERVER_INSTRUCTIONS,
        "host": host,
        "port": port,
        "streamable_http_path": streamable_http_path,
    }
    if auth_token:
        from mcp.server.auth.settings import AuthSettings

        base_url = (auth_base_url or _default_remote_base_url(host, port)).rstrip("/")
        server_kwargs["auth"] = AuthSettings(
            issuer_url=base_url,
            resource_server_url=base_url,
            required_scopes=[REMOTE_READ_SCOPE],
        )
        server_kwargs["token_verifier"] = _StaticBearerVerifier(auth_token)

    server = FastMCP(**server_kwargs)

    @server.tool(
        name="find",
        description=(
            "Ranked relevance search. Use for 'find passages/videos about X'. "
            "`in_='chunks'` searches transcript chunks; `titles` and `descriptions` "
            "search video metadata lexically."
        ),
    )
    def find(  # noqa: D401
        text: str,
        in_: Literal["chunks", "titles", "descriptions"] = "chunks",
        mode: Literal["lexical", "semantic", "hybrid", "none"] | None = None,
        channel: str | None = None,
        since: str | None = None,
        until: str | None = None,
        source: str | None = None,
        language: str | None = None,
        group_by: Literal["video", "channel", "transcript_source"] | None = None,
        limit: int = 10,
        offset: int = 0,
        project: str | None = None,
    ) -> dict[str, Any]:
        return tool_find(
            text=text,
            in_=in_,
            mode=mode,
            channel=channel,
            since=since,
            until=until,
            source=source,
            language=language,
            group_by=group_by,
            limit=limit,
            offset=offset,
            project=project,
        )

    @server.tool(
        name="list",
        description="Enumeration by filter: videos, channels, attention, or status.",
    )
    def list_tool(  # noqa: D401
        entity: Literal["video", "videos", "channel", "channels", "attention", "status"],
        channel: str | None = None,
        since: str | None = None,
        until: str | None = None,
        status: str | None = None,
        source: str | None = None,
        language: str | None = None,
        selected: bool | None = None,
        order_by: str | None = None,
        limit: int = 20,
        offset: int = 0,
        project: str | None = None,
    ) -> dict[str, Any]:
        return tool_list(
            entity=entity,
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

    @server.tool(
        name="show",
        description=(
            "Fetch by id or expand a citation. Kinds: chunk, video, channel, "
            "transcript, context, source."
        ),
    )
    def show(  # noqa: D401
        kind: Literal["chunk", "video", "channel", "transcript", "context", "source"],
        id_: str | None = None,
        token_budget: int = 3000,
        video_id: str | None = None,
        time_seconds: int | None = None,
        youtube_url: str | None = None,
    ) -> dict[str, Any]:
        return tool_show(
            kind=kind,
            id_=id_,
            token_budget=token_budget,
            video_id=video_id,
            time_seconds=time_seconds,
            youtube_url=youtube_url,
        )

    @server.tool(name="q", description="Raw QueryRequest primitive for advanced queries.")
    def raw_query(request: dict[str, Any]) -> dict[str, Any]:  # noqa: D401
        return tool_q(request)

    @server.resource(
        uri="yutome://chunk/{chunk_id}",
        name="yutome_chunk",
        description="Full chunk text and provenance for a transcript chunk.",
        mime_type="application/json",
    )
    def chunk_resource(chunk_id: str) -> str:  # noqa: D401
        return json.dumps(resource_chunk(chunk_id), ensure_ascii=False)

    @server.resource(
        uri="yutome://video/{video_id}",
        name="yutome_video",
        description="Video metadata and active transcript provenance.",
        mime_type="application/json",
    )
    def video_resource(video_id: str) -> str:  # noqa: D401
        return json.dumps(resource_video(video_id), ensure_ascii=False)

    @server.resource(
        uri="yutome://channel/{channel_id}",
        name="yutome_channel",
        description="Channel metadata and local library status.",
        mime_type="application/json",
    )
    def channel_resource(channel_id: str) -> str:  # noqa: D401
        return json.dumps(resource_channel(channel_id), ensure_ascii=False)

    @server.resource(
        uri="yutome://transcript/{transcript_version_id}",
        name="yutome_transcript",
        description="Transcript provenance plus plain text (capped at 200k chars).",
        mime_type="application/json",
    )
    def transcript_resource(transcript_version_id: str) -> str:  # noqa: D401
        return json.dumps(resource_transcript(transcript_version_id), ensure_ascii=False)

    return server


def run_stdio_server(config_path: Path) -> None:
    """Configure runtime and run the FastMCP server over stdio."""
    configure(config_path)
    server = build_server()
    server.run()


def run_streamable_http_server(
    config_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8766,
    path: str = "/mcp",
    require_token_for_non_loopback: bool = True,
    server_url: str | None = None,
) -> None:
    """Configure runtime and run the MCP server over streamable HTTP."""
    configure(config_path)
    token = os.environ.get(REMOTE_TOKEN_ENV_VAR)
    if require_token_for_non_loopback and not _is_loopback_host(host) and not token:
        raise RuntimeError(
            f"{REMOTE_TOKEN_ENV_VAR} is required when binding remote MCP to non-loopback host {host!r}"
        )
    server = build_server(
        host=host,
        port=port,
        streamable_http_path=path,
        auth_token=token,
        auth_base_url=server_url,
    )
    server.run(transport="streamable-http")


def _default_remote_base_url(host: str, port: int) -> str:
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}"


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    return normalized in {"127.0.0.1", "localhost", "::1"} or normalized.startswith("127.")
