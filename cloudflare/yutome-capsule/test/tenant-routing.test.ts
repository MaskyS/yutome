import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import test from "node:test";
import {
  buildOfflineResponseMetadata,
  deriveBridgeRelayObjectName,
  deriveConnectorMcpObjectName,
  resolveBridgeRelayIdentityFromHeaders,
  resolveMcpBridgeIdentity,
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

test("offline metadata identifies attempted route, workspace, and Durable Object name", () => {
  const metadata = buildOfflineResponseMetadata(
    { workspace_id: "ws_alice", install_id: "inst_mac" },
    { attempted_served_from: "bridge", reason: "bridge_offline" },
  );

  assert.equal(metadata.ok, false);
  assert.equal(metadata.status, "offline");
  assert.equal(metadata.served_from, null);
  assert.equal(metadata.attempted_served_from, "bridge");
  assert.equal(metadata.workspace_id, "ws_alice");
  assert.equal(metadata.install_id, "inst_mac");
  assert.match(metadata.durable_object_name, /^yutome:v1:relay:workspace:/);
  assert.notEqual(metadata.durable_object_name, "default");
});
