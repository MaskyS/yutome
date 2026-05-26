import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { handlePairingRequest } from "../src/pairing.ts";
import type { Env } from "../src/env.ts";
import {
  accountGrantKey,
  handleRevokeRequest,
  HostedAccountGrantError,
  readHostedAccountGrant,
  revokeHostedAccountGrant,
  resolveActiveHostedAccountGrantFromProps,
  resolveHostedMcpAuthContextFromStoredGrant,
} from "../src/account-grants.ts";
import { HostedMcpApiClient, resolveHostedMcpAuthContext } from "../src/tenant-routing.ts";

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
        resource: "https://mcp.yutome.com/mcp",
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
    YUTOME_ACCOUNT_USER_ID: "user_alice",
    YUTOME_MCP_AUDIENCE: "https://mcp.yutome.com/mcp",
    YUTOME_TOKEN_VERSION: "v2",
    YUTOME_TOKEN_TTL_SECONDS: "3600",
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
    userId: string;
    metadata: Record<string, unknown>;
    props: Record<string, unknown>;
  };
  assert.deepEqual(authorization.props, {
    capsule: "hosted",
    workspace_id: "ws_alice",
    install_id: "inst_mac",
    connector_grant_id: grantId,
    grant_id: grantId,
    user_id: "user_alice",
    client_id: "client-1",
    scopes: ["yutome.search.read"],
    audience: "https://mcp.yutome.com/mcp",
    token_version: "v2",
    paired_at: authorization.props.paired_at,
    expires_at: authorization.props.expires_at,
  });
  assert.equal(typeof authorization.props.paired_at, "string");
  assert.equal(typeof authorization.props.expires_at, "string");
  assert.equal(Number.isNaN(Date.parse(authorization.props.paired_at as string)), false);
  assert.equal(Number.isNaN(Date.parse(authorization.props.expires_at as string)), false);
  assert.equal(Date.parse(authorization.props.expires_at as string) > Date.parse(authorization.props.paired_at as string), true);
  assert.deepEqual(authorization.metadata, {
    workspace_id: "ws_alice",
    install_id: "inst_mac",
    connector_grant_id: grantId,
    grant_id: grantId,
    user_id: "user_alice",
    client_id: "client-1",
    scopes: ["yutome.search.read"],
    audience: "https://mcp.yutome.com/mcp",
    token_version: "v2",
    paired_at: authorization.props.paired_at,
    expires_at: authorization.props.expires_at,
  });

  assert.deepEqual(resolveHostedMcpAuthContext(authorization.props, { requiredScope: "yutome.search.read" }), {
    workspace_id: "ws_alice",
    scopes: ["yutome.search.read"],
    user_id: "user_alice",
    grant_id: grantId,
    client_id: "client-1",
    audience: "https://mcp.yutome.com/mcp",
    expires_at: authorization.props.expires_at,
    token_version: "v2",
  });
  assert.equal(authorization.userId, "user_alice");

  const persistedGrant = await readHostedAccountGrant(env, grantId);
  assert.deepEqual(persistedGrant, {
    grant_id: grantId,
    user_id: "user_alice",
    workspace_id: "ws_alice",
    client_id: "client-1",
    scopes: ["yutome.search.read"],
    audience: "https://mcp.yutome.com/mcp",
    token_version: "v2",
    status: "active",
    created_at: authorization.props.paired_at,
    updated_at: authorization.props.paired_at,
    expires_at: authorization.props.expires_at,
  });

  await revokeHostedAccountGrant(env, grantId, new Date("2026-05-26T12:00:00.000Z"));
  let revokedError: unknown;
  try {
    await resolveActiveHostedAccountGrantFromProps(env, authorization.props);
  } catch (err) {
    revokedError = err;
  }
  assert.equal(
    revokedError instanceof Error &&
      revokedError.name === "HostedAccountGrantError" &&
      /revoked/.test(revokedError.message),
    true,
  );

  for (const credentialKey of ["GEMINI_API_KEY", "OPENAI_API_KEY", "WEB_SHARE_TOKEN"]) {
    assert.equal(credentialKey in authorization.props, false);
    assert.equal(credentialKey in authorization.metadata, false);
  }
});

