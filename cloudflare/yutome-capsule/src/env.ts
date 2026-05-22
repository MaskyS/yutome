/**
 * Worker bindings declared in wrangler.toml.
 *
 * Secrets (set via `wrangler secret put`):
 *   YUTOME_RELAY_TOKEN  — bearer token the laptop bridge presents on /relay/connect
 *   YUTOME_PAIRING_CODE — printed by `yutome connect`, consumed at /pair
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
  YUTOME_PAIRING_CODE: string;

  // Vars
  YUTOME_WORKER_MODE: string;
}

/** OAuth grant props attached to each access token. */
export interface YutomeAuthProps extends Record<string, unknown> {
  capsule: "owner";
  paired_at: string;
}
