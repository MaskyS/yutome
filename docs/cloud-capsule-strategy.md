# Yutome Cloud Capsule Strategy

Last researched: 2026-05-21

This document records the product and architecture decision for making Yutome usable from mainstream LLM apps without turning the default product into a centrally hosted transcript database.

The short version:

- **Remote MCP is the primary noob product surface.** The user should connect Claude or ChatGPT to Yutome once, then ask questions in the LLM app they already use.
- **V1 works when the user's computer is on.** The remote endpoint is a public connector front door that routes requests back to Yutome Desktop.
- **Always-on search comes next through a user-owned replica.** The same connector can later answer from a Cloudflare-hosted mirror in the user's own account when the laptop is off.
- **Provider setup stays just-in-time.** Remote MCP mode must not require Voyage, Webshare, Gemini, proxy accounts, or Yutome cloud identity.
- **Replica mode may require provider keys.** If the user wants semantic/hybrid search while the laptop is off, the cloud needs a query embedding path. For parity with the local LanceDB/Voyage index, the first design uses the user's Voyage key as a Cloudflare secret, with explicit consent.

## Product Context

Yutome is a local-first YouTube antilibrary. The current product value is that a user's subscribed or selected channels become searchable, citable, exportable, and available to agents without requiring a central Yutome server. The existing docs already establish this local-first posture in [product-design.md](product-design.md) and the existing remote/API surface in [remote-access.md](remote-access.md).

The noob-user problem is not primarily "how do they run a server?" It is "how do they ask their normal assistant about their YouTube corpus without thinking about Yutome as an app?" Many users will not want to open a dashboard or deploy a service. They will open Claude, ChatGPT, or another assistant and ask:

- "What did this channel say about magnesium?"
- "Find the timestamp where that guest talked about GLP-1s."
- "What videos in my library discuss sleep apnea?"
- "Show me the source clip."

That pushes Yutome toward a connector-first product:

```text
Claude / ChatGPT / agent
  -> Yutome remote MCP connector
  -> local or replicated Yutome corpus
```

The web UI can still exist later as an inspector, but the main surface should be the user's daily-driver LLM app.

## End-to-End Validation

Tested on 2026-05-21 with a user-owned Cloudflare Worker remote MCP endpoint and the local Yutome bridge.

- Claude custom connector worked against the Worker `/mcp` endpoint and returned the two newest Yutome videos from the local corpus.
- ChatGPT Apps developer mode worked after adding the app with the MCP Server URL, choosing no auth for the temporary test connector, and selecting the app in a chat from `+` > `More`.
- Both Claude and ChatGPT returned the same result shape through remote tool calls: video titles and channels from `list`.
- No-auth was used only as a temporary read-only developer proof. The deployed Worker now defaults to OAuth/pairing via `@cloudflare/workers-oauth-provider` for private remote MCP.

Observed noob-facing implication: installing the ChatGPT app is not enough. The user also needs to opt the app into each ChatGPT conversation from the composer. The setup copy should say "Apps" first for ChatGPT and reserve "connector" as the protocol/setup bridge term.

## Platform Constraints

### MCP transport

The current MCP remote transport direction is **Streamable HTTP**. The MCP spec says Streamable HTTP uses a single MCP endpoint, such as `/mcp`, for HTTP POST/GET message exchange, replacing the older HTTP+SSE transport. It also recommends localhost binding and proper authentication for local HTTP servers because of DNS rebinding and remote-access risks. Source: [MCP Transports](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports).

Yutome already has:

- local stdio MCP;
- local/authenticated HTTP;
- remote streamable HTTP MCP over the same `find`, `list`, `show`, and `q` query verbs.

The capsule should preserve that contract. Connector layers must not invent a second retrieval API.

### MCP authorization

MCP authorization is optional at the protocol level, but private remote Yutome endpoints should not be authless. The MCP authorization spec is based on OAuth 2.1 plus authorization server metadata, dynamic client registration, and protected resource metadata. Source: [MCP Authorization](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization).

Important implication: "No Cloudflare Auth" is reasonable. "No auth at all" is not reasonable for a private searchable corpus.

The capsule should use built-in Worker OAuth/pairing rather than Cloudflare Access, Auth0, Clerk, or a Yutome login for the user-owned version.

### Claude

