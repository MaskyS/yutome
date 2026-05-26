/**
 * /authorize + /pair — OAuth consent flow used by the OAuthProvider's defaultHandler.
 *
 * When Claude/ChatGPT redirects the browser to /authorize, the OAuth provider
 * routes the unauthenticated request here. Connector-only deployments ask for
 * the pairing code that `yutome connect` printed. Hosted deployments require
 * the account app to set a signed account-session cookie and then approve a
 * selected workspace. A valid approval calls `OAuthHelpers.completeAuthorization()`,
 * which finalizes the grant and redirects the browser back to the MCP client
 * with the auth code.
 */
import type { Env, YutomeAuthProps } from "./env";
import type { AuthRequest, ClientInfo, OAuthHelpers } from "@cloudflare/workers-oauth-provider";
import {
  assertAuthRequestTargetsMcpAudience,
  configuredMcpAudience,
  HostedAccountGrantError,
  type HostedAccountSession,
  hostedGrantProps,
  issueHostedAccountGrant,
  resolveHostedAccountSessionFromRequest,
} from "./account-grants.ts";
import { isHostedWorkerMode, resolveConfiguredBridgeIdentity, TenantRoutingError } from "./tenant-routing.ts";

interface PairingContext {
  request: Request;
  env: Env;
  oauthHelpers: OAuthHelpers;
}

interface StoredAuthorizationRequest {
  authRequest: AuthRequest;
  clientName?: string;
  clientUri?: string;
  logoUri?: string;
  redirectUri: string;
  scope: string[];
  csrfToken: string;
  expiresAt: number;
  completedRedirectTo?: string;
}

interface RenderAuthorizationState {
  authRequestId: string;
  csrfToken: string;
  clientName?: string;
  clientUri?: string;
  logoUri?: string;
  redirectUri: string;
  scope: string[];
}

const AUTH_STATE_TTL_SECONDS = 10 * 60;
const COMPLETED_AUTH_STATE_TTL_SECONDS = 60;
const DEFAULT_OAUTH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60;
const AUTH_STATE_PREFIX = "yutome:pairing:auth:";
const CSRF_COOKIE_PREFIX = "__Host-yutome_pairing_";
const AUTH_REQUEST_ID_PATTERN = /^[0-9a-f-]{36}$/;

export async function handleAuthorizeRequest(ctx: PairingContext): Promise<Response> {
  const { request, env, oauthHelpers } = ctx;
  const url = new URL(request.url);
  const authRequest = await oauthHelpers.parseAuthRequest(request);
  const client = await oauthHelpers.lookupClient(authRequest.clientId);
  if (isHostedWorkerMode(env.YUTOME_WORKER_MODE)) {
    let accountSession;
    try {
      accountSession = await resolveHostedAccountSessionFromRequest(request, env, {
        allowWorkspaceSelection: true,
      });
    } catch (err) {
      if (err instanceof HostedAccountGrantError) {
        return errorResponse(err.message, undefined, err.status);
      }
      throw err;
    }
    try {
      assertAuthRequestTargetsMcpAudience(authRequest, configuredMcpAudience(env));
    } catch (err) {
      if (err instanceof HostedAccountGrantError) {
        return errorResponse(err.message, undefined, err.status);
      }
      throw err;
    }
    const renderState = await createAuthorizationState(env, authRequest, client);
    return renderHostedConsentForm(url, "", renderState, accountSession);
  }
  const renderState = await createAuthorizationState(env, authRequest, client);
  return renderForm(url, "", renderState);
}

