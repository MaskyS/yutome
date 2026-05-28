import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import test from "node:test";
import {
  buildOfflineResponseMetadata,
  HostedMcpApiClient,
  HostedMcpApiError,
  HostedMcpAuthError,
  MAX_DURABLE_OBJECT_NAME_LENGTH,
  MAX_TENANT_ID_LENGTH,
  deriveBridgeRelayObjectName,
  deriveConnectorMcpObjectName,
  normalizeTenantId,
  resolveBridgeRelayIdentityFromHeaders,
  resolveHostedMcpAuthContext,
  resolveMcpBridgeIdentity,
  shouldServeConnectorRelayRoutes,
  TenantRoutingError,
  validateTenantIdsNotInToolArguments,
} from "../src/tenant-routing.ts";

test("hosted Durable Object routing never derives the default object name", () => {
  const relayName = deriveBridgeRelayObjectName({
    workspace_id: "ws_alice",
    install_id: "inst_mac",
  });
  const mcpName = deriveConnectorMcpObjectName({
    workspace_id: "ws_alice",
    connector_grant_id: "grant_claude",
  });

  assert.match(relayName, /^yutome:v1:relay:workspace:/);
  assert.match(relayName, /:install:/);
  assert.notEqual(relayName, "default");

  assert.match(mcpName, /^yutome:v1:mcp-session:workspace:/);
  assert.match(mcpName, /:grant:/);
  assert.notEqual(mcpName, "default");
});

test("hosted Durable Object names stay bounded for maximum tenant ids", () => {
  const maxWorkspaceId = `w${"a".repeat(MAX_TENANT_ID_LENGTH - 1)}`;
  const maxInstallId = `i${"b".repeat(MAX_TENANT_ID_LENGTH - 1)}`;
  const maxGrantId = `g${"c".repeat(MAX_TENANT_ID_LENGTH - 1)}`;

  const relayName = deriveBridgeRelayObjectName({
    workspace_id: maxWorkspaceId,
    install_id: maxInstallId,
  });
  const mcpName = deriveConnectorMcpObjectName({
    workspace_id: maxWorkspaceId,
    connector_grant_id: maxGrantId,
  });

  assert.equal(relayName.length <= MAX_DURABLE_OBJECT_NAME_LENGTH, true);
  assert.equal(mcpName.length <= MAX_DURABLE_OBJECT_NAME_LENGTH, true);
  assert.notEqual(relayName, mcpName);
});

test("hosted tenant ids reject empty unsafe and overlong values", () => {
  const overlong = `w${"a".repeat(MAX_TENANT_ID_LENGTH)}`;
  for (const field of ["workspace_id", "install_id", "connector_grant_id"] as const) {
    for (const value of ["", "   ", "has/slash", overlong]) {
      assert.throws(
        () => normalizeTenantId(value, field),
        (err) =>
          err instanceof TenantRoutingError &&
          (err.code === "tenant_id_missing" || err.code === "tenant_id_invalid") &&
          err.message.includes(field),
      );
    }
  }
});

