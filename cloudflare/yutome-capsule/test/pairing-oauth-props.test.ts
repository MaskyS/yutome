import assert from "node:assert/strict";
import test from "node:test";
import { handlePairingRequest } from "../src/pairing.ts";
import type { Env } from "../src/env.ts";

const AUTH_STATE_PREFIX = "yutome:pairing:auth:";

test("OAuth props carry hosted tenant ids without provider credentials", async () => {
  const grantId = "11111111-2222-4333-8444-555555555555";
  const csrfToken = "csrf-token";
  const kv = new MemoryKv();
  await kv.put(
    `${AUTH_STATE_PREFIX}${grantId}`,
    JSON.stringify({
      authRequest: {
        clientId: "client-1",
        redirectUri: "https://assistant.example/callback",
        scope: ["yutome.search.read"],
      },
      redirectUri: "https://assistant.example/callback",
      scope: ["yutome.search.read"],
      csrfToken,
      expiresAt: Date.now() + 60_000,
    }),
  );

  let completed: unknown;
  const env = {
    OAUTH_KV: kv as unknown as KVNamespace,
    YUTOME_PAIRING_CODE: "PAIR-123",
    YUTOME_RELAY_TOKEN: "bridge-token",
    YUTOME_WORKER_MODE: "hosted",
    YUTOME_WORKSPACE_ID: "ws_alice",
    YUTOME_INSTALL_ID: "inst_mac",
    RELAY: {} as DurableObjectNamespace,
    MCP_OBJECT: {} as DurableObjectNamespace,
    GEMINI_API_KEY: "provider-secret",
    OPENAI_API_KEY: "provider-secret",
    WEB_SHARE_TOKEN: "provider-secret",
  } as Env & Record<string, unknown>;

  const response = await handlePairingRequest({
    request: pairingPostRequest(grantId, csrfToken, "PAIR-123"),
    env,
    oauthHelpers: {
      completeAuthorization: async (input: unknown) => {
        completed = input;
        return { redirectTo: "https://assistant.example/done" };
      },
    } as never,
  });

  assert.equal(response.status, 302);
  assert.equal(response.headers.get("location"), "https://assistant.example/done");

  const authorization = completed as {
    metadata: Record<string, unknown>;
    props: Record<string, unknown>;
  };
  assert.deepEqual(authorization.props, {
    capsule: "owner",
    workspace_id: "ws_alice",
    install_id: "inst_mac",
    connector_grant_id: grantId,
    paired_at: authorization.props.paired_at,
  });
  assert.equal(typeof authorization.props.paired_at, "string");
  assert.deepEqual(authorization.metadata, {
    workspace_id: "ws_alice",
    install_id: "inst_mac",
    connector_grant_id: grantId,
    paired_at: authorization.props.paired_at,
  });

  for (const credentialKey of ["GEMINI_API_KEY", "OPENAI_API_KEY", "WEB_SHARE_TOKEN"]) {
    assert.equal(credentialKey in authorization.props, false);
    assert.equal(credentialKey in authorization.metadata, false);
  }
});

test("hosted OAuth pairing rejects missing configured tenant identity", async () => {
  const grantId = "22222222-3333-4444-8555-666666666666";
  const csrfToken = "csrf-token";
  const kv = new MemoryKv();
  await kv.put(
    `${AUTH_STATE_PREFIX}${grantId}`,
    JSON.stringify({
      authRequest: {
        clientId: "client-1",
        redirectUri: "https://assistant.example/callback",
        scope: ["yutome.search.read"],
      },
      redirectUri: "https://assistant.example/callback",
      scope: ["yutome.search.read"],
      csrfToken,
      expiresAt: Date.now() + 60_000,
    }),
  );

  let completed = false;
  const env = {
    OAUTH_KV: kv as unknown as KVNamespace,
    YUTOME_PAIRING_CODE: "PAIR-123",
    YUTOME_RELAY_TOKEN: "bridge-token",
    YUTOME_WORKER_MODE: "hosted",
    RELAY: {} as DurableObjectNamespace,
    MCP_OBJECT: {} as DurableObjectNamespace,
  } as Env;

  const response = await handlePairingRequest({
    request: pairingPostRequest(grantId, csrfToken, "PAIR-123"),
    env,
    oauthHelpers: {
      completeAuthorization: async () => {
        completed = true;
        return { redirectTo: "https://assistant.example/done" };
      },
    } as never,
  });

  assert.equal(response.status, 400);
  assert.equal(completed, false);
  assert.match(await response.text(), /Missing hosted tenant identity/);
});

test("connector-only OAuth pairing keeps local defaults explicit", async () => {
  const grantId = "33333333-4444-4555-8666-777777777777";
  const csrfToken = "csrf-token";
  const kv = new MemoryKv();
  await kv.put(
    `${AUTH_STATE_PREFIX}${grantId}`,
    JSON.stringify({
      authRequest: {
        clientId: "client-1",
        redirectUri: "https://assistant.example/callback",
        scope: ["yutome.search.read"],
      },
      redirectUri: "https://assistant.example/callback",
      scope: ["yutome.search.read"],
      csrfToken,
      expiresAt: Date.now() + 60_000,
    }),
  );

  let completed: unknown;
  const env = {
    OAUTH_KV: kv as unknown as KVNamespace,
    YUTOME_PAIRING_CODE: "PAIR-123",
    YUTOME_RELAY_TOKEN: "bridge-token",
    YUTOME_WORKER_MODE: "connector_only",
    RELAY: {} as DurableObjectNamespace,
    MCP_OBJECT: {} as DurableObjectNamespace,
  } as Env;

  const response = await handlePairingRequest({
    request: pairingPostRequest(grantId, csrfToken, "PAIR-123"),
    env,
    oauthHelpers: {
      completeAuthorization: async (input: unknown) => {
        completed = input;
        return { redirectTo: "https://assistant.example/done" };
      },
    } as never,
  });

  const authorization = completed as { props: Record<string, unknown> };
  assert.equal(response.status, 302);
  assert.equal(authorization.props.workspace_id, "local");
  assert.equal(authorization.props.install_id, "desktop");
});

function pairingPostRequest(authRequestId: string, csrfToken: string, pairingCode: string): Request {
  const form = new URLSearchParams({
    auth_request_id: authRequestId,
    csrf_token: csrfToken,
    pairing_code: pairingCode,
  });
  return new Request("https://mcp.yutome.com/pair", {
    method: "POST",
    headers: {
      "content-type": "application/x-www-form-urlencoded",
      cookie: `__Host-yutome_pairing_${authRequestId}=${csrfToken}`,
    },
    body: form,
  });
}

class MemoryKv {
  private readonly values = new Map<string, string>();

  async get(key: string): Promise<string | null> {
    return this.values.get(key) ?? null;
  }

  async put(key: string, value: string): Promise<void> {
    this.values.set(key, value);
  }

  async delete(key: string): Promise<void> {
    this.values.delete(key);
  }
}