export async function handlePairingRequest(ctx: PairingContext): Promise<Response> {
  const { request, env, oauthHelpers } = ctx;
  const url = new URL(request.url);

  if (request.method === "GET") {
    return renderForm(url, "");
  }
  if (request.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  const form = await request.formData();
  const authRequestId = String(form.get("auth_request_id") || "").trim();
  const csrfToken = String(form.get("csrf_token") || "").trim();
  const cookieName = csrfCookieName(authRequestId);
  const cookieToken = cookieName ? readCookie(request.headers.get("cookie") || "", cookieName) : null;
  const state = cookieName ? await loadAuthorizationState(env, authRequestId) : null;
  if (!authRequestId || !csrfToken || !cookieToken || !state || cookieToken !== csrfToken || state.csrfToken !== csrfToken) {
    return errorResponse("Missing or expired authorization context. Restart connector setup from your assistant.", authRequestId);
  }
  if (state.completedRedirectTo) {
    return redirectResponse(state.completedRedirectTo);
  }
  const renderState = toRenderState(authRequestId, state);

  if (isHostedWorkerMode(env.YUTOME_WORKER_MODE)) {
    let accountSession;
    try {
      accountSession = await resolveHostedAccountSessionFromRequest(request, env, {
        selectedWorkspaceId: String(form.get("workspace_id") || "").trim(),
      });
    } catch (err) {
      if (err instanceof HostedAccountGrantError) {
        return errorResponse(err.message, authRequestId, err.status);
      }
      throw err;
    }

    let grant;
    try {
      grant = await resolveHostedOAuthGrant({
        authRequestId,
        authRequest: state.authRequest,
        accountSession,
        env,
      });
    } catch (err) {
      if (err instanceof HostedAccountGrantError) {
        return errorResponse(err.message, authRequestId, err.status);
      }
      throw err;
    }

    return completePairingAuthorization({
      authRequestId,
      env,
      oauthHelpers,
      state,
      grant,
    });
  }

  const supplied = String(form.get("pairing_code") || "").trim().toUpperCase();
  const expected = String(env.YUTOME_PAIRING_CODE || "").trim().toUpperCase();
  if (!expected) {
    return errorResponse("Yutome pairing is not configured. Run `yutome connect --deploy` again.");
  }
  if (!supplied || supplied !== expected) {
    return renderForm(
      url,
      "That pairing code was not accepted. Check `yutome status` or rerun `yutome connect`.",
      renderState,
    );
  }

  let bridgeIdentity;
  try {
    bridgeIdentity = resolveConfiguredBridgeIdentity(env);
  } catch (err) {
    if (err instanceof TenantRoutingError) {
      return errorResponse(`Missing hosted tenant identity: ${err.message}`, authRequestId);
    }
    throw err;
  }

  const grant = resolveConnectorOAuthGrant({
    authRequestId,
    authRequest: state.authRequest,
    bridgeIdentity,
    env,
  });

  return completePairingAuthorization({
    authRequestId,
    env,
    oauthHelpers,
    state,
    grant,
  });
}

async function resolveHostedOAuthGrant({
  authRequestId,
  authRequest,
  accountSession,
  env,
}: {
  authRequestId: string;
  authRequest: AuthRequest;
  accountSession: HostedAccountSession;
  env: Env;
}): Promise<{ userId: string; props: YutomeAuthProps }> {
  const pairedAt = new Date();
  const audience = configuredMcpAudience(env);
  assertAuthRequestTargetsMcpAudience(authRequest, audience);
  const accountGrant = await issueHostedAccountGrant(env, {
    grantId: authRequestId,
    authRequest,
    accountSession,
    audience,
    tokenVersion: configuredTokenVersion(env),
    expiresAt: new Date(pairedAt.getTime() + configuredTokenTtlSeconds(env) * 1000).toISOString(),
    now: pairedAt,
  });
  return {
    userId: accountGrant.user_id,
    props: hostedGrantProps(accountGrant),
  };
}

function resolveConnectorOAuthGrant({
  authRequestId,
  authRequest,
  bridgeIdentity,
  env,
}: {
  authRequestId: string;
  authRequest: AuthRequest;
  bridgeIdentity: { workspace_id: string; install_id: string };
  env: Env;
}): { userId: string; props: YutomeAuthProps } {
  return {
    userId: "yutome-owner",
    props: buildConnectorOAuthGrantProps({
      authRequestId,
      authRequest,
      bridgeIdentity,
      env,
    }),
  };
}

async function completePairingAuthorization({
  authRequestId,
  env,
  oauthHelpers,
  state,
  grant,
}: {
  authRequestId: string;
  env: Env;
  oauthHelpers: OAuthHelpers;
  state: StoredAuthorizationRequest;
  grant: { userId: string; props: YutomeAuthProps };
}): Promise<Response> {
  const { redirectTo } = await oauthHelpers.completeAuthorization({
    request: state.authRequest,
    userId: grant.userId,
    metadata: oauthGrantMetadata(grant.props),
    scope: state.authRequest.scope,
    props: grant.props,
  });

  await env.OAUTH_KV.put(
    authStateKey(authRequestId),
    JSON.stringify({
      ...state,
      completedRedirectTo: redirectTo,
      expiresAt: Date.now() + COMPLETED_AUTH_STATE_TTL_SECONDS * 1000,
    }),
    { expirationTtl: COMPLETED_AUTH_STATE_TTL_SECONDS },
  );

  return redirectResponse(redirectTo);
}

function buildConnectorOAuthGrantProps({
  authRequestId,
  authRequest,
  bridgeIdentity,
  env,
}: {
  authRequestId: string;
  authRequest: AuthRequest;
  bridgeIdentity: { workspace_id: string; install_id: string };
  env: Env;
}): YutomeAuthProps {
  const pairedAt = new Date();
  const tokenTtlSeconds = configuredTokenTtlSeconds(env);
  return {
    capsule: "owner",
    workspace_id: bridgeIdentity.workspace_id,
    install_id: bridgeIdentity.install_id,
    connector_grant_id: authRequestId,
    grant_id: authRequestId,
    client_id: authRequest.clientId,
    scopes: [...authRequest.scope],
    audience: configuredMcpAudience(env),
    token_version: configuredTokenVersion(env),
    paired_at: pairedAt.toISOString(),
    expires_at: new Date(pairedAt.getTime() + tokenTtlSeconds * 1000).toISOString(),
  };
}

function oauthGrantMetadata(props: YutomeAuthProps): Record<string, unknown> {
  return withoutUndefined({
    workspace_id: props.workspace_id,
    install_id: props.install_id,
    connector_grant_id: props.connector_grant_id,
    grant_id: props.grant_id,
    user_id: props.user_id,
    client_id: props.client_id,
    session_id: props.session_id,
    scopes: props.scopes,
    audience: props.audience,
    token_version: props.token_version,
    paired_at: props.paired_at,
    expires_at: props.expires_at,
  });
}

function configuredTokenVersion(env: Env): string {
  return env.YUTOME_TOKEN_VERSION?.trim() || "v1";
}

function configuredTokenTtlSeconds(env: Env): number {
  const configured = env.YUTOME_TOKEN_TTL_SECONDS?.trim();
  if (!configured) {
    return DEFAULT_OAUTH_TOKEN_TTL_SECONDS;
  }
  const parsed = Number(configured);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return DEFAULT_OAUTH_TOKEN_TTL_SECONDS;
  }
  return Math.floor(parsed);
}