test("hosted source paths do not call idFromName with the default tenant", async () => {
  const sourceFiles = (await readdir("src"))
    .filter((file) => file.endsWith(".ts"))
    .map((file) => `src/${file}`);

  for (const file of sourceFiles) {
    const source = await readFile(file, "utf8");
    assert.doesNotMatch(source, /idFromName\(\s*["']default["']\s*\)/, file);
  }
});

test("hosted relay routing ignores raw tenant headers and uses verified bridge config", () => {
  const identity = resolveBridgeRelayIdentityFromHeaders(
    new Headers({
      "x-yutome-workspace-id": "ws_attacker",
      "x-yutome-install-id": "inst_attacker",
    }),
    {
      YUTOME_WORKER_MODE: "hosted",
      YUTOME_WORKSPACE_ID: "ws_alice",
      YUTOME_INSTALL_ID: "inst_mac",
    },
  );

  assert.deepEqual(identity, {
    workspace_id: "ws_alice",
    install_id: "inst_mac",
  });
});

test("hosted mode rejects missing verified tenant identity before routing", () => {
  assert.throws(
    () =>
      resolveBridgeRelayIdentityFromHeaders(new Headers(), {
        YUTOME_WORKER_MODE: "hosted",
      }),
    (err) =>
      err instanceof TenantRoutingError &&
      err.code === "tenant_id_missing" &&
      /workspace_id/.test(err.message),
  );

  assert.throws(
    () =>
      resolveMcpBridgeIdentity(null, {
        YUTOME_WORKER_MODE: "hosted",
        YUTOME_WORKSPACE_ID: "ws_env_must_not_be_used_for_mcp",
        YUTOME_INSTALL_ID: "inst_env_must_not_be_used_for_mcp",
      }),
    (err) =>
      err instanceof TenantRoutingError &&
      err.code === "tenant_id_missing" &&
      /workspace_id/.test(err.message),
  );
});

test("connector-only mode keeps explicit local bridge compatibility", () => {
  assert.equal(shouldServeConnectorRelayRoutes("hosted"), false);
  assert.equal(shouldServeConnectorRelayRoutes("connector_only"), true);

  assert.deepEqual(
    resolveBridgeRelayIdentityFromHeaders(new Headers(), {
      YUTOME_WORKER_MODE: "connector_only",
    }),
    {
      workspace_id: "local",
      install_id: "desktop",
    },
  );

  assert.deepEqual(
    resolveMcpBridgeIdentity(null, {
      YUTOME_WORKER_MODE: "connector_only",
    }),
    {
      workspace_id: "local",
      install_id: "desktop",
    },
  );
});

test("tenant identity fields in tool arguments are rejected anywhere in the payload", () => {
  const result = validateTenantIdsNotInToolArguments({
    query: "vector search",
    filters: [
      { channel: "UC123" },
      { nested: { connectorGrantId: "grant_other" } },
    ],
    workspace_id: "ws_other",
  });

  assert.equal(result.ok, false);
  assert.deepEqual(
    result.violations.map((violation) => violation.path),
    ["$.filters[1].nested.connectorGrantId", "$.workspace_id"],
  );
  assert.match(result.message ?? "", /hosted tenant identity fields/);
});

test("ordinary hosted query fields pass tenant argument validation", () => {
  const result = validateTenantIdsNotInToolArguments({
    video_id: "OEDoJyhQhXs",
    channel: "leoandlongevity",
    source: "youtube",
    language: "en",
    limit: 10,
    offset: 0,
    request: {
      query: "longevity",
      filters: { tags: ["nutrition"] },
    },
  });

  assert.equal(result.ok, true);
  assert.deepEqual(result.violations, []);
});

test("tenant argument scanner fails closed when traversal limits are exceeded", () => {
  const byDepth = validateTenantIdsNotInToolArguments({ a: { b: { c: true } } }, { maxDepth: 1 });
  assert.equal(byDepth.ok, false);
  assert.deepEqual(byDepth.violations, [{ path: "$.a.b", key: "<scan_limit>", reason: "scan_limit" }]);

  const byNodes = validateTenantIdsNotInToolArguments([{ ok: true }, { ok: true }], { maxNodes: 1 });
  assert.equal(byNodes.ok, false);
  assert.equal(byNodes.violations[0].reason, "scan_limit");
});

test("offline metadata identifies attempted route, workspace, and Durable Object name", () => {
  const metadata = buildOfflineResponseMetadata(
    { workspace_id: "ws_alice", install_id: "inst_mac" },
    { attempted_served_from: "bridge", reason: "bridge_offline", last_seen_at: "2026-05-26T12:00:00.000Z" },
  );

  assert.equal(metadata.ok, false);
  assert.equal(metadata.status, "offline");
  assert.equal(metadata.served_from, null);
  assert.equal(metadata.attempted_served_from, "bridge");
  assert.equal(metadata.workspace_id, "ws_alice");
  assert.equal(metadata.install_id, "inst_mac");
  assert.equal(metadata.desktop_offline, true);
  assert.equal(metadata.hosted_replica_available, false);
  assert.equal(metadata.last_seen_at, "2026-05-26T12:00:00.000Z");
  assert.match(metadata.durable_object_name, /^yutome:v1:relay:workspace:/);
  assert.notEqual(metadata.durable_object_name, "default");
});

test("offline metadata can describe an unavailable search replica", () => {
  const metadata = buildOfflineResponseMetadata(
    { workspace_id: "ws_alice", connector_grant_id: "grant_claude" },
    { attempted_served_from: "replica", hosted_replica_available: false },
  );

  assert.equal(metadata.ok, false);
  assert.equal(metadata.status, "offline");
  assert.equal(metadata.served_from, null);
  assert.equal(metadata.attempted_served_from, "replica");
  assert.equal(metadata.desktop_offline, false);
  assert.equal(metadata.hosted_replica_available, false);
  assert.equal(metadata.workspace_id, "ws_alice");
  assert.equal(metadata.connector_grant_id, "grant_claude");
  assert.match(metadata.durable_object_name, /^yutome:v1:mcp-session:workspace:/);
  assert.notEqual(metadata.durable_object_name, "default");
});

test("hosted auth context comes from OAuth props and requires search scope", () => {
  const auth = resolveHostedMcpAuthContext(
    {
      workspace_id: "ws_alice",
      connector_grant_id: "grant_claude",
      client_id: "client_chatgpt",
      user_id: "user_alice",
      scopes: ["profile", "yutome.search.read"],
      audience: "https://mcp.getyutome.com/mcp",
      expires_at: "2026-05-26T12:00:00.000Z",
      token_version: "v1",
    },
    { requiredScope: "yutome.search.read", sessionId: "session_123" },
  );

  assert.deepEqual(auth, {
    workspace_id: "ws_alice",
    scopes: ["profile", "yutome.search.read"],
    user_id: "user_alice",
    grant_id: "grant_claude",
    client_id: "client_chatgpt",
    session_id: "session_123",
    audience: "https://mcp.getyutome.com/mcp",
    expires_at: "2026-05-26T12:00:00.000Z",
    token_version: "v1",
  });

  assert.throws(
    () =>
      resolveHostedMcpAuthContext(
        { workspace_id: "ws_alice", scopes: ["profile"] },
        { requiredScope: "yutome.search.read" },
      ),
    (err) =>
      err instanceof HostedMcpAuthError &&
      err.code === "insufficient_scope" &&
      /yutome\.search\.read/.test(err.message),
  );
});

test("hosted API client dispatches tool calls with Yutome auth headers", async () => {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  let fetchThis: unknown;
  const fetcher = (async function (this: unknown, input: RequestInfo | URL, init?: RequestInit) {
    fetchThis = this;
    calls.push({ url: String(input), init: init ?? {} });
    return Response.json({
      ok: true,
      result: { rows: [{ chunk_id: "chunk_1", snippet: "hello" }], notes: [], total: null },
    });
  }) as typeof fetch;
  const client = new HostedMcpApiClient(
    {
      YUTOME_HOSTED_API_URL: "https://hosted.example/mcp/",
      YUTOME_HOSTED_API_TOKEN: "edge-secret",
    },
    fetcher,
  );

  const result = await client.callTool(
    {
      workspace_id: "ws_alice",
      scopes: ["yutome.search.read"],
      user_id: "user_alice",
      grant_id: "grant_1",
      client_id: "client_1",
      session_id: "session_1",
    },
    "find",
    { text: "vector databases" },
  );

  assert.deepEqual(result, {
    rows: [{ chunk_id: "chunk_1", snippet: "hello" }],
    notes: [],
    total: null,
  });
  assert.equal(fetchThis, globalThis);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://hosted.example/mcp/tools/call");
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(JSON.parse(String(calls[0].init.body)), {
    name: "find",
    arguments: { text: "vector databases" },
  });
  const headers = new Headers(calls[0].init.headers);
  assert.equal(headers.get("authorization"), "Bearer edge-secret");
  assert.equal(headers.get("x-yutome-workspace-id"), "ws_alice");
  assert.equal(headers.get("x-yutome-scopes"), "yutome.search.read");
  assert.equal(headers.get("x-yutome-user-id"), "user_alice");
  assert.equal(headers.get("x-yutome-grant-id"), "grant_1");
  assert.equal(headers.get("x-yutome-client-id"), "client_1");
  assert.equal(headers.get("x-yutome-session-id"), "session_1");
});

test("hosted API client dispatches resource reads and surfaces API failures", async () => {
  const resourceCalls: Array<{ url: string; init: RequestInit }> = [];
  const resourceClient = new HostedMcpApiClient(
    {
      YUTOME_HOSTED_API_URL: "https://hosted.example",
      YUTOME_HOSTED_API_TOKEN: "edge-secret",
    },
    (async (input: RequestInfo | URL, init?: RequestInit) => {
      resourceCalls.push({ url: String(input), init: init ?? {} });
      return Response.json({ ok: true, result: { contents: [{ uri: "yutome://chunk/ch_1", text: "{}" }] } });
    }) as typeof fetch,
  );

  await resourceClient.readResource(
    { workspace_id: "ws_alice", scopes: ["yutome.search.read"] },
    "yutome://chunk/ch_1",
  );

  assert.equal(resourceCalls[0].url, "https://hosted.example/resources/read");
  assert.deepEqual(JSON.parse(String(resourceCalls[0].init.body)), { uri: "yutome://chunk/ch_1" });

  const failingClient = new HostedMcpApiClient(
    {
      YUTOME_HOSTED_API_URL: "https://hosted.example",
      YUTOME_HOSTED_API_TOKEN: "edge-secret",
    },
    (async () =>
      Response.json(
        {
          detail: {
            code: "insufficient_scope",
            message: "Hosted MCP requests require the yutome.search.read scope.",
          },
        },
        { status: 403 },
      )) as typeof fetch,
  );

  let caught: unknown;
  try {
    await failingClient.callTool(
      { workspace_id: "ws_alice", scopes: ["profile"] },
      "find",
      { text: "anything" },
    );
  } catch (err) {
    caught = err;
  }
  assert.equal(caught instanceof HostedMcpApiError, true);
  const apiError = caught as HostedMcpApiError;
  assert.equal(apiError.status, 403);
  assert.equal(apiError.code, "insufficient_scope");
  assert.match(apiError.message, /yutome\.search\.read/);
});