test("hosted OAuth pairing rejects revoked staged account grant", async () => {
  const grantId = "44444444-5555-4666-8777-888888888888";
  const csrfToken = "csrf-token";
  const kv = new MemoryKv();
  await kv.put(
    `${AUTH_STATE_PREFIX}${grantId}`,
    JSON.stringify({
      authRequest: {
        clientId: "client-1",
        redirectUri: "https://assistant.example/callback",
        scope: ["yutome.search.read"],
        resource: "https://mcp.yutome.com/mcp",
      },
      redirectUri: "https://assistant.example/callback",
      scope: ["yutome.search.read"],
      csrfToken,
      expiresAt: Date.now() + 60_000,
    }),
  );
  await kv.put(
    accountGrantKey(grantId),
    JSON.stringify({
      grant_id: grantId,
      user_id: "user_alice",
      workspace_id: "ws_alice",
      client_id: "client-1",
      scopes: ["yutome.search.read"],
      audience: "https://mcp.yutome.com/mcp",
      token_version: "v1",
      status: "revoked",
      created_at: "2026-05-26T10:00:00.000Z",
      updated_at: "2026-05-26T11:00:00.000Z",
      revoked_at: "2026-05-26T11:00:00.000Z",
    }),
  );

  let completed = false;
  const env = {
    OAUTH_KV: kv as unknown as KVNamespace,
    YUTOME_PAIRING_CODE: "PAIR-123",
    YUTOME_RELAY_TOKEN: "bridge-token",
    YUTOME_WORKER_MODE: "hosted",
    YUTOME_WORKSPACE_ID: "ws_alice",
    YUTOME_INSTALL_ID: "inst_mac",
    YUTOME_ACCOUNT_USER_ID: "user_alice",
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

  assert.equal(response.status, 403);
  assert.equal(completed, false);
  assert.match(await response.text(), /revoked/);
});

test("hosted OAuth pairing rejects expired staged account grant", async () => {
  const grantId = "77777777-8888-4999-aaaa-bbbbbbbbbbbb";
  const csrfToken = "csrf-token";
  const kv = new MemoryKv();
  await kv.put(
    `${AUTH_STATE_PREFIX}${grantId}`,
    JSON.stringify({
      authRequest: {
        clientId: "client-1",
        redirectUri: "https://assistant.example/callback",
        scope: ["yutome.search.read"],
        resource: "https://mcp.yutome.com/mcp",
      },
      redirectUri: "https://assistant.example/callback",
      scope: ["yutome.search.read"],
      csrfToken,
      expiresAt: Date.now() + 60_000,
    }),
  );
  await kv.put(
    accountGrantKey(grantId),
    JSON.stringify({
      grant_id: grantId,
      user_id: "user_alice",
      workspace_id: "ws_alice",
      client_id: "client-1",
      scopes: ["yutome.search.read"],
      audience: "https://mcp.yutome.com/mcp",
      token_version: "v1",
      status: "active",
      created_at: "2000-01-01T00:00:00.000Z",
      updated_at: "2000-01-01T00:00:00.000Z",
      expires_at: "2000-01-02T00:00:00.000Z",
    }),
  );

  let completed = false;
  const env = {
    OAUTH_KV: kv as unknown as KVNamespace,
    YUTOME_PAIRING_CODE: "PAIR-123",
    YUTOME_RELAY_TOKEN: "bridge-token",
    YUTOME_WORKER_MODE: "hosted",
    YUTOME_WORKSPACE_ID: "ws_alice",
    YUTOME_INSTALL_ID: "inst_mac",
    YUTOME_ACCOUNT_USER_ID: "user_alice",
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

  assert.equal(response.status, 401);
  assert.equal(completed, false);
  assert.match(await response.text(), /expired/);
});

test("hosted account grant resolver rejects expired stored grants and accepts future grants", async () => {
  const expiredGrantId = "88888888-9999-4aaa-bbbb-cccccccccccc";
  const activeGrantId = "99999999-aaaa-4bbb-cccc-dddddddddddd";
  const kv = new MemoryKv();
  const env = {
    OAUTH_KV: kv as unknown as KVNamespace,
    YUTOME_PAIRING_CODE: "PAIR-123",
    YUTOME_RELAY_TOKEN: "bridge-token",
    YUTOME_WORKER_MODE: "hosted",
    RELAY: {} as DurableObjectNamespace,
    MCP_OBJECT: {} as DurableObjectNamespace,
  } as Env;

  await kv.put(
    accountGrantKey(expiredGrantId),
    JSON.stringify({
      grant_id: expiredGrantId,
      user_id: "user_alice",
      workspace_id: "ws_alice",
      client_id: "client-1",
      scopes: ["yutome.search.read"],
      audience: "https://mcp.yutome.com/mcp",
      token_version: "v1",
      status: "active",
      created_at: "2000-01-01T00:00:00.000Z",
      updated_at: "2000-01-01T00:00:00.000Z",
      expires_at: "2000-01-02T00:00:00.000Z",
    }),
  );
  await kv.put(
    accountGrantKey(activeGrantId),
    JSON.stringify({
      grant_id: activeGrantId,
      user_id: "user_alice",
      workspace_id: "ws_alice",
      client_id: "client-1",
      scopes: ["yutome.search.read"],
      audience: "https://mcp.yutome.com/mcp",
      token_version: "v1",
      status: "active",
      created_at: "2999-01-01T00:00:00.000Z",
      updated_at: "2999-01-01T00:00:00.000Z",
      expires_at: "2999-01-02T00:00:00.000Z",
    }),
  );

  let expiredError: unknown;
  try {
    await resolveActiveHostedAccountGrantFromProps(env, {
      workspace_id: "ws_alice",
      grant_id: expiredGrantId,
      scopes: ["yutome.search.read"],
    });
  } catch (err) {
    expiredError = err;
  }
  assert.equal(expiredError instanceof HostedAccountGrantError, true);
  assert.equal((expiredError as HostedAccountGrantError).code, "hosted_grant_expired");
  assert.equal((expiredError as HostedAccountGrantError).status, 401);

  const active = await resolveActiveHostedAccountGrantFromProps(env, {
    workspace_id: "ws_alice",
    grant_id: activeGrantId,
    scopes: ["yutome.search.read"],
  });
  assert.equal(active.grant_id, activeGrantId);
  assert.equal(active.status, "active");
  assert.equal(active.expires_at, "2999-01-02T00:00:00.000Z");
});

test("hosted grant auth rejects token prop mismatches before hosted routing", async () => {
  const grantId = "aaaaaaa1-bbbb-4ccc-8ddd-eeeeeeeeeeee";
  const kv = new MemoryKv();
  const env = {
    OAUTH_KV: kv as unknown as KVNamespace,
    YUTOME_PAIRING_CODE: "PAIR-123",
    YUTOME_RELAY_TOKEN: "bridge-token",
    YUTOME_WORKER_MODE: "hosted",
    YUTOME_HOSTED_API_URL: "https://hosted.example",
    YUTOME_HOSTED_API_TOKEN: "edge-secret",
    RELAY: {} as DurableObjectNamespace,
    MCP_OBJECT: {} as DurableObjectNamespace,
  } as Env;

  await kv.put(
    accountGrantKey(grantId),
    JSON.stringify({
      grant_id: grantId,
      user_id: "user_stored",
      workspace_id: "ws_stored",
      client_id: "client_stored",
      scopes: ["profile", "yutome.search.read"],
      audience: "https://mcp.yutome.com/mcp",
      token_version: "v2",
      status: "active",
      created_at: "2999-01-01T00:00:00.000Z",
      updated_at: "2999-01-01T00:00:00.000Z",
      expires_at: "2999-01-02T00:00:00.000Z",
    }),
  );

  let routed = false;
  const client = new HostedMcpApiClient(
    env,
    (async () => {
      routed = true;
      return Response.json({ ok: true, result: {} });
    }) as typeof fetch,
  );

  let caught: unknown;
  try {
    const auth = await resolveHostedMcpAuthContextFromStoredGrant(
      env,
      {
        grant_id: grantId,
        workspace_id: "ws_forged",
        user_id: "user_forged",
        client_id: "client_forged",
        scopes: ["profile"],
        audience: "https://attacker.example/mcp",
        token_version: "v0",
      },
      { requiredScope: "yutome.search.read" },
    );
    await client.callTool(auth, "find", { text: "must not route" });
  } catch (err) {
    caught = err;
  }

  assert.equal(caught instanceof HostedAccountGrantError, true);
  assert.equal((caught as HostedAccountGrantError).code, "hosted_grant_mismatch");
  assert.match((caught as Error).message, /workspace_id/);
  assert.match((caught as Error).message, /user_id/);
  assert.match((caught as Error).message, /client_id/);
  assert.match((caught as Error).message, /scopes/);
  assert.match((caught as Error).message, /audience/);
  assert.match((caught as Error).message, /token_version/);
  assert.equal(routed, false);
});

test("hosted grant auth derives routing from stored grant when token only identifies the grant", async () => {
  const grantId = "bbbbbbb2-cccc-4ddd-8eee-ffffffffffff";
  const kv = new MemoryKv();
  const env = {
    OAUTH_KV: kv as unknown as KVNamespace,
    YUTOME_PAIRING_CODE: "PAIR-123",
    YUTOME_RELAY_TOKEN: "bridge-token",
    YUTOME_WORKER_MODE: "hosted",
    YUTOME_HOSTED_API_URL: "https://hosted.example/mcp",
    YUTOME_HOSTED_API_TOKEN: "edge-secret",
    RELAY: {} as DurableObjectNamespace,
    MCP_OBJECT: {} as DurableObjectNamespace,
  } as Env;

  await kv.put(
    accountGrantKey(grantId),
    JSON.stringify({
      grant_id: grantId,
      user_id: "user_stored",
      workspace_id: "ws_stored",
      client_id: "client_stored",
      scopes: ["profile", "yutome.search.read"],
      audience: "https://mcp.yutome.com/mcp",
      token_version: "v2",
      status: "active",
      created_at: "2999-01-01T00:00:00.000Z",
      updated_at: "2999-01-01T00:00:00.000Z",
      expires_at: "2999-01-02T00:00:00.000Z",
    }),
  );

  const auth = await resolveHostedMcpAuthContextFromStoredGrant(
    env,
    { grant_id: grantId },
    { requiredScope: "yutome.search.read", sessionId: "session_stored" },
  );
  assert.deepEqual(auth, {
    workspace_id: "ws_stored",
    scopes: ["profile", "yutome.search.read"],
    user_id: "user_stored",
    grant_id: grantId,
    client_id: "client_stored",
    session_id: "session_stored",
    audience: "https://mcp.yutome.com/mcp",
    expires_at: "2999-01-02T00:00:00.000Z",
    token_version: "v2",
  });

  const calls: Array<{ url: string; init: RequestInit }> = [];
  const client = new HostedMcpApiClient(
    env,
    (async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init: init ?? {} });
      return Response.json({ ok: true, result: { rows: [] } });
    }) as typeof fetch,
  );
  await client.callTool(auth, "find", { text: "stored route" });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://hosted.example/mcp/tools/call");
  const headers = new Headers(calls[0].init.headers);
  assert.equal(headers.get("x-yutome-workspace-id"), "ws_stored");
  assert.equal(headers.get("x-yutome-user-id"), "user_stored");
  assert.equal(headers.get("x-yutome-client-id"), "client_stored");
  assert.equal(headers.get("x-yutome-grant-id"), grantId);
  assert.equal(headers.get("x-yutome-session-id"), "session_stored");
  assert.equal(headers.get("x-yutome-scopes"), "profile yutome.search.read");
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
        resource: "https://mcp.yutome.com/mcp",
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

  const authorization = completed as { userId: string; props: Record<string, unknown> };
  assert.equal(response.status, 302);
  assert.equal(authorization.userId, "yutome-owner");
  assert.equal(authorization.props.capsule, "owner");
  assert.equal(authorization.props.workspace_id, "local");
  assert.equal(authorization.props.install_id, "desktop");
  assert.equal(authorization.props.connector_grant_id, grantId);
  assert.equal(authorization.props.grant_id, grantId);
  assert.equal(authorization.props.client_id, "client-1");
  assert.deepEqual(authorization.props.scopes, ["yutome.search.read"]);
  assert.equal(authorization.props.audience, "https://mcp.yutome.com/mcp");
  assert.equal(authorization.props.token_version, "v1");
  assert.equal(typeof authorization.props.expires_at, "string");
});

