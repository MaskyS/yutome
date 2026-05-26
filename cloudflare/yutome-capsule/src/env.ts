/**
 * Worker bindings declared in wrangler.toml.
 *
 * Secrets (set via `wrangler secret put`):
 *   YUTOME_RELAY_TOKEN  — bearer token the laptop bridge presents on /relay/connect
 *   YUTOME_PAIRING_CODE — printed by `yutome connect`, consumed at /pair in connector-only mode
 *   YUTOME_HOSTED_API_TOKEN — bearer token used by hosted-mode edge → API calls
 *   YUTOME_ACCOUNT_SESSION_HMAC_SECRET — verifies account-app signed hosted OAuth sessions
 *
 * Vars:
 *   YUTOME_WORKSPACE_ID — optional connector relay routing id
 *   YUTOME_INSTALL_ID   — optional desktop/install routing id
 *   YUTOME_HOSTED_API_URL — Railway/Python hosted MCP query API base URL
 *   YUTOME_ACCOUNT_SESSION_AUDIENCE — optional account-session audience, defaults to yutome:hosted-oauth
 *   YUTOME_ACCOUNT_SESSION_MAX_AGE_SECONDS — optional signed session max age, defaults to 1 hour
 *   YUTOME_ACCOUNT_SESSION_CLOCK_SKEW_SECONDS — optional signed session clock skew, defaults to 60 seconds
 *   YUTOME_MCP_AUDIENCE — optional OAuth audience/resource for hosted MCP tokens
 *   YUTOME_TOKEN_VERSION — optional token contract version, defaults to v1
 *   YUTOME_TOKEN_TTL_SECONDS — optional token prop expiry hint, defaults to 30 days
 *
 * KV (for workers-oauth-provider state):
 *   OAUTH_KV — create with `wrangler kv namespace create OAUTH_KV` and bind in wrangler.toml
 */
export interface Env {
  // Durable Object namespaces
  RELAY: DurableObjectNamespace;
  MCP_OBJECT: DurableObjectNamespace;

  // KV for OAuth provider state
  OAUTH_KV: KVNamespace;

  // Secrets
  YUTOME_RELAY_TOKEN: string;
  YUTOME_PAIRING_CODE?: string;
  YUTOME_HOSTED_API_TOKEN?: string;
  YUTOME_ACCOUNT_SESSION_HMAC_SECRET?: string;

  // Vars
  YUTOME_WORKER_MODE: string;
  YUTOME_WORKSPACE_ID?: string;
  YUTOME_INSTALL_ID?: string;
  YUTOME_HOSTED_API_URL?: string;
  YUTOME_ACCOUNT_SESSION_AUDIENCE?: string;
  YUTOME_ACCOUNT_SESSION_MAX_AGE_SECONDS?: string;
  YUTOME_ACCOUNT_SESSION_CLOCK_SKEW_SECONDS?: string;
  // Parent domain (e.g. "yutome.com") so the account-session cookie set here is
  // shared with app.yutome.com. Unset = host-only (single-host / local dev).
  YUTOME_COOKIE_DOMAIN?: string;
  YUTOME_MCP_AUDIENCE?: string;
  YUTOME_TOKEN_VERSION?: string;
  YUTOME_TOKEN_TTL_SECONDS?: string;
}

/** OAuth grant props attached to each access token. */
export interface YutomeAuthProps extends Record<string, unknown> {
  capsule?: "owner" | "hosted";
  workspace_id: string;
  install_id?: string;
  connector_grant_id?: string;
  grant_id?: string;
  user_id?: string;
  client_id?: string;
  session_id?: string;
  scopes?: string[];
  audience?: string;
  expires_at?: string | number;
  token_version?: string;
  paired_at?: string;
}
