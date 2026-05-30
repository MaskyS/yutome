# MCP capabilities

Yutome has one MCP contract but different trust boundaries. The local and private surfaces are intentionally full-capability for the corpus owner; hosted MCP is multi-tenant and authorizes writes through workspace-scoped grants.

## Capability model

| Surface | Command/path | Trust boundary | Capability model |
|---|---|---|---|
| Local stdio MCP | `yutome serve mcp` | A local process launched by the user's MCP client. | Full capability. Every tool in `contract.TOOLS` is registered, including `index` and `jobs`. No bearer token is needed because stdio is local process execution. |
| Local HTTP API | `yutome serve http` | Loopback HTTP for the same machine, or a private bind only when explicitly configured. | Local HTTP exposes the query HTTP API. A configured `YUTOME_HTTP_TOKEN` authorizes the local owner to use the protected endpoints; it is not a per-tool read/write scope. |
| Private remote MCP | `yutome serve remote mcp` | Streamable HTTP MCP for a private network, reverse proxy, VPN, or similar owner-controlled access path. | Full capability. The shared `YUTOME_HTTP_TOKEN` bearer proof authorizes the corpus owner to use the whole MCP registry, including `index` and `jobs`, even though the baseline MCP scope string remains `yutome.search.read` for compatibility. |
| Remote bridge | `yutome connect` / `yutome serve bridge` | A Cloudflare-backed connector relays jobs to the owner's running Desktop/bridge process. | Full capability while the bridge is online. The bridge dispatches tool calls through `contract.tool_by_name(...)`, so `find`, `list`, `show`, `index`, `jobs`, and `q` share the same local owner trust model. |
| Hosted MCP | Hosted `/mcp/tools/call` and `/mcp/resources/read` | Multi-tenant hosted service, with workspace identity supplied by verified account/connector context. | Read tools require the baseline `contract.AUTH_SCOPE`. Hosted `index` additionally requires `contract.SOURCE_WRITE_SCOPE` and `contract.JOB_WRITE_SCOPE`; without them the adapter returns `insufficient_scope`. |

## Local/private bearer tokens

For local/private MCP surfaces, the bearer token is a full-capability owner credential. It is not a read-only grant and it is not meant to split tools into read and write buckets. Possession of the token means "this caller is allowed to act as the corpus owner on this corpus," including enqueueing public YouTube source indexing through `index` and inspecting indexing status through `jobs`.

This is why `src/yutome/mcp_server.py` registers the entire `contract.TOOLS` registry for stdio and streamable HTTP. The `contract.AUTH_SCOPE` constant is deliberately a neutral baseline identifier in code; its historical string value, `yutome.search.read`, should not be read as the local/private capability boundary.

## Hosted authorization

Hosted MCP is different because it serves multiple workspaces. `HostedMcpAuthContext.validated()` in `src/yutome/hosted/mcp_query.py` requires an authenticated workspace and `contract.AUTH_SCOPE` before any hosted MCP call proceeds. The hosted `index` implementation then calls `_require_scopes(...)` with `contract.SOURCE_WRITE_SCOPE` and `contract.JOB_WRITE_SCOPE`.

Those hosted scopes come from the connector-grant model: assistant OAuth grants are account/workspace records carrying `mcp_client` identity and a scope list. Current hosted MCP grants default to `yutome.search.read`, `yutome.source.write`, and `yutome.job.write`; older read-only grants can still query but must reconnect before `index` is allowed.
