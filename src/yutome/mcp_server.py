"""MCP server exposing yutome's tools and resources over stdio or HTTP.

Tool/resource definitions come from :mod:`yutome.contract`; this module wires
the registry into a FastMCP server.
"""
from __future__ import annotations

import base64
import functools
import inspect
import json
import secrets
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

from yutome import contract, runtime
from yutome.contract import AUTH_SCOPE


SERVER_NAME = "Yutome (My YouTube Library)"
SERVER_WEBSITE_URL = "https://github.com/MaskyS/yutome"
REMOTE_TOKEN_ENV_VAR = "YUTOME_HTTP_TOKEN"
SERVER_INSTRUCTIONS = contract.SERVER_INSTRUCTIONS

# Sizes shipped under src/yutome/assets/yutome-icon-<size>.png. Offered to
# clients via MCP `Implementation.icons` so connector UIs can pick whichever
# size fits. Spec: SEP-973 / schema 2025-11-25.
_ICON_SIZES_PX = (48, 128, 256)


@lru_cache(maxsize=1)
def _server_icons() -> list[Any]:
    from mcp.types import Icon

    icons: list[Icon] = []
    for size in _ICON_SIZES_PX:
        try:
            data = (
                resources.files("yutome")
                .joinpath(f"assets/yutome-icon-{size}.png")
                .read_bytes()
            )
        except (FileNotFoundError, ModuleNotFoundError):
            continue
        encoded = base64.b64encode(data).decode("ascii")
        icons.append(
            Icon(
                src=f"data:image/png;base64,{encoded}",
                mimeType="image/png",
                sizes=[f"{size}x{size}"],
            )
        )
    return icons


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
            scopes=[AUTH_SCOPE],
        )


def configure(config_path: Path) -> runtime.Runtime:
    """Initialise the shared runtime. Call once before serving."""
    return runtime.configure(config_path)


def build_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    streamable_http_path: str = "/mcp",
    auth_token: str | None = None,
    auth_base_url: str | None = None,
) -> Any:
    """Construct the FastMCP server with every tool and resource from the
    contract registry registered."""
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server_kwargs: dict[str, Any] = {
        "name": SERVER_NAME,
        "instructions": SERVER_INSTRUCTIONS,
        "website_url": SERVER_WEBSITE_URL,
        "host": host,
        "port": port,
        "streamable_http_path": streamable_http_path,
    }
    icons = _server_icons()
    if icons:
        server_kwargs["icons"] = icons
    if auth_token:
        from mcp.server.auth.settings import AuthSettings

        base_url = (auth_base_url or _default_remote_base_url(host, port)).rstrip("/")
        server_kwargs["auth"] = AuthSettings(
            issuer_url=base_url,
            resource_server_url=base_url,
            required_scopes=[AUTH_SCOPE],
        )
        server_kwargs["token_verifier"] = _StaticBearerVerifier(auth_token)

    server = FastMCP(**server_kwargs)

    # Register tools from the registry. FastMCP introspects each handler's
    # signature to derive its JSON Schema, so the handler functions in
    # contract.py carry the canonical parameter shape.
    for tool in contract.TOOLS:
        annotations = ToolAnnotations(
            title=tool.title,
            readOnlyHint=tool.read_only,
            openWorldHint=tool.open_world,
        )
        server.tool(
            name=tool.name,
            title=tool.title,
            description=tool.description,
            annotations=annotations,
        )(tool.handler)

    # Register resource templates. Each handler is wrapped to return a JSON
    # string (FastMCP's contract for application/json resources).
    for resource in contract.RESOURCES:
        _register_resource(server, resource)

    return server


def _register_resource(server: Any, resource: contract.ResourceSpec) -> None:
    handler = resource.handler

    @functools.wraps(handler)
    def serializer(**kwargs: Any) -> str:
        return json.dumps(handler(**kwargs), ensure_ascii=False)

    # FastMCP introspects the registered function's signature to match URI
    # template parameters. functools.wraps copies __name__ / __doc__ /
    # __annotations__; we additionally override __signature__ so
    # inspect.signature() returns the handler's parameter list (e.g.
    # ``chunk_id``) instead of ``**kwargs``.
    serializer.__signature__ = inspect.signature(handler)  # type: ignore[attr-defined]

    server.resource(
        uri=resource.uri_template,
        name=resource.name,
        description=resource.description,
        mime_type=resource.mime_type,
    )(serializer)


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
    import os

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
