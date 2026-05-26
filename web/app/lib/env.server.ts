// Server-only access to the validated subset of Cloudflare env bindings the
// dashboard BFF needs. Never import from client component code — the
// `.server.ts` suffix keeps the service token out of the browser bundle.

export interface YutomeWebEnv {
  YUTOME_HOSTED_API_URL: string;
  YUTOME_DASHBOARD_API_TOKEN: string;
  YUTOME_ACCOUNT_SESSION_AUDIENCE: string;
  YUTOME_COOKIE_DOMAIN: string;
  YUTOME_MCP_URL: string;
}

interface CloudflareContext {
  cloudflare: { env: unknown };
}

function required(env: Record<string, unknown>, key: string): string {
  const value = env[key];
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error(`Missing required server env: ${key}`);
  }
  return value;
}

function optional(env: Record<string, unknown>, key: string, fallback = ""): string {
  const value = env[key];
  return typeof value === "string" ? value : fallback;
}

export function getEnv(context: CloudflareContext): YutomeWebEnv {
  const env = (context.cloudflare?.env ?? {}) as Record<string, unknown>;
  return {
    YUTOME_HOSTED_API_URL: required(env, "YUTOME_HOSTED_API_URL"),
    YUTOME_DASHBOARD_API_TOKEN: required(env, "YUTOME_DASHBOARD_API_TOKEN"),
    YUTOME_ACCOUNT_SESSION_AUDIENCE: optional(env, "YUTOME_ACCOUNT_SESSION_AUDIENCE", "yutome:hosted-oauth"),
    YUTOME_COOKIE_DOMAIN: optional(env, "YUTOME_COOKIE_DOMAIN", ""),
    YUTOME_MCP_URL: optional(env, "YUTOME_MCP_URL", "https://mcp.getyutome.com/mcp"),
  };
}