function withoutUndefined<T extends Record<string, unknown>>(value: T): T {
  return Object.fromEntries(
    Object.entries(value).filter(([, entryValue]) => entryValue !== undefined),
  ) as T;
}

/**
 * Renders the pairing form. /authorize provides a short-lived server-side
 * authorization state id plus a CSRF token. Direct /pair visits are still
 * allowed so the URL has a useful explanation, but they cannot approve until
 * an MCP client starts a real OAuth flow.
 */
export function renderForm(url: URL, error: string, authState?: RenderAuthorizationState): Response {
  // The pairing form ALWAYS posts to /pair regardless of which route rendered
  // it. /authorize only handles GET; the POST step lives entirely in
  // handlePairingRequest.
  const action = `/pair${url.search}`;
  const hidden = authState
    ? [
        `<input type="hidden" name="auth_request_id" value="${escapeHtml(authState.authRequestId)}" />`,
        `<input type="hidden" name="csrf_token" value="${escapeHtml(authState.csrfToken)}" />`,
      ].join("\n    ")
    : "";
  const target = authState
    ? `<dl class="target">
        <div><dt>Assistant app</dt><dd>${escapeHtml(authState.clientName || "Unnamed MCP client")}</dd></div>
        <div><dt>Redirect</dt><dd><code>${escapeHtml(authState.redirectUri)}</code></dd></div>
        <div><dt>Scope</dt><dd>${escapeHtml(authState.scope.join(" ") || "none")}</dd></div>
      </dl>`
    : `<p class="hint">Start connector setup from Claude, ChatGPT, or another MCP client. The assistant will open this page during OAuth setup.</p>`;
  const body = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Pair Yutome Remote MCP</title>
  <link rel="icon" type="image/png" sizes="48x48" href="/icon-48.png" />
  <link rel="apple-touch-icon" sizes="256x256" href="/icon.png" />
  <style>
    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.5; margin: 2.5rem auto; max-width: 38rem; padding: 0 1rem; color: #111; }
    .brand { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.5rem; }
    .brand img { width: 48px; height: 48px; }
    .brand h1 { margin: 0; }
    h1 { font-size: 1.4rem; margin-bottom: 0.5rem; }
    label { display: grid; gap: 0.4rem; margin: 1.25rem 0; font-weight: 500; }
    input { padding: 0.7rem; border: 1px solid #999; border-radius: 8px; font: inherit; }
    button { padding: 0.8rem 1.1rem; border: 0; border-radius: 8px; background: #111; color: white; font-weight: 600; font-size: 1rem; }
    code { background: #f3f3f3; padding: 0.1rem 0.35rem; border-radius: 4px; }
    dl { border: 1px solid #ddd; border-radius: 8px; padding: 0.85rem; }
    dl div + div { margin-top: 0.65rem; }
    dt { color: #555; font-size: 0.82rem; }
    dd { margin: 0.1rem 0 0; overflow-wrap: anywhere; }
    .error { color: #9f1239; font-weight: 500; }
    .hint { color: #555; font-size: 0.95rem; }
  </style>
</head>
<body>
  <div class="brand">
    <img src="/icon-48.png" alt="" width="48" height="48" />
    <h1>Pair Yutome with this assistant</h1>
  </div>
  <p class="hint">Claude or ChatGPT wants permission to search this Yutome library while your computer is online.</p>
  <p class="hint">Enter the latest pairing code printed by <code>uv run yutome connect --deploy</code> or saved with <code>uv run yutome connect --endpoint ... --pairing-code ...</code>. No Yutome account is needed.</p>
  <p class="hint">If you reran <code>yutome connect</code> or have several Yutome tabs open, use the newest tab and the newest code.</p>
  ${target}
  ${error ? `<p class="error">${escapeHtml(error)}</p>` : ""}
  <form method="post" action="${escapeHtml(action)}" ${authState ? "" : "hidden"}>
    ${hidden}
    <label>Pairing code
      <input name="pairing_code" autocomplete="one-time-code" autofocus required />
    </label>
    <button type="submit">Approve</button>
  </form>
</body>
</html>
	`;
  const headers = new Headers(securityHeaders());
  if (authState) {
    const cookieName = csrfCookieName(authState.authRequestId);
    if (cookieName) {
      headers.append(
        "set-cookie",
        `${cookieName}=${authState.csrfToken}; Path=/; Max-Age=${AUTH_STATE_TTL_SECONDS}; Secure; HttpOnly; SameSite=Lax`,
      );
    }
  }
  return new Response(body, {
    status: error ? 401 : 200,
    headers,
  });
}

function renderHostedConsentForm(
  url: URL,
  error: string,
  authState: RenderAuthorizationState,
  accountSession: HostedAccountSession,
): Response {
  const action = `/pair${url.search}`;
  const workspaceOptions = accountSession.workspace_ids.map((workspaceId) =>
    `<option value="${escapeHtml(workspaceId)}" ${workspaceId === accountSession.workspace_id ? "selected" : ""}>${escapeHtml(workspaceId)}</option>`,
  ).join("");
  const workspaceControl = accountSession.workspace_ids.length > 1
    ? `<label>Workspace
      <select name="workspace_id" required>${workspaceOptions}</select>
    </label>`
    : `<input type="hidden" name="workspace_id" value="${escapeHtml(accountSession.workspace_id)}" />`;
  const body = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Authorize Yutome Remote MCP</title>
  <link rel="icon" type="image/png" sizes="48x48" href="/icon-48.png" />
  <link rel="apple-touch-icon" sizes="256x256" href="/icon.png" />
  <style>
    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.5; margin: 2.5rem auto; max-width: 38rem; padding: 0 1rem; color: #111; }
    .brand { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.5rem; }
    .brand img { width: 48px; height: 48px; }
    .brand h1 { margin: 0; }
    h1 { font-size: 1.4rem; margin-bottom: 0.5rem; }
    label { display: grid; gap: 0.4rem; margin: 1.25rem 0; font-weight: 500; }
    select { padding: 0.7rem; border: 1px solid #999; border-radius: 8px; font: inherit; background: white; }
    button { padding: 0.8rem 1.1rem; border: 0; border-radius: 8px; background: #111; color: white; font-weight: 600; font-size: 1rem; }
    code { background: #f3f3f3; padding: 0.1rem 0.35rem; border-radius: 4px; }
    dl { border: 1px solid #ddd; border-radius: 8px; padding: 0.85rem; }
    dl div + div { margin-top: 0.65rem; }
    dt { color: #555; font-size: 0.82rem; }
    dd { margin: 0.1rem 0 0; overflow-wrap: anywhere; }
    .error { color: #9f1239; font-weight: 500; }
    .hint { color: #555; font-size: 0.95rem; }
  </style>
</head>
<body>
  <div class="brand">
    <img src="/icon-48.png" alt="" width="48" height="48" />
    <h1>Authorize Yutome for this assistant</h1>
  </div>
  <p class="hint">Claude or ChatGPT wants permission to search a Yutome workspace.</p>
  <dl class="target">
    <div><dt>Assistant app</dt><dd>${escapeHtml(authState.clientName || "Unnamed MCP client")}</dd></div>
    <div><dt>Redirect</dt><dd><code>${escapeHtml(authState.redirectUri)}</code></dd></div>
    <div><dt>Scope</dt><dd>${escapeHtml(authState.scope.join(" ") || "none")}</dd></div>
    <div><dt>User</dt><dd>${escapeHtml(accountSession.user_id)}</dd></div>
  </dl>
  ${error ? `<p class="error">${escapeHtml(error)}</p>` : ""}
  <form method="post" action="${escapeHtml(action)}">
    <input type="hidden" name="auth_request_id" value="${escapeHtml(authState.authRequestId)}" />
    <input type="hidden" name="csrf_token" value="${escapeHtml(authState.csrfToken)}" />
    ${workspaceControl}
    <button type="submit">Approve</button>
  </form>
</body>
</html>
	`;
  const headers = new Headers(securityHeaders());
  const cookieName = csrfCookieName(authState.authRequestId);
  if (cookieName) {
    headers.append(
      "set-cookie",
      `${cookieName}=${authState.csrfToken}; Path=/; Max-Age=${AUTH_STATE_TTL_SECONDS}; Secure; HttpOnly; SameSite=Lax`,
    );
  }
  return new Response(body, {
    status: error ? 401 : 200,
    headers,
  });
}

function errorResponse(message: string, authRequestId?: string, status = 400): Response {
  const response = new Response(message, {
    status,
    headers: securityHeaders("text/plain; charset=utf-8"),
  });
  if (authRequestId) {
    appendClearCsrfCookie(response.headers, authRequestId);
  }
  return response;
}

function redirectResponse(location: string): Response {
  return new Response(null, {
    status: 302,
    headers: { location },
  });
}

async function createAuthorizationState(
  env: Env,
  authRequest: AuthRequest,
  client: ClientInfo | null,
): Promise<RenderAuthorizationState> {
  const authRequestId = crypto.randomUUID();
  const csrfToken = crypto.randomUUID() + crypto.randomUUID();
  const now = Date.now();
  const stored: StoredAuthorizationRequest = {
    authRequest,
    clientName: client?.clientName,
    clientUri: client?.clientUri,
    logoUri: client?.logoUri,
    redirectUri: authRequest.redirectUri,
    scope: authRequest.scope,
    csrfToken,
    expiresAt: now + AUTH_STATE_TTL_SECONDS * 1000,
  };
  await env.OAUTH_KV.put(authStateKey(authRequestId), JSON.stringify(stored), {
    expirationTtl: AUTH_STATE_TTL_SECONDS,
  });
  return toRenderState(authRequestId, stored);
}

async function loadAuthorizationState(env: Env, authRequestId: string): Promise<StoredAuthorizationRequest | null> {
  const raw = await env.OAUTH_KV.get(authStateKey(authRequestId));
  if (!raw) {
    return null;
  }
  const parsed = JSON.parse(raw) as StoredAuthorizationRequest;
  if (!parsed.expiresAt || parsed.expiresAt < Date.now()) {
    await env.OAUTH_KV.delete(authStateKey(authRequestId));
    return null;
  }
  return parsed;
}

function toRenderState(authRequestId: string, state: StoredAuthorizationRequest): RenderAuthorizationState {
  return {
    authRequestId,
    csrfToken: state.csrfToken,
    clientName: state.clientName,
    clientUri: state.clientUri,
    logoUri: state.logoUri,
    redirectUri: state.redirectUri,
    scope: state.scope,
  };
}

function authStateKey(authRequestId: string): string {
  return `${AUTH_STATE_PREFIX}${authRequestId}`;
}

function csrfCookieName(authRequestId: string): string | null {
  if (!AUTH_REQUEST_ID_PATTERN.test(authRequestId)) {
    return null;
  }
  return `${CSRF_COOKIE_PREFIX}${authRequestId}`;
}

function appendClearCsrfCookie(headers: Headers, authRequestId: string): void {
  const cookieName = csrfCookieName(authRequestId);
  if (!cookieName) {
    return;
  }
  headers.append("set-cookie", `${cookieName}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Lax`);
}

function readCookie(cookieHeader: string, name: string): string | null {
  for (const part of cookieHeader.split(";")) {
    const [rawKey, ...rawValue] = part.trim().split("=");
    if (rawKey === name) {
      return rawValue.join("=");
    }
  }
  return null;
}

function securityHeaders(contentType = "text/html; charset=utf-8"): HeadersInit {
  return {
    "content-type": contentType,
    "cache-control": "no-store",
    "content-security-policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
  };
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => {
    switch (char) {
      case "&": return "&amp;";
      case "<": return "&lt;";
      case ">": return "&gt;";
      case '"': return "&quot;";
      case "'": return "&#39;";
      default: return char;
    }
  });
}
