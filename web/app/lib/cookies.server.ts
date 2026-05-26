// The account session cookie is shared with the existing OAuth worker on
// mcp.yutome.com (same registrable domain). Keep this NAME in lockstep with
// ACCOUNT_SESSION_COOKIE_NAME in the Python API (http_api.py) and the worker
// (cloudflare/yutome-capsule/src/account-grants.ts).
export const SESSION_COOKIE_NAME = "yutome_account_session";

export function readSessionCookie(request: Request): string | null {
  const header = request.headers.get("cookie");
  if (!header) return null;
  for (const part of header.split(";")) {
    const trimmed = part.trim();
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    if (trimmed.slice(0, eq) === SESSION_COOKIE_NAME) {
      const value = trimmed.slice(eq + 1);
      try {
        return decodeURIComponent(value) || null;
      } catch {
        return value || null;
      }
    }
  }
  return null;
}

interface CookieOptions {
  domain: string;
  maxAgeSeconds: number;
}

// In production `domain` is "yutome.com" so app.yutome.com sets a cookie that
// mcp.yutome.com can read for the connect handoff (app<->mcp is same-site).
// Secure is paired with Domain; omitted in local dev (host-only, http://localhost),
// where a Domain attribute would otherwise be rejected by the browser.
export function buildSessionCookie(token: string, { domain, maxAgeSeconds }: CookieOptions): string {
  const attrs = [
    `${SESSION_COOKIE_NAME}=${encodeURIComponent(token)}`,
    "Path=/",
    "HttpOnly",
    "SameSite=Lax",
    `Max-Age=${Math.max(0, Math.floor(maxAgeSeconds))}`,
  ];
  if (domain) {
    attrs.push(`Domain=${domain}`, "Secure");
  }
  return attrs.join("; ");
}

export function clearSessionCookie(domain: string): string {
  const attrs = [`${SESSION_COOKIE_NAME}=`, "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"];
  if (domain) {
    attrs.push(`Domain=${domain}`, "Secure");
  }
  return attrs.join("; ");
}
