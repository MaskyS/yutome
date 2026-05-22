# yutome-capsule (Cloudflare Worker)

Remote MCP endpoint for yutome. Tools and resource templates come from the
Python contract registry (`src/yutome/contract.py`) via `src/contract.json`,
which is emitted by `uv run yutome contract emit`.

## Components

- `src/index.ts` — entry point. Wraps `YutomeMcpAgent.serve("/mcp")` with
  `OAuthProvider` from `@cloudflare/workers-oauth-provider`.
- `src/yutome-mcp-agent.ts` — `McpAgent` subclass. Loads `contract.json`,
  registers tools and resource templates with `this.server`, and dispatches
  every call through the `YutomeRelay` Durable Object.
- `src/yutome-relay.ts` — Durable Object that holds the laptop bridge
  WebSocket (with hibernation) plus the request/result queue. Exposes
  `/relay/connect` for the bridge to upgrade and `/relay/status` for live
  bridge health, and is consulted by the McpAgent for every tool/resource call.
- `src/pairing.ts` — HTML/POST handler for `/pair`; consumes the printed
  pairing code that `yutome connect` prints.
- `src/contract.json` — emitted by `yutome contract emit`. Do not edit by
  hand; the parity test catches drift.

## Design decisions and constraints

- The public MCP URL is `/mcp`, using Streamable HTTP. This matches the current
  MCP transport spec, Cloudflare Agents guidance, ChatGPT Apps setup, and Codex
  remote MCP configuration. Do not ask users to paste `/authorize`, `/pair`, or
  `/sse`.
- Legacy SSE is not mounted by default. Add it only if a specific target client
  cannot use Streamable HTTP; otherwise it adds a second transport surface with
  separate auth and testing burden.
- `/mcp` is protected by `@cloudflare/workers-oauth-provider` with `/authorize`,
  `/token`, `/register`, and OAuth metadata routes. The Worker is the OAuth
  authority for this single-user connector; no Auth0, Clerk, Cloudflare Access,
  or Yutome account is required.
- Pairing is a consent gate around OAuth, not the MCP transport itself. The form
  stores the OAuth request server-side in `OAUTH_KV` and uses an auth-request
  specific CSRF cookie so duplicate browser tabs do not invalidate one another.
- The Desktop bridge is separate from OAuth. It connects to `/relay/connect`
  with `YUTOME_RELAY_TOKEN`; `/relay/status` uses the same bearer token and only
  returns minimal live state (`bridge_online`, `last_seen_at`).
- `yutome status` should prefer `/relay/status` when a relay token is saved,
  and only fall back to local last-seen timestamps when the Worker status probe
  is unavailable.
- The tracked `wrangler.toml` intentionally omits account-specific Cloudflare
  resource ids. Assisted deploy writes those to ignored state under
  `data/remote/cloudflare/`.
- Connector-only mode requires this computer and `yutome remote bridge` to be
  online. The always-on replica mode is a separate future path, not a hidden
  fallback in this Worker.

Useful external docs:

- MCP transport spec: <https://modelcontextprotocol.io/specification/2025-06-18/basic/transports>
- Cloudflare MCP transport: <https://developers.cloudflare.com/agents/model-context-protocol/transport/>
- Cloudflare OAuth MCP guide: <https://developers.cloudflare.com/agents/guides/securing-mcp-server/>
- Claude remote custom connectors: <https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp>
- ChatGPT Apps connector setup: <https://developers.openai.com/apps-sdk/deploy/connect-chatgpt>
- Codex MCP configuration: <https://developers.openai.com/codex/mcp>

## Deploy

Use the assisted deploy:

```bash
uv run yutome connect --deploy
```

That command runs `contract emit`, creates an account-local `OAUTH_KV`
namespace, writes an ignored generated Wrangler config under
`data/remote/cloudflare/`, runs `wrangler deploy`, pushes secrets, and saves the
resulting URL to local state. The tracked `wrangler.toml` intentionally does not
contain a real KV namespace id because those ids are Cloudflare-account-specific.
Every assisted deploy refreshes the pairing code and bridge token. Use the
newest printed code in the OAuth browser tab, and restart any already-running
`uv run yutome remote bridge` process after redeploying.

## Secrets

Manual deploys must set:

```bash
npx wrangler secret put YUTOME_RELAY_TOKEN
npx wrangler secret put YUTOME_PAIRING_CODE
```

For an already-deployed endpoint, save the same values locally:

```bash
uv run yutome connect \
  --endpoint https://your-worker.example.workers.dev \
  --relay-token <YUTOME_RELAY_TOKEN> \
  --pairing-code <YUTOME_PAIRING_CODE>
```

`yutome status` reports whether the values are configured, but it does not print
secret values.

During Claude/ChatGPT setup, the assistant opens `/authorize`, which renders the
Yutome pairing form. Do not open `/pair` directly. If retries leave multiple
Yutome tabs open, submit the newest printed code in the newest tab and close the
extras after the connector reports success.
