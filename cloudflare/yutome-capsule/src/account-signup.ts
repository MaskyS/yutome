import type { Env } from "./env";
import { ACCOUNT_SESSION_COOKIE_NAME } from "./account-grants.ts";
import { isHostedWorkerMode } from "./tenant-routing.ts";

interface AccountBootstrapRequest {
  email: string;
  name: string;
  workspace_name: string;
}

export async function handleAccountSignupRequest(
  request: Request,
  env: Env,
  fetcher: typeof fetch = fetch,
): Promise<Response> {
  if (!isHostedWorkerMode(env.YUTOME_WORKER_MODE)) {
    return new Response("Not Found", {
      status: 404,
      headers: { "cache-control": "no-store" },
    });
  }

  if (request.method === "GET") {
    return renderSignupForm(new URL(request.url), "");
  }
  if (request.method !== "POST") {
    return new Response("Method Not Allowed", {
      status: 405,
      headers: {
        allow: "GET, POST",
        "cache-control": "no-store",
      },
    });
  }

  const form = await request.formData();
  const returnTo = safeReturnTo(String(form.get("return_to") || ""), "/authorize");
  const bootstrapRequest: AccountBootstrapRequest = {
    email: String(form.get("email") || "").trim(),
    name: String(form.get("name") || "").trim(),
    workspace_name: String(form.get("workspace_name") || "").trim(),
  };
  const validationError = validateBootstrapRequest(bootstrapRequest);
  if (validationError) {
    return renderSignupForm(new URL(request.url), validationError, returnTo, bootstrapRequest);
  }

  const bootstrap = await callAccountBootstrap(env, bootstrapRequest, fetcher);
  if (bootstrap instanceof Response) {
    return bootstrap;
  }

  const cookieMaxAge = sessionCookieMaxAge(bootstrap.session.expires_at);
  if (cookieMaxAge === null) {
    return hostedApiErrorResponse(
      502,
      "invalid_hosted_api_response",
      "Hosted account bootstrap response did not include a valid session expiry.",
    );
  }

  const headers = new Headers({
    location: returnTo,
    "cache-control": "no-store",
  });
  // Scope to the parent domain (e.g. yutome.com) so app.yutome.com and
  // mcp.yutome.com share one session; host-only when unset (local/single-host).
  const cookieDomain = env.YUTOME_COOKIE_DOMAIN?.trim();
  const domainAttr = cookieDomain ? `; Domain=${cookieDomain}` : "";
  headers.append(
    "set-cookie",
    `${ACCOUNT_SESSION_COOKIE_NAME}=${encodeURIComponent(bootstrap.session.token)}; HttpOnly; Secure; SameSite=Lax; Path=/${domainAttr}; Max-Age=${cookieMaxAge}`,
  );
  return new Response(null, {
    status: 302,
    headers,
  });
}

export function safeReturnTo(value: string | null | undefined, fallback = "/authorize"): string {
  const raw = String(value || "").trim();
  if (!raw) {
    return fallback;
  }
  if (!raw.startsWith("/") || raw.startsWith("//") || /[\r\n\\]/.test(raw) || raw.includes("#")) {
    return fallback;
  }
  try {
    const parsed = new URL(raw, "https://mcp.yutome.local");
    if (parsed.origin !== "https://mcp.yutome.local") {
      return fallback;
    }
    return `${parsed.pathname}${parsed.search}`;
  } catch {
    return fallback;
  }
}

export function signupRedirectResponse(request: Request): Response {
  const url = new URL(request.url);
  const returnTo = `${url.pathname}${url.search}`;
  return new Response(null, {
    status: 302,
    headers: {
      location: `/account/signup?return_to=${encodeURIComponent(returnTo)}`,
      "cache-control": "no-store",
    },
  });
}

