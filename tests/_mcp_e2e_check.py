"""End-to-end MCP smoke test.

Launches `uv run yutome mcp serve --config yutome.toml` as a subprocess and drives
it through the MCP Python client over stdio. Not part of the pytest run by
default (subprocess + live LanceDB makes it slow and environment-dependent).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    params = StdioServerParameters(
        command="uv",
        args=["run", "yutome", "mcp", "serve", "--config", "yutome.toml"],
        cwd=str(repo_root),
        env=os.environ.copy(),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("SERVER:", init.serverInfo.name, init.serverInfo.version)

            tools = await session.list_tools()
            tool_names = sorted(t.name for t in tools.tools)
            print("TOOLS:", tool_names)
            expected_tools = {"find", "list", "show", "q"}
            assert expected_tools <= set(tool_names), f"missing tools: {expected_tools - set(tool_names)}"

            templates = await session.list_resource_templates()
            template_uris = sorted(t.uriTemplate for t in templates.resourceTemplates)
            print("RESOURCE TEMPLATES:", template_uris)
            expected_uris = {
                "yutome://chunk/{chunk_id}",
                "yutome://video/{video_id}",
                "yutome://channel/{channel_id}",
                "yutome://transcript/{transcript_version_id}",
            }
            assert expected_uris <= set(template_uris), f"missing resource templates: {expected_uris - set(template_uris)}"

            status = await session.call_tool("list", {"entity": "status"})
            status_result = status.structuredContent or json.loads(status.content[0].text)
            payload = status_result["rows"][0]
            print(
                "CORPUS STATUS:",
                json.dumps(
                    {k: payload[k] for k in ("searchable_now", "videos", "chunks")},
                    indent=2,
                ),
            )
            assert payload["videos"] > 0, "expected indexed videos in live corpus"

            hits = await session.call_tool(
                "find",
                {"text": "Crohn probiotics", "mode": "lexical", "limit": 3},
            )
            hit_result = hits.structuredContent or json.loads(hits.content[0].text)
            hit_payload = hit_result["rows"]
            assert isinstance(hit_payload, list) and hit_payload, (
                f"lexical search returned no hits; structured={hits.structuredContent!r} "
                f"content={[(c.type, getattr(c, 'text', '')[:120]) for c in hits.content]}"
            )
            first = hit_payload[0]
            print(
                "FIRST HIT:",
                json.dumps(
                    {
                        "chunk_id": first["chunk_id"][:16] + "...",
                        "title": first.get("title"),
                        "youtube_url": first["youtube_url"],
                        "snippet": first["snippet"][:80] + "...",
                    },
                    indent=2,
                ),
            )

            ctx = await session.call_tool(
                "show",
                {"kind": "context", "id_": first["chunk_id"], "token_budget": 1500},
            )
            ctx_payload = ctx.structuredContent or json.loads(ctx.content[0].text)
            assert ctx_payload["anchor"]["chunk_id"] == first["chunk_id"]
            assert ctx_payload["citations"], "context returned no citations"
            print(
                "CONTEXT:",
                f"estimated_tokens={ctx_payload['estimated_tokens']} "
                f"chunks={len(ctx_payload['chunks'])} citations={len(ctx_payload['citations'])}",
            )

            chunk_uri = f"yutome://chunk/{first['chunk_id']}"
            res = await session.read_resource(chunk_uri)
            res_payload = json.loads(res.contents[0].text)
            assert res_payload["chunk_id"] == first["chunk_id"]
            assert res_payload["text"], "chunk resource returned empty text"
            print("CHUNK RESOURCE:", f"{len(res_payload['text'])} chars of text")

    print("\nALL E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
