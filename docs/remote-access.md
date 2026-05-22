# Remote Access

Remote access means authenticated read access to the same retrieval surface used by the CLI, local MCP server, and local HTTP API. It is intended for multiple devices, agents, and hosted clients that need to query an already-indexed corpus.

There are now two remote surfaces:

- `yutome connect` is the beginner path. It prepares or records a Cloudflare-backed remote MCP connector for Claude, ChatGPT, and other MCP apps. The V1 connector is laptop-backed: Claude/ChatGPT call the public `/mcp` URL, and `yutome remote bridge` answers from the local corpus while this computer is on.
- `yutome remote serve` and `yutome remote mcp` are power-user surfaces for private networks, reverse proxies, scripts, and self-hosted authenticated MCP/HTTP clients.

The product and architecture decision for the Cloudflare-backed connector is in [cloud-capsule-strategy.md](cloud-capsule-strategy.md).

## Beginner Remote MCP Connector

Use this when the goal is "ask Claude or ChatGPT about my Yutome library" rather than "host my own API server."

```bash
uv run yutome connect
uv run yutome remote bridge
```

`yutome connect --deploy` deploys the tracked TypeScript Worker at `cloudflare/yutome-capsule/` to your Cloudflare account. It runs `yutome contract emit` first (refreshes the tool/resource JSON from the Python registry), creates the account-local `OAUTH_KV` namespace if missing, writes an ignored generated Wrangler config under `data/remote/cloudflare/`, runs `npx wrangler deploy`, generates a `YUTOME_RELAY_TOKEN` + `YUTOME_PAIRING_CODE` pair, pushes both as encrypted Wrangler secrets, and prints the pairing code. Node 18+ / npm / npx must be on PATH.

Each `--deploy` run refreshes the pairing code and bridge token. Use the newest printed code in the OAuth browser tab, and restart any old `uv run yutome remote bridge` process after redeploying.

If you already have an endpoint URL, save it without redeploying. Include the Worker secrets if this computer should run the laptop bridge:

```bash
uv run yutome connect \
  --endpoint https://your-worker.example.workers.dev \
  --relay-token <YUTOME_RELAY_TOKEN> \
  --pairing-code <YUTOME_PAIRING_CODE>
```

Then add the printed `/mcp` URL to each assistant account:

- **Claude**: add one custom connector from Customize > Connectors. Same Claude account makes it available across web, mobile, Desktop, and Cowork. During OAuth, Claude opens the Yutome pairing page in a browser tab; verify the assistant app/redirect shown on the page, then paste the latest pairing code printed by `yutome connect`. If multiple Yutome tabs opened during retries, use the newest tab/newest code and close the extras after success. After connecting, expand the Yutome connector settings, find "Read-only tools," and switch the per-group permission from "Needs approval" to "Allowed always" if you trust this read-only server — otherwise every tool call interrupts with a confirm prompt. Claude's custom connector docs are at <https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp>.
- **ChatGPT**: create an App/connector in ChatGPT with the MCP Server URL. Choose the authenticated/OAuth option. In each chat, select Yutome from `+` > `More` / composer tools before asking. If you rerun deploy while testing, use the newest pairing code and refresh/reconnect the app if ChatGPT keeps using an old OAuth tab. OpenAI's current ChatGPT developer-mode docs are at <https://developers.openai.com/api/docs/guides/developer-mode>, and MCP auth notes are at <https://developers.openai.com/api/docs/mcp>.
- **Other MCP clients**: use the same `/mcp` URL with Streamable HTTP. The MCP transport docs are at <https://modelcontextprotocol.io/docs/concepts/transports>. For manual smoke testing, the Cloudflare guide for MCP Inspector is at <https://developers.cloudflare.com/agents/guides/test-remote-mcp-server/>.

Setup is account-level, not device-level. You should not need a new Yutome endpoint for every phone or laptop. ChatGPT still requires adding the app to a conversation before it considers Yutome tools.

Laptop on (bridge running):

- Claude/ChatGPT can call `find`, `list`, `show`, and `q` through the Worker.
- Results come from local SQLite, LanceDB, and artifacts via a long-lived WebSocket from the bridge to the Worker's Durable Object (Cloudflare WebSocket Hibernation).
- Resources (`yutome://chunk/{id}`, `yutome://video/{id}`, `yutome://channel/{id}`, `yutome://transcript/{id}`) are reachable via `resources/read` — host UIs can render citations inline without burning a tool call.

Laptop off or bridge stopped:

- The connector remains installed.
- Tool calls return a clear "Yutome Desktop is offline" response with last-seen information when available; resource reads return a JSON-RPC `-32002` with the same metadata.

The Worker uses Cloudflare's `@cloudflare/workers-oauth-provider` for OAuth 2.1 (DCR, optional CIMD, PKCE S256, refresh tokens) and the Agents SDK's `McpAgent` for the MCP protocol surface. Pairing is gated by the printed code plus short-lived OAuth state and CSRF validation; no Yutome account, Auth0, Clerk, or Cloudflare Access setup is required.