Claude custom connectors using remote MCP are available through Claude's connector UI. Current Claude help says remote MCP custom connectors are available across Free, Pro, Max, Team, and Enterprise plans, with Free limited to one custom connector. Claude connects to the remote MCP server from Anthropic cloud infrastructure rather than from the user's local device. Sources: [Claude remote MCP custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp), [Claude remote MCP docs](https://claude.com/docs/connectors/custom/remote-mcp).

That means:

- `localhost` on the user's machine is invisible to Claude web/mobile.
- private VPN-only endpoints are not sufficient for Claude web/mobile.
- a remote connector must expose a public HTTPS endpoint.
- individual users add a custom connector from Claude Customize > Connectors; Claude may route Settings > Connectors there. Team/Enterprise workspaces may require an owner to add it first.

Claude's connector auth docs support OAuth-style remote connector auth, including Dynamic Client Registration and Client ID Metadata Document patterns. They also support authless connectors, but an authless endpoint is not acceptable for a private Yutome corpus. Source: [Claude connector authentication](https://claude.com/docs/connectors/building/authentication).

### ChatGPT

ChatGPT exposes remote MCP servers as Apps. Current OpenAI developer-mode docs describe enabling developer mode from Settings > Apps > Advanced settings, creating an app from Settings > Apps with the public `/mcp` endpoint, and selecting the app from the composer tools/More menu in each chat. OpenAI's MCP docs recommend OAuth for private remote MCP servers and describe ChatGPT support for Client ID Metadata Documents and Dynamic Client Registration. Sources: [ChatGPT Developer mode](https://developers.openai.com/api/docs/guides/developer-mode), [Building MCP servers for ChatGPT Apps and API integrations](https://developers.openai.com/api/docs/mcp).

There is plan/workspace gating to be careful about. Product copy should therefore say "where developer mode is available" rather than promise every user can add a custom ChatGPT MCP app today. Sources: [Developer mode and MCP apps in ChatGPT](https://help.openai.com/en/articles/12584461), [ChatGPT Developer mode](https://developers.openai.com/api/docs/guides/developer-mode).

Current product implication:

- ChatGPT support is more developer/workspace-shaped than Claude's custom connector path.
- ChatGPT setup is account-level, but use is conversation-level: the app must be selected into a chat before the model considers its tools.
- We should still design the Yutome endpoint as a standards-based remote MCP server, because that is the shared path across Claude, ChatGPT, Cursor-style clients, and future clients.

### OpenAI Apps SDK constraints

The Apps SDK docs add a few constraints that matter for Yutome's copy and Worker metadata:

- Developer Mode lets a user test an unpublished app, but the MCP server still needs to be reachable over public HTTPS.
- No-auth tools are acceptable for anonymous/read-only behavior or a temporary developer proof. Anything exposing user-specific data or write actions should authenticate users with OAuth 2.1 conforming to the MCP authorization spec.
- Tool descriptions should start with clear "Use this when..." guidance, parameter schemas should document arguments, and read-only tools should set `readOnlyHint: true`.
- ChatGPT can show tool-call payloads during testing, and write tools require confirmation. Yutome's current remote tools are read-only search/list/show/query operations.
- Public distribution is through OpenAI's app submission process; Developer Mode should be treated as testing, not the production distribution story.

Sources: [OpenAI Apps SDK authentication](https://developers.openai.com/apps-sdk/build/auth), [Optimize Metadata](https://developers.openai.com/apps-sdk/guides/optimize-metadata), [Define tools](https://developers.openai.com/apps-sdk/plan/tools).

### DCR and CIMD

Dynamic Client Registration (DCR) and Client ID Metadata Documents (CIMD) are two ways for an MCP client to identify itself to the OAuth server during connector setup.

- **DCR**: the client POSTs to a `registration_endpoint`; the authorization server returns a generated `client_id` and stores a registration record for that connector/client instance.
- **CIMD**: the client presents an HTTPS metadata-document URL as its `client_id`; the authorization server fetches and validates that document instead of creating a stored registration.

The noob-facing product should hide both acronyms. Internally they matter because major clients are not identical:

- Claude supports both `oauth_dcr` and `oauth_cimd` out of the box, plus authless connectors. Claude's docs recommend CIMD or Anthropic-held credentials for higher-traffic directory connectors because DCR can create many registered clients. Source: [Claude connector authentication](https://claude.com/docs/connectors/building/authentication).
- ChatGPT supports CIMD, DCR, and predefined OAuth clients, and its docs describe CIMD as the preferred registration method when supported. Source: [OpenAI Apps SDK authentication](https://developers.openai.com/apps-sdk/build/auth).

Yutome should implement both where practical:

- advertise DCR with `/register` for clients that expect dynamic registration;
- enable CIMD support so ChatGPT and Claude can use URL-backed client identity without creating endless stored client records;
- support PKCE S256 and protected resource metadata;
- return a real `401 WWW-Authenticate` challenge when `/mcp` requires authorization.

This does not mean the user signs up for another auth product. In the user-owned Cloudflare version, the Worker can be its own OAuth provider and pair the connector to the local Desktop/capsule identity.

### MCP Instructions And Skills

MCP servers can provide usage guidance directly in the protocol. The important always-on surfaces are:

- the server `instructions` returned during MCP initialization;
- tool descriptions and JSON schemas;
- prompts/resources where a client supports them.

`SKILL.md` / Agent Skills are related but not identical. They are a packaging convention for clients that support skill discovery and progressive disclosure. A remote MCP server should not assume Claude, ChatGPT, or every MCP client will fetch a `SKILL.md` from the connector. For Yutome, the product contract is:

- put concise "how to use Yutome" guidance in MCP `instructions`;
- keep `find`, `list`, `show`, and `q` tool descriptions self-contained;
- optionally ship a future Yutome Agent Skill for coding-agent clients that support skills, but treat it as an enhancement rather than the primary remote connector documentation path.

## Decision

Build a **Yutome Cloud Capsule** with two product modes.

### Mode 1: Remote Connector Only

This is the V1 default.

The user's local machine remains the source of truth. Cloudflare only provides the public HTTPS/OAuth/MCP front door and request routing. The corpus, SQLite catalog, LanceDB index, transcript artifacts, Webshare config, Google OAuth refresh token, Gemini config, and local job state all remain on the user's computer.

Flow:

```text
Claude / ChatGPT
  -> https://<user-capsule>.workers.dev/mcp
  -> Cloudflare Worker + Durable Object session/router
  -> WebSocket bridge to Yutome Desktop (Cloudflare WebSocket Hibernation)
  -> local api.py find/list/show/q
  -> local SQLite + LanceDB + artifacts
```

Laptop on:

- Claude/ChatGPT can call Yutome through the remote MCP connector.
- Results come from the current local corpus and local retrieval implementation.
- No Voyage/Webshare/Gemini/proxy account is required just to connect remotely.
- If semantic search is already enabled locally, Desktop uses its normal local Voyage/LanceDB setup.

Laptop off:

- The connector stays installed, but the Worker returns a clear offline response.
- The response should include last seen time, capsule mode, and a plain instruction to open Yutome Desktop.
- No transcript search is available.

This mode solves the "best app is no app" problem without making Yutome a hosted transcript provider.

Implementation note: the bridge uses Cloudflare WebSocket Hibernation. Claude/ChatGPT call `/mcp`; the `McpAgent` request handler invokes `dispatch(kind, method, params)` on the `YutomeRelay` Durable Object; the DO sends a `{type:"job"}` frame over the live WebSocket to `yutome remote bridge`; the bridge runs the local `find/list/show/q` or `resources/read` handler and posts a `{type:"result"}` frame back. The DO hibernates while idle (zero compute), and the bridge auto-reconnects with exponential backoff if the socket drops.

### Mode 2: Always-On Search Replica

This is the next milestone after remote connector mode works.

Desktop remains the ingestion and source-of-truth engine, but it syncs a read-only searchable mirror to the user's Cloudflare account. The same MCP URL continues to work. When Desktop is online, the connector may use live Desktop or the replica; when Desktop is offline, it answers from the last synced replica.

Flow:

```text
Yutome Desktop
  -> remote connection sync
  -> D1 catalog + R2 artifacts + Vectorize index

Claude / ChatGPT
  -> same /mcp URL
  -> Worker answers from replica when Desktop is offline
```

Laptop on:

- Desktop indexes new channels/videos locally.
- Desktop syncs the cloud mirror.
- Claude/ChatGPT can answer through the same connector.

Laptop off:

- Claude/ChatGPT can search, list, show, and quote from the last synced corpus.
- Fresh YouTube sync, transcript cleanup, Webshare use, Gemini fallback, and ASR wait until Desktop is online.

V1 replica should be read-only. Do not run YouTube ingestion, proxy work, transcript cleanup, or ASR in Cloudflare in the first version.

## Why Cloudflare

Cloudflare is the best first substrate because it fits both the product goal and the trust boundary.

### 1. It is already oriented around remote MCP

Cloudflare's remote MCP guide shows Worker-hosted MCP over Streamable HTTP and explicitly supports authenticated or authless servers. Source: [Cloudflare remote MCP server guide](https://developers.cloudflare.com/agents/guides/remote-mcp-server/).

Cloudflare's own MCP launch post is directly aligned with the problem Yutome has: moving MCP beyond local stdio so web and mobile LLM clients can use it. The post calls out OAuth provider support, `McpAgent`, `mcp-remote`, and remote MCP testing. Source: [Cloudflare remote MCP blog](https://blog.cloudflare.com/remote-model-context-protocol-servers-mcp/).

Cloudflare also maintains `workers-oauth-provider`, a Worker library that handles OAuth 2.1 provider mechanics, token management, DCR, protected-resource metadata, and optional CIMD support. Cloudflare's MCP security docs recommend it for OAuth-protected MCP servers. Sources: [Securing MCP servers](https://developers.cloudflare.com/agents/guides/securing-mcp-server/), [workers-oauth-provider](https://github.com/cloudflare/workers-oauth-provider).

The Worker uses Cloudflare's official MCP stack — `@cloudflare/workers-oauth-provider` for OAuth 2.1 (protected-resource metadata, authorization-server metadata, DCR registration, authorization-code + PKCE S256, refresh tokens, signed bearer tokens) and the Agents SDK's `McpAgent` for the MCP protocol (initialize, tools/*, resources/*). A small `pairing.ts` handler renders the local pairing-code consent page that the OAuth provider invokes during `/authorize`. That keeps the deployable Worker small and matches every major OAuth surface Claude/ChatGPT expect.

### 2. It supports the front-door role without owning the corpus

For Remote Connector Only mode, Cloudflare can act as:

- public HTTPS endpoint;
- OAuth/pairing endpoint;
- MCP Streamable HTTP endpoint;
- Durable Object-backed session/router;
- WebSocket bridge (Cloudflare WebSocket Hibernation) to Yutome Desktop.

Durable Objects are a good fit for long-lived session coordination. Cloudflare documents Durable Objects as WebSocket-capable endpoints, and the hibernation API reduces idle cost by allowing objects to sleep without disconnecting clients. Source: [Durable Objects WebSockets](https://developers.cloudflare.com/durable-objects/best-practices/websockets/).

Cloudflare also supports SQLite-backed Durable Objects on the Workers Free plan, which is enough for the small rendezvous queue used by laptop-backed Remote MCP. Source: [Durable Objects migrations](https://developers.cloudflare.com/durable-objects/reference/durable-objects-migrations/).

### 3. It supports user-owned one-click deployment

Deploy-to-Cloudflare buttons can provision resources declared in Wrangler config, including D1, R2, Vectorize, Durable Objects, Workers AI, Queues, and secrets. Source: [Cloudflare Deploy Buttons](https://developers.cloudflare.com/workers/platform/deploy-buttons/).

This matters because user-owned deployment is the way to make the system noob-friendly without making Yutome the default data host. The user may still need a Cloudflare account, but they do not need:

- a Yutome cloud account;
- Cloudflare Access;
- Auth0/Clerk;
- a separate hosted vector database account;
- a separate object storage account.

### 4. It has the right replica primitives

For Always-On Search Replica mode:

- **D1** can hold channel/video/chunk/catalog metadata.
- **R2** can hold transcript artifacts and exportable corpus files.
- **Vectorize** can hold queryable embeddings.
- **Worker secrets** can hold the user's Voyage key if they opt into semantic/hybrid replica parity.

Cloudflare documents Worker secrets as encrypted text bindings for API keys and auth tokens. Source: [Cloudflare Workers secrets](https://developers.cloudflare.com/workers/configuration/secrets/).

Costs are likely acceptable for personal corpora:

- Workers Paid is a $5/month account minimum and includes Workers/Durable Objects base usage. Source: [Cloudflare Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/).
- D1 includes a free plan and paid allowances around rows read/written plus 5 GB storage. Source: [Cloudflare D1 pricing](https://developers.cloudflare.com/d1/platform/pricing/).
- R2 has free monthly storage/operation allowances and no egress bandwidth fees. Source: [Cloudflare R2 pricing](https://developers.cloudflare.com/r2/pricing/).
- Vectorize pricing is based on stored and queried vector dimensions; the paid plan includes initial allowances and small per-dimension overages. Source: [Cloudflare Vectorize pricing](https://developers.cloudflare.com/vectorize/platform/pricing/).

Important caveat: Vectorize is a paid Workers feature. The noob copy should not imply that always-on semantic search is free forever.

## Why Not the Other Options

### Claude MCPB / Desktop Extension

MCPB is excellent for local Claude Desktop users. It packages a local MCP server into a `.mcpb` file, runs locally over stdio, works offline, and requires no OAuth. Source: [Claude MCPB docs](https://claude.com/docs/connectors/building/mcpb).

It should still be a distribution path, but it does not solve:

- Claude web/mobile;
- ChatGPT;
- "ask from any device";
- laptop-off search.

Use it as the best local-only noob path, not as the remote strategy.

### zrok / OpenZiti

zrok is the strongest open-source tunnel alternative. It is built on OpenZiti, supports public HTTPS shares, and supports private shares for zrok users. Sources: [zrok overview](https://netfoundry.io/docs/zrok/intro), [zrok private shares](https://docs.zrok.io/docs/concepts/sharing-private/), [zrok getting started](https://docs.zrok.io/docs/getting-started/).

The issue is client reachability:

- Claude and ChatGPT cloud clients need a public HTTPS endpoint.
- zrok private shares require the accessor to run `zrok access` and have a zrok-enabled account on the same service instance.
- Therefore private zrok shares are not a fit for Claude/ChatGPT web connectors.

zrok public shares can work as a BYO tunnel mode, but they are not as integrated as Cloudflare for OAuth, deploy provisioning, replica storage, and per-user cloud account billing.

### ngrok

ngrok has strong MCP-specific gateway docs and is polished for exposing a local MCP server securely. It can sit in front of a local server with traffic policy, identity, observability, and public endpoints. Source: [ngrok MCP gateway](https://ngrok.com/docs/using-ngrok-with/using-mcp).

It is a good BYO provider option, especially for developers. It is less ideal as the default because:

- the user now needs an ngrok account;
- ngrok remains in the request path;
- it does not solve the always-on replica story;
- it does not give us D1/R2/Vectorize-style user-owned storage primitives.

### Tailscale Funnel

Tailscale Funnel can expose a local service to the public internet with HTTPS. Source: [Tailscale Funnel](https://tailscale.com/docs/features/tailscale-funnel).

It is useful for power users already on Tailscale, but it has product drawbacks:

- Funnel is still documented as beta.
- It requires Tailscale account/tailnet setup.
- Tailscale Serve is private to the tailnet, while Claude/ChatGPT cloud clients need Funnel/public internet reachability.
- It does not provide the replica storage/index story.

Keep it as a power-user BYO path, not the default noob path.

### Managed MCP hosts: Smithery, MCP Nest

Managed MCP hosts can run MCP servers remotely and expose endpoints to Claude/ChatGPT/Cursor-style clients. Smithery supports hosted custom containers and publishes hosted MCP URLs. Source: [Smithery custom containers](https://smithery.mintlify.dev/docs/build/deployments/custom-container). MCP Nest advertises cloud-hosted remote MCP endpoints for local stdio or HTTP servers. Source: [MCP Nest](https://mcpnest.dev/).

These are good for demos and distribution, but they push Yutome toward one of two uncomfortable states:

- the corpus/index runs inside someone else's hosted MCP environment; or
- the hosted MCP server still needs a tunnel back to the laptop.

Neither gives us the clean user-owned Cloudflare replica path.

### SaaS MCP platforms: Zapier, Pipedream, Composio-style tools

Zapier MCP, Pipedream MCP, and Composio-style MCP services show that users can connect one MCP endpoint to many apps, and they support major clients like Claude and ChatGPT. Sources: [Zapier MCP](https://help.zapier.com/hc/en-us/articles/36265392843917-Use-Zapier-MCP-with-your-client), [Pipedream MCP](https://pipedream.com/docs/connect/mcp/users/), [Composio MCP](https://docs.composio.dev/docs/mcp-providers).

These platforms are strong for SaaS APIs and workflow actions. Yutome is different: the core data is a private local transcript/index corpus. A SaaS action platform does not remove the need to host, tunnel, or replicate that corpus.

### Fully Yutome-hosted corpus

This is the best UX and the worst default trust boundary.

Flow:

```text
User pays Yutome
  -> Yutome hosts transcripts/chunks/vectors
  -> Yutome handles ingestion, provider keys, quotas, billing
```

It would let noobs skip Cloudflare/Voyage/Webshare setup, but it makes Yutome responsible for:

- hosted transcript corpora;
- embeddings and storage cost;
- user deletion/export flows;
- abuse and rate limits;
- provider billing;
- security and breach risk;
- more legible legal/compliance questions around hosted media-derived data.

This can be a later paid managed product, but it should not be the default path while we are still validating usage and support burden.

## Data Boundary

The word "everything" needs precision.

For replica mode, "full corpus replica" should mean enough data to reproduce local search, citation, and inspection while offline:

- channels and selected-library state;
- videos and metadata;
- transcript versions and active-version pointers;
- normalized transcript artifacts;
- chunk text, timestamps, source segment references, and citation URLs;
- embedding vectors and embedding model/dimension metadata;
- corpus health/status summaries needed for noob-facing explanations.

It should not include:

- `.env`;
- Google OAuth refresh/access tokens;
- Webshare credentials;
- generic proxy credentials;
- Gemini API keys;
- Voyage key unless explicitly uploaded as a Cloudflare secret for replica semantic query embeddings;
- local logs;
- caches;
- raw provider debug dumps that are not needed for search/citation;
- local job queue internals;
- machine paths that leak unnecessary filesystem details.

This distinction keeps the replica useful while avoiding accidental cloud backup of operational secrets.

## Provider Handling

### Remote Connector Only

Requires no new provider accounts.

If the local corpus was built with lexical search only, remote MCP still works for lexical find/list/show/q. If local semantic search is configured, Desktop uses the existing local Voyage/LanceDB setup. If Webshare is not configured, that only affects future ingest reliability, not remote search over already-indexed data.

### Always-On Replica

Replica mode can support lexical search without Voyage, but the product decision is to prefer semantic/hybrid parity. For semantic/hybrid offline search, the Worker needs to embed the query. To avoid local/cloud ranking drift, use the same Voyage model/dimension as local search.

Wizard behavior:

1. If `VOYAGE_API_KEY` is configured locally, explain that semantic cloud search requires storing this key as an encrypted Cloudflare Worker secret in the user's account.
2. Ask for explicit confirmation before upload.
3. If missing, open the Voyage key setup flow, save locally, then upload after confirmation.
4. Never ask for Webshare or Gemini during replica setup.

Webshare remains just-in-time and local. It should be requested only after YouTube transcript fetching is blocked/rate-limited or the user explicitly configures a proxy.

## Auth Decision

Do not use Cloudflare Auth for the user-owned capsule.

Do use OAuth-compatible connector auth implemented by the Worker. The simplest product language is "pair this connector with Yutome Desktop." Internally the Worker should expose the metadata and token endpoints needed by Claude/ChatGPT-compatible MCP OAuth flows.

Authless mode is acceptable only for:

- public demo corpora;
- local stdio MCP;
- maybe temporary developer testing.

It is not acceptable for a private replica. A hard-to-guess URL is not enough because URLs leak through browser history, logs, screenshots, support bundles, LLM traces, and shared connector settings.

## User Flows

### First setup

Noob-facing copy should avoid implementation words like Worker, Durable Object, Vectorize, and OAuth until the user asks for advanced details.

The user-facing verb should be **connect**, not **capsule**. "Cloud Capsule" is the architecture/product name for the user-owned remote environment, but a normal user should not need to understand or type it. Remote setup should appear as a guided step inside `yutome setup`, plus a direct `yutome connect` command for users who already have a local corpus.

`yutome setup` should introduce this in noob language as **Use Yutome from Claude/ChatGPT** after the local corpus steps. The copy should explain the value first: the user can ask their normal assistant about their YouTube library instead of opening Yutome. Then it should explain the rough shape: one remote MCP connector URL, add it once per assistant account, the user chooses which assistant they want help with (Claude, ChatGPT, both, or another MCP client), ChatGPT also requires selecting the Yutome app in each chat from `+` > `More` / composer tools, the laptop-backed V1 needs this computer and `yutome remote bridge` online, and setup needs a small public connector endpoint. Do not assume the noob user has a Cloudflare account; if Yutome or a team provides the endpoint they can paste it, otherwise Yutome can prepare Cloudflare deploy files for the user or a helper to deploy. The copy should also say that this step does not require Voyage, Webshare, Gemini, or proxy credentials.

Primary choice:

```text
Where should Yutome work?

[Use while this computer is on]
Connect Claude/ChatGPT to Yutome. Your corpus stays on this computer.

[Use even when this computer is off]
Create a private cloud copy in your Cloudflare account.
```

Advanced detail can map this to:

- Remote Connector Only
- Always-On Search Replica

### Remote Connector Only setup

1. User runs `yutome setup` and accepts the "Connect Claude/ChatGPT" step, or runs `yutome connect`.
2. User chooses "Use while this computer is on."
3. CLI deploys the tracked TypeScript Worker project at `cloudflare/yutome-capsule/`. It emits `contract.json` from the Python registry, ensures an account-local `OAUTH_KV` namespace exists (auto-creates it on first deploy), writes the real KV binding to ignored generated config under `data/remote/cloudflare/`, runs `npx wrangler deploy`, generates `YUTOME_RELAY_TOKEN` + `YUTOME_PAIRING_CODE`, pushes both as Wrangler secrets, and saves them to `data/remote/connection.json`.
4. If Node 22+ / npm / npx are available, `yutome connect --deploy` is the default assisted path: Yutome uses `npx` to run Wrangler, downloading it if needed, runs the deploy, and lets Wrangler open Cloudflare sign-in if needed. The user does not need a global Wrangler install.
5. If Node/npm/npx are missing or Node is older than 22, Yutome explains the runtime problem in plain language and asks the user to install Node.js 22 LTS or newer, then rerun `yutome connect --deploy`. (The dashboard-paste fallback is no longer offered because the Worker is a multi-file TypeScript project, not a single JS file.)
6. Future no-node best path should be a public Deploy-to-Cloudflare template. Cloudflare documents Deploy buttons as a way to let users deploy a Workers app into their own account, with resource provisioning from the app configuration. Source: [Cloudflare Deploy Buttons](https://developers.cloudflare.com/workers/platform/deploy-buttons/).
7. CLI stores the deployed endpoint and normalized `/mcp` URL in local remote state.
8. User starts `yutome remote bridge` when they want the assistant to reach the laptop-backed corpus.
9. User adds the MCP URL to Claude/ChatGPT.
   - Claude: add one custom connector for the Claude account from Customize > Connectors, leaving advanced OAuth fields blank.
   - ChatGPT: turn on Developer mode where available, create the app with the MCP Server URL from Settings > Apps, choose OAuth/authenticated, then select Yutome from `+` > `More` / composer tools in each chat.
   - Other clients: use the same `/mcp` URL with Streamable HTTP; OAuth/DCR is handled by the Worker, and the latest printed pairing code is the only user-facing secret.
10. Desktop holds the Worker bridge WebSocket, executes local tool calls, and posts results back.
11. Claude/ChatGPT can query local Yutome while Desktop is online.
12. Connector OAuth/pairing protects `/mcp`; no-auth is only an explicit development/debug switch.

Transport decision:

- `/mcp` is the canonical public connector URL. It uses MCP Streamable HTTP, which the current MCP spec defines as the standard remote transport and which Cloudflare, ChatGPT Apps, and Codex document as the production path.
- `/sse` is legacy compatibility only. It is not part of the default Yutome setup unless a specific target client cannot use `/mcp`; adding it means testing and protecting a second transport path.
- Remote Claude custom connectors still need a public URL because calls originate from Anthropic infrastructure, including for Claude Desktop and Cowork. Local Desktop MCP configuration is a separate product path.
- Claude API examples still show SSE in places, but the API connector documentation says publicly exposed HTTP servers support both Streamable HTTP and SSE. For Yutome's Claude/ChatGPT/Codex noob path, the instruction remains: paste the exact `/mcp` URL.

The setup command should treat local deploy capability as a convenience, not an assumption. My machine having Wrangler is not representative of noob machines. The CLI should detect the actual machine state:

```text
Node 22+ / npm / npx present:
  "Yutome can deploy this from here. This may open Cloudflare sign-in."

Node/npm/npx missing or Node older than 22:
  "This computer cannot run Cloudflare's deploy tool yet."
  "Install Node.js 22 LTS or newer, then rerun yutome connect --deploy."
```

This keeps the beginner path honest. If local assisted deploy is possible, it is the default. If it is not possible, the CLI does not pretend a copied Wrangler command is noob-friendly.

### Always-On Replica setup

1. User runs `yutome setup` and accepts the "Connect Claude/ChatGPT" step, or runs `yutome connect`.
2. User chooses "Use even when this computer is off."
3. CLI opens Deploy to Cloudflare.
4. Cloudflare provisions Worker, D1, R2, Vectorize, and needed secrets.
5. CLI checks whether local semantic search/Voyage is configured.
6. If semantic replica is enabled, CLI confirms uploading the local Voyage key as a Worker secret.
7. CLI runs the initial remote sync.
8. User adds the same MCP URL to Claude/ChatGPT.
9. If Desktop goes offline, Worker serves from the last synced replica.

### Status command

Remote status should be exposed through the normal status surface, not a brand-new noob namespace:

- `yutome status` should include a compact remote connector section when configured.
- `yutome remote status` can expose the detailed power-user view.

Status should show:

- mode: connector-only or replica;
- endpoint URL;
- Claude/ChatGPT connector URL;
- Desktop connection: online/offline/last seen;
- Desktop connection source: live Worker status when `/relay/status` is reachable, otherwise local last-seen fallback;
- local corpus health: videos, chunks, embeddings, attention rows;
- cloud readiness: Worker reachable, auth configured, replica available;
- last sync time;
- offline search: enabled/disabled;
- semantic replica: enabled/disabled and model/dimension.

`/relay/status` should route to the same Durable Object as `/relay/connect`,
require the same `YUTOME_RELAY_TOKEN`, and return only `bridge_online` plus
`last_seen_at`. The normal CLI should not guess based only on local state when
the Worker can answer live status.

### Connector setup across apps

The Cloudflare endpoint is per Yutome corpus, not per physical device. The user should create one remote MCP URL and reuse it across the assistant apps they use:

```text
One Yutome remote MCP URL
  -> connect once in Claude
  -> connect once in ChatGPT
  -> connect once in Cursor or another MCP client if desired
```

For Claude remote connectors, current Claude docs say remote connectors are brokered through the Claude account and are available across Claude web, mobile, Desktop, Cowork, and Claude Code surfaces. Product copy should therefore say: connect Yutome once per Claude account or workspace, not once per laptop/phone/tablet.

Important caveats:

- A different Claude account or workspace needs its own connection.
- Claude local MCPB/Desktop extensions are separate from Claude remote connectors.
- ChatGPT needs its own connection; Claude setup does not carry over.
- Other MCP clients need their own app/account setup.
- If Yutome is in Remote Connector Only mode and Desktop is offline, the connector may be installed everywhere but will return the Yutome-offline response.

`yutome connect` should make this explicit:

```text
Use this same MCP URL for each assistant account.
You do not need a new Yutome endpoint for every device.
```

## Implementation Shape

### Local code

The local implementation should keep beginner verbs aligned with the existing CLI philosophy:

- `yutome setup` should offer remote connection as an optional guided step after basic local setup.
- `yutome connect` should be the direct user-facing command for connecting Claude/ChatGPT; without an endpoint it should generate a deployable Cloudflare Worker project, explain whether assisted deploy is available on this machine, and with `--deploy` it should use `npx`/Wrangler for the user.
- `yutome status` should include remote connector health when configured.
- `yutome remote status` should provide detailed operational status.
- `yutome remote sync` should handle replica export/upload when always-on search is enabled.
- `yutome disconnect` is the beginner cleanup command. It removes local remote connector state and, when Yutome knows the deployed Worker name and Wrangler can run, removes that Worker from the user's Cloudflare account. `yutome remote disconnect` remains as a detailed alias; destructive nouns like `delete` are not part of the beginner-facing remote CLI.
- An internal or advanced `capsule` module/package can still hold implementation code and state helpers.

Local state should live under `data/remote/` or another project data path, not in tracked config by default. It should include:

- capsule id;
- mode;
- endpoint URL;
- local pairing secret/public identifier;
- last seen / last sync metadata;
- cloud resource identifiers if needed;
- schema/export version.

### Worker code

The Worker should be a deployable project, likely under a directory such as `cloudflare/yutome-capsule/`, with:

- `/mcp` Streamable HTTP endpoint;
- OAuth metadata, authorize, token, and registration endpoints;
- pairing UI/API;
- Durable Object session/router for Desktop-backed mode;
- WebSocket bridge endpoint (`/relay/connect`) for the Desktop relay, authenticated by `YUTOME_RELAY_TOKEN`;
- live bridge status endpoint (`/relay/status`), authenticated with the same relay token;
- D1/R2/Vectorize access for replica-backed mode;
- `/healthz` and `/readyz`;
- administrative sync endpoints protected by a local pairing/admin token.

Worker auth and pairing constraints:

- `@cloudflare/workers-oauth-provider` owns OAuth token storage, DCR, PKCE validation, protected-resource metadata, and access-token validation for `/mcp`.
- Yutome's pairing form is the consent gate for a single-owner connector. It should preserve OAuth request state in `OAUTH_KV`, use an auth-request-specific CSRF cookie, and make duplicate/retried authorization tabs safe.
- The pairing code and relay token are Worker secrets. Assisted deploy rotates both, persists the same values locally, and prints only the pairing code as the user-facing secret.
- Manual secret writes should send newline-terminated values to Wrangler; the saved local state and deployed Worker secret must match exactly or the Desktop bridge will get `401`.

### Sync/export format

Replica sync should be versioned and idempotent:

- write a manifest with schema version, source corpus id, export time, local Yutome version, embedding provider/model/dimension, and counts;
- upload catalog batches to D1;
- upload transcript artifacts to R2;
- upsert vectors to Vectorize;
- record tombstones/deletions;
- finish by marking the snapshot active.

Cloud should never treat a half-uploaded snapshot as active.

## Rollout

### Milestone 1: Documentation and scaffolding

- Add this decision doc.
- Add Cloudflare Worker scaffold.
- Add `yutome connect`, remote status, and remote sync scaffolding that persist state and print next steps.
- Add tests that setup/status do not require provider keys.

### Milestone 2: Remote Connector Only

- Implement Worker OAuth/pairing before broad live/private traffic.
- Support DCR, PKCE S256, protected-resource metadata, and a noob-readable pairing/consent screen. CIMD can be added if a target client requires stricter URL-backed client identity.
- Implement Desktop bridge to Worker. Polling is acceptable for the first working proof; WebSocket can follow.
- Forward MCP calls to local `api.py`.
- Return structured offline status when Desktop is unavailable.
- Test with MCP inspector, Claude custom connector, and ChatGPT Developer Mode.

### Milestone 3: Replica export

- Add `yutome remote sync --dry-run`.
- Export corpus manifest and batches locally.
- Verify secret exclusion.
- Add deletion/tombstone tracking.

### Milestone 4: Replica serving

- Upload D1/R2/Vectorize data.
- Serve `find/list/show/q` from cloud replica.
- Add offline mode fallback.
- Compare local and replica result quality on fixed evals.

### Milestone 5: Managed options

Only after usage is understood:

- optional Yutome-managed hosted corpus;
- optional Yutome-managed embedding/proxy brokerage;
- full cloud ingestion.

## Open Risks

- Claude and ChatGPT MCP connector behavior is still moving. Keep the Worker standards-based and test both clients regularly.
- OAuth compatibility is the highest-risk implementation area. Prefer Cloudflare's existing OAuth provider library if it fits the pairing model.
- Query parity between LanceDB local hybrid search and Vectorize/D1 cloud search may not be exact. Treat "same answer quality" as an eval target, not a guaranteed identical ranking.
- User-owned Cloudflare is cleaner legally and financially, but still adds a required account for always-on mode.
- Uploading a Voyage key to Cloudflare is a meaningful trust boundary change. It must be explicit and reversible.

## Final Recommendation

Ship **Remote Connector Only** first, using Cloudflare as a user-owned public MCP front door over the local Yutome Desktop corpus. This gives noob users the main product experience: ask Claude/ChatGPT about Yutome without opening Yutome.

Then ship **Always-On Search Replica** behind the same connector URL. The replica should be read-only, user-owned, and scoped to search/citation data. It should sync corpus data but not local secrets. Semantic replica uses Voyage parity with explicit key upload consent.

Do not start with fully Yutome-hosted transcript search. That may become a paid managed tier later, but the first architecture should prove connector usage and preserve the local-first trust boundary.