test("OAuth metadata advertises canonical MCP resource, scope, DCR, S256 PKCE, and revoke support", async () => {
  const source = await readFile("src/index.ts", "utf8");
  assert.match(source, /authorizeEndpoint:\s*"\/authorize"/);
  assert.match(source, /tokenEndpoint:\s*"\/token"/);
  assert.match(source, /clientRegistrationEndpoint:\s*"\/register"/);
  assert.match(source, /allowPlainPKCE:\s*false/);
  assert.match(source, /scopesSupported:\s*\[YUTOME_MCP_SCOPE\]/);
  assert.match(source, /resourceMetadata:\s*{/);
  assert.match(source, /resource:\s*DEFAULT_MCP_AUDIENCE/);
  assert.match(source, /authorization_servers:\s*\["https:\/\/mcp\.yutome\.com"\]/);
  assert.match(source, /scopes_supported:\s*\[YUTOME_MCP_SCOPE\]/);
  assert.match(source, /bearer_methods_supported:\s*\["header"\]/);
  assert.match(source, /resource_name:\s*"Yutome MCP"/);
  assert.match(source, /url\.pathname === "\/revoke"/);
});

test("hosted OAuth pairing rejects authorization requests missing canonical MCP resource", async () => {
  const grantId = "55555555-6666-4777-8888-999999999999";
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
    YUTOME_WORKSPACE_ID: "ws_alice",
    YUTOME_INSTALL_ID: "inst_mac",
    YUTOME_ACCOUNT_USER_ID: "user_alice",
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
  assert.match(await response.text(), /resource=https:\/\/mcp\.yutome\.com\/mcp/);
});

test("hosted revoke endpoint revokes current staged grant and OAuth grant", async () => {
  const grantId = "66666666-7777-4888-9999-aaaaaaaaaaaa";
  const kv = new MemoryKv();
  const env = {
    OAUTH_KV: kv as unknown as KVNamespace,
    YUTOME_PAIRING_CODE: "PAIR-123",
    YUTOME_RELAY_TOKEN: "bridge-token",
    YUTOME_WORKER_MODE: "hosted",
    RELAY: {} as DurableObjectNamespace,
    MCP_OBJECT: {} as DurableObjectNamespace,
  } as Env;
  await kv.put(
    accountGrantKey(grantId),
    JSON.stringify({
      grant_id: grantId,
      user_id: "user_alice",
      workspace_id: "ws_alice",
      client_id: "client-1",
      scopes: ["yutome.search.read"],
      audience: "https://mcp.yutome.com/mcp",
      token_version: "v1",
      status: "active",
      created_at: "2026-05-26T10:00:00.000Z",
      updated_at: "2026-05-26T10:00:00.000Z",
    }),
  );

  const revokedOAuthGrants: Array<{ grantId: string; userId: string }> = [];
  const response = await handleRevokeRequest({
    request: new Request("https://mcp.yutome.com/revoke", {
      method: "POST",
      headers: { authorization: "Bearer current-token" },
    }),
    env,
    oauthHelpers: {
      unwrapToken: async (token: string) => {
        assert.equal(token, "current-token");
        return {
          userId: "user_alice",
          grantId: "oauth-grant-1",
          audience: "https://mcp.yutome.com/mcp",
          grant: {
            props: {
              capsule: "hosted",
              workspace_id: "ws_alice",
              grant_id: grantId,
              user_id: "user_alice",
              client_id: "client-1",
              scopes: ["yutome.search.read"],
              audience: "https://mcp.yutome.com/mcp",
              token_version: "v1",
            },
          },
        };
      },
      revokeGrant: async (oauthGrantId: string, userId: string) => {
        revokedOAuthGrants.push({ grantId: oauthGrantId, userId });
      },
    } as never,
  });

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true, revoked: true, grant_id: grantId });
  assert.deepEqual(revokedOAuthGrants, [{ grantId: "oauth-grant-1", userId: "user_alice" }]);
  const stored = await readHostedAccountGrant(env, grantId);
  assert.equal(stored?.status, "revoked");
  assert.equal(typeof stored?.revoked_at, "string");
});

test("revoke endpoint keeps connector-only behavior explicit", async () => {
  const response = await handleRevokeRequest({
    request: new Request("https://mcp.yutome.com/revoke", {
      method: "POST",
      headers: { authorization: "Bearer current-token" },
    }),
    env: {
      OAUTH_KV: new MemoryKv() as unknown as KVNamespace,
      YUTOME_PAIRING_CODE: "PAIR-123",
      YUTOME_RELAY_TOKEN: "bridge-token",
      YUTOME_WORKER_MODE: "connector_only",
      RELAY: {} as DurableObjectNamespace,
      MCP_OBJECT: {} as DurableObjectNamespace,
    } as Env,
    oauthHelpers: {} as never,
  });

  assert.equal(response.status, 404);
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
