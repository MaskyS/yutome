/**
 * Yutome Remote MCP — Worker entry point.
 *
 * Architecture:
 *   /mcp                  → OAuth-protected. McpAgent (YutomeMcpAgent) handles
 *                           the MCP streamable-HTTP transport. Tool/resource
 *                           calls are routed through the YutomeRelay DO to
 *                           the laptop bridge.
 *   /authorize, /token,   → workers-oauth-provider handles OAuth 2.1.
 *   /register, /.well-known/oauth-*  Consent is gated on the pairing code.
 *   /relay/connect        → laptop bridge WebSocket upgrade (Bearer-token).
 *   /healthz              → liveness probe.
 */
import OAuthProvider from "@cloudflare/workers-oauth-provider";
import type { OAuthHelpers } from "@cloudflare/workers-oauth-provider";
import type { Env } from "./env";
import { YutomeMcpAgent } from "./yutome-mcp-agent";
import { YutomeRelay } from "./yutome-relay";
import { handleAuthorizeRequest, handlePairingRequest } from "./pairing";

interface DefaultHandlerEnv extends Env {
  OAUTH_PROVIDER: OAuthHelpers;
}

// Handler the OAuthProvider invokes for everything outside the protected
// API surface — /authorize (consent form), /pair, /relay/*, /healthz.
const defaultHandler: ExportedHandler<DefaultHandlerEnv> = {
  async fetch(request, env, _ctx) {
    const url = new URL(request.url);

    if (url.pathname === "/healthz") {
      return Response.json({ ok: true, service: "yutome-remote-mcp", mode: env.YUTOME_WORKER_MODE });
    }

    if (url.pathname === "/relay/connect" || url.pathname === "/relay/status") {
      const id = env.RELAY.idFromName("default");
      const stub = env.RELAY.get(id);
      return stub.fetch(request);
    }

    if (url.pathname === "/authorize") {
      return handleAuthorize(request, env);
    }

    if (url.pathname === "/pair") {
      return handlePairingRequest({ request, env, oauthHelpers: env.OAUTH_PROVIDER });
    }

    if (url.pathname === "/") {
      return new Response(
        "Yutome Remote MCP is deployed. Use /mcp as the connector URL.\n" +
        "Browser-visit /pair after running `yutome connect`.\n",
        { headers: { "content-type": "text/plain; charset=utf-8" } },
      );
    }

    return new Response("Not Found", { status: 404 });
  },
};

async function handleAuthorize(request: Request, env: DefaultHandlerEnv): Promise<Response> {
  return handleAuthorizeRequest({ request, env, oauthHelpers: env.OAUTH_PROVIDER });
}

// OAuthProvider expects handlers whose `fetch` property is non-optional.
// McpAgent.serve() and our defaultHandler both supply fetch — just retype
// them so the optional-fetch generic narrows to required.
type FetchHandler = { fetch: NonNullable<ExportedHandler<DefaultHandlerEnv>["fetch"]> };

const apiHandler = YutomeMcpAgent.serve("/mcp") as unknown as FetchHandler;
const defaultHandlerForProvider = defaultHandler as unknown as FetchHandler;

const provider = new OAuthProvider<DefaultHandlerEnv>({
  apiRoute: "/mcp",
  apiHandler,
  defaultHandler: defaultHandlerForProvider,
  authorizeEndpoint: "/authorize",
  tokenEndpoint: "/token",
  clientRegistrationEndpoint: "/register",
  scopesSupported: ["yutome.search.read"],
  allowPlainPKCE: false,
  clientIdMetadataDocumentEnabled: true,
});

export default provider;
export { YutomeMcpAgent, YutomeRelay };