Remote MCP mode does not require Voyage, Webshare, Gemini, or proxy credentials. The basic laptop-backed connector fits Cloudflare's free Workers plan (Workers + 1 KV namespace + 2 Durable Objects, all SQLite-backed). Always-on/offline search is a later replica mode and may require enabling Cloudflare billing.

## Current Supported Shape

The ready paths today are an authenticated HTTP API for scripts/apps and an authenticated MCP streamable HTTP server for agent clients:

```bash
uv run yutome remote prepare
uv run yutome remote serve --host 0.0.0.0 --port 8765
uv run yutome remote mcp --host 0.0.0.0 --port 8766
```

`remote prepare` writes the shared `YUTOME_HTTP_TOKEN` into `.env` if it is missing. The token is not printed by default. Use `--show-token` when you need to copy it into a client.

`remote serve` and `remote mcp` refuse to bind to a non-loopback interface unless `YUTOME_HTTP_TOKEN` is configured.

## Endpoints

Unauthenticated liveness:

- `GET /healthz`

Authenticated readiness and query endpoints:

- `GET /readyz`
- `POST /find`
- `POST /list`
- `POST /show`
- `POST /q`
- `GET /chunks/{id}`
- `GET /videos/{id}`
- `GET /channels/{id}`
- `GET /transcripts/{id}`

Send:

```text
Authorization: Bearer <YUTOME_HTTP_TOKEN>
```

## Deployment Posture

Preferred production-ish shape:

1. Run yutome on a private host or home server.
2. Bind yutome to loopback:

```bash
uv run yutome remote serve --host 127.0.0.1 --port 8765
uv run yutome remote mcp --host 127.0.0.1 --port 8766
```

3. Put a real HTTPS reverse proxy or private-access layer in front of it.
4. Keep the yutome bearer token enabled even behind that proxy.

Acceptable private-network shape:

```bash
uv run yutome remote serve --host 0.0.0.0 --port 8765
uv run yutome remote mcp --host 0.0.0.0 --port 8766
```

Use this only on a trusted LAN/VPN/Tailscale-style network. Do not expose this directly to the public internet without an HTTPS/auth layer in front.

## Browser Clients

For browser-based clients, allow exact origins:

```bash
uv run yutome remote serve \
  --host 127.0.0.1 \
  --port 8765 \
  --cors-origin https://client.example
```

or set:

```text
YUTOME_HTTP_CORS_ORIGINS=https://client.example,https://other.example
```

Do not use wildcard CORS for a personal corpus.

## Check A Deployment

```bash
uv run yutome remote check https://yutome.example.com --token "$YUTOME_HTTP_TOKEN"
```

If `--token` is omitted, the command reads `YUTOME_HTTP_TOKEN` from `.env` or the process environment.

## Remote MCP

Remote MCP is available at `/mcp` by default:

```bash
uv run yutome remote mcp --host 0.0.0.0 --port 8766
```

It uses the same bearer token as the HTTP API:

```text
Authorization: Bearer <YUTOME_HTTP_TOKEN>
```

When serving behind a public HTTPS reverse proxy, set the public base URL for MCP auth metadata:

```bash
uv run yutome remote mcp \
  --host 127.0.0.1 \
  --port 8766 \
  --server-url https://yutome.example.com
```

The MCP server exposes the same `find`, `list`, `show`, and `q` tools and `yutome://...` resources as the local stdio MCP server. It is a transport adapter over the same in-process API, not a second retrieval implementation.

## Local Claude / Agent Setup

For local Claude-style clients that read an MCP config, this repo includes `.mcp.json`:

```json
{
  "mcpServers": {
    "yutome": {
      "command": "uv",
      "args": ["run", "yutome", "mcp", "serve", "--config", "yutome.toml"]
    }
  }
}
```

Use it from the repo root, or convert `yutome.toml` to an absolute path if the client launches from a different working directory. The local MCP server is stdio and does not need `YUTOME_HTTP_TOKEN`.

The yutome retrieval skill lives at `.claude/skills/yutome-retrieval/SKILL.md`. It teaches agents to use `find`, `list`, `show`, and `q` with timestamped citations and full-transcript escalation. In Claude Code/local repo sessions it also teaches the noob indexing workflow: `uv run yutome add SOURCE`, `uv run yutome sync SOURCE`, then retrieve and cite.

Keep the split clear:

- MCP exposes capabilities. Yutome's current MCP surface is read-only retrieval.
- The CLI owns local mutation and indexing until remote job queues exist.
- Skills teach Claude Code workflow and failure recovery.
- A Claude Code plugin can bundle both a skill and MCP config for distribution. Remote Claude.ai custom connectors only receive the remote MCP surface and its tool/server instructions; they do not automatically receive this project skill.

Before public hosted remote MCP:

- Add user accounts or app-issued tokens.
- Add corpus ownership/ACL checks.
- Add rate limits and audit logging.
- Decide whether remote clients are read-only or can enqueue sync/quality jobs.
- Decide where the corpus and LanceDB indexes live: private server, hosted read-only replica, or sync from the local machine.