function renderSignupForm(
  url: URL,
  error: string,
  explicitReturnTo?: string,
  values: Partial<AccountBootstrapRequest> = {},
): Response {
  const returnTo = safeReturnTo(explicitReturnTo ?? url.searchParams.get("return_to"), "/authorize");
  const body = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Sign up for Yutome</title>
  <style>
    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.5; margin: 2.5rem auto; max-width: 34rem; padding: 0 1rem; color: #111; }
    h1 { font-size: 1.4rem; margin-bottom: 0.5rem; }
    label { display: grid; gap: 0.4rem; margin: 1.1rem 0; font-weight: 500; }
    input { padding: 0.7rem; border: 1px solid #999; border-radius: 8px; font: inherit; }
    button { padding: 0.8rem 1.1rem; border: 0; border-radius: 8px; background: #111; color: white; font-weight: 600; font-size: 1rem; }
    .error { color: #9f1239; font-weight: 500; }
    .hint { color: #555; font-size: 0.95rem; }
  </style>
</head>
<body>
  <h1>Create your Yutome account</h1>
  <p class="hint">Set up a hosted workspace before authorizing Yutome MCP access.</p>
  ${error ? `<p class="error">${escapeHtml(error)}</p>` : ""}
  <form method="post" action="/account/signup">
    <input type="hidden" name="return_to" value="${escapeHtml(returnTo)}" />
    <label>Email
      <input name="email" type="email" autocomplete="email" value="${escapeHtml(values.email || "")}" required autofocus />
    </label>
    <label>Name
      <input name="name" autocomplete="name" value="${escapeHtml(values.name || "")}" required />
    </label>
    <label>Workspace name
      <input name="workspace_name" autocomplete="organization" value="${escapeHtml(values.workspace_name || "")}" required />
    </label>
    <button type="submit">Continue</button>
  </form>
</body>
</html>
`;
  return new Response(body, {
    status: error ? 400 : 200,
    headers: securityHeaders(),
  });
}

async function callAccountBootstrap(
  env: Env,
  body: AccountBootstrapRequest,
  fetcher: typeof fetch,
): Promise<AccountBootstrapResponse | Response> {
  const endpoint = accountBootstrapEndpoint(env);
  if (endpoint instanceof Response) {
    return endpoint;
  }
  const token = env.YUTOME_HOSTED_API_TOKEN?.trim();
  if (!token) {
    return hostedApiErrorResponse(500, "hosted_api_token_missing", "YUTOME_HOSTED_API_TOKEN is required in hosted worker mode.");
  }

  const response = await fetcher(endpoint, {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
      accept: "application/json",
    },
    body: JSON.stringify(body),
  });
  const payload = await responseJsonObject(response);
  if (!response.ok || payload.ok === false) {
    return hostedApiErrorResponse(response.status || 502, stringField(payload, "error") || "hosted_api_error", errorMessage(payload));
  }
  const parsed = parseBootstrapResponse(payload);
  if (!parsed) {
    return hostedApiErrorResponse(502, "invalid_hosted_api_response", "Hosted account bootstrap response did not include a session token.");
  }
  return parsed;
}

function accountBootstrapEndpoint(env: Env): string | Response {
  const rawUrl = env.YUTOME_HOSTED_API_URL?.trim();
  if (!rawUrl) {
    return hostedApiErrorResponse(500, "hosted_api_url_missing", "YUTOME_HOSTED_API_URL is required in hosted worker mode.");
  }
  try {
    const base = new URL(rawUrl);
    const pathname = base.pathname.replace(/\/+$/g, "");
    base.pathname = `${pathname}/account/bootstrap`;
    base.search = "";
    base.hash = "";
    return base.toString();
  } catch {
    return hostedApiErrorResponse(500, "hosted_api_url_invalid", "YUTOME_HOSTED_API_URL must be an absolute URL.");
  }
}

interface AccountBootstrapResponse {
  ok: true;
  principal: unknown;
  session: {
    token: string;
    expires_at: string;
    audience: string;
    cookie_name: string;
  };
}

function parseBootstrapResponse(payload: Record<string, unknown>): AccountBootstrapResponse | null {
  const session = recordField(payload, "session");
  const token = stringField(session, "token");
  const expiresAt = stringField(session, "expires_at");
  const audience = stringField(session, "audience");
  const cookieName = stringField(session, "cookie_name") || ACCOUNT_SESSION_COOKIE_NAME;
  if (payload.ok !== true || !session || !token || !expiresAt || !audience) {
    return null;
  }
  return {
    ok: true,
    principal: payload.principal,
    session: {
      token,
      expires_at: expiresAt,
      audience,
      cookie_name: cookieName,
    },
  };
}

function validateBootstrapRequest(value: AccountBootstrapRequest): string | null {
  if (!value.email || !value.email.includes("@")) {
    return "Enter a valid email address.";
  }
  if (!value.name) {
    return "Enter your name.";
  }
  if (!value.workspace_name) {
    return "Enter a workspace name.";
  }
  return null;
}

function sessionCookieMaxAge(expiresAt: string): number | null {
  const expiresAtMs = Date.parse(expiresAt);
  if (!Number.isFinite(expiresAtMs)) {
    return null;
  }
  const seconds = Math.floor((expiresAtMs - Date.now()) / 1000);
  return seconds > 0 ? seconds : null;
}

async function responseJsonObject(response: Response): Promise<Record<string, unknown>> {
  const text = await response.text();
  if (!text) {
    return {};
  }
  try {
    const parsed = JSON.parse(text);
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed) ? parsed as Record<string, unknown> : {};
  } catch {
    return {};
  }
}

function hostedApiErrorResponse(status: number, error: string, message: string): Response {
  return Response.json(
    { ok: false, error, message },
    {
      status,
      headers: { "cache-control": "no-store" },
    },
  );
}

function errorMessage(payload: Record<string, unknown>): string {
  return stringField(payload, "message") || stringField(payload, "detail") || "Hosted account bootstrap failed.";
}

function recordField(value: Record<string, unknown> | null | undefined, field: string): Record<string, unknown> | null {
  const entry = value?.[field];
  return typeof entry === "object" && entry !== null && !Array.isArray(entry) ? entry as Record<string, unknown> : null;
}

function stringField(value: Record<string, unknown> | null | undefined, field: string): string | null {
  const entry = value?.[field];
  return typeof entry === "string" && entry.trim() ? entry.trim() : null;
}

function securityHeaders(contentType = "text/html; charset=utf-8"): HeadersInit {
  return {
    "content-type": contentType,
    "cache-control": "no-store",
    "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
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
