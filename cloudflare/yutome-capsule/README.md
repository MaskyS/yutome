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
  `/relay/connect` for the bridge to upgrade, and is consulted by the
  McpAgent for every tool/resource call.
- `src/pairing.ts` — HTML/POST handler for `/pair`; consumes the printed
  pairing code that `yutome connect` prints.
- `src/contract.json` — emitted by `yutome contract emit`. Do not edit by
  hand; the parity test catches drift.

## Deploy

```bash
uv run yutome contract emit
cd cloudflare/yutome-capsule
npm install
npx wrangler deploy
```

`uv run yutome connect --deploy` runs `contract emit` and `wrangler deploy`
together and saves the resulting URL to local state.

## Secrets

After the first deploy, set:

```bash
npx wrangler secret put YUTOME_RELAY_TOKEN
npx wrangler secret put YUTOME_PAIRING_CODE
```

Both values are printed by `yutome connect` (or `yutome status`).
