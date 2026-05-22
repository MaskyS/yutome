"""Serialize the yutome contract registry to JSON.

The TypeScript Worker (``cloudflare/yutome-capsule``) imports the emitted
``contract.json`` at build time and registers tools and resource templates
with ``McpAgent`` from the same single source of truth used by the local
Python adapters. Run via ``yutome contract emit``.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from yutome import contract


def build_contract_payload() -> dict[str, Any]:
    """Return the JSON-serializable dict of tools, resources, and scope.

    Tool input schemas are derived from FastMCP's introspection of the
    handler signatures declared in ``yutome.contract``. This guarantees the
    schema seen by the TS Worker matches the schema the local stdio MCP
    server advertises.
    """
    from yutome.mcp_server import build_server

    server = build_server()

    async def collect_tools() -> list[dict[str, Any]]:
        tools = await server.list_tools()
        result: list[dict[str, Any]] = []
        for tool, spec in zip(tools, contract.TOOLS, strict=True):
            annotations = {
                "title": spec.title,
                "readOnlyHint": spec.read_only,
                "openWorldHint": spec.open_world,
            }
            result.append(
                {
                    "name": spec.name,
                    "title": spec.title,
                    "description": spec.description,
                    "inputSchema": tool.inputSchema,
                    "annotations": annotations,
                }
            )
        return result

    tools_payload = asyncio.run(collect_tools())

    resource_templates_payload = [
        {
            "uriTemplate": spec.uri_template,
            "name": spec.name,
            "description": spec.description,
            "mimeType": spec.mime_type,
            "host": spec.host,
        }
        for spec in contract.RESOURCES
    ]

    return {
        "$schema_version": 1,
        "auth_scope": contract.AUTH_SCOPE,
        "tools": tools_payload,
        "resource_templates": resource_templates_payload,
    }


def emit_contract_json(output_path: Path) -> Path:
    """Write the contract payload to ``output_path``. Returns the path."""
    payload = build_contract_payload()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return output_path


DEFAULT_CONTRACT_OUTPUT = Path("cloudflare/yutome-capsule/src/contract.json")
