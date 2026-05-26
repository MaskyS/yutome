/**
 * Pure hosted-tenant routing helpers for the Cloudflare MCP edge.
 *
 * This file intentionally has no Worker, Durable Object, or MCP imports. The
 * integration layer can call these helpers before `idFromName(...)`, before
 * relay dispatch, and before returning offline structured content.
 */

export type TenantIdentityField =
  | "workspace_id"
  | "install_id"
  | "connector_grant_id";

export type DurableObjectPurpose = "relay" | "mcp-session";
export type ServedFrom = "bridge" | "replica";
export type OfflineStatus = "offline";
export type TenantArgumentViolationReason = "tenant_key" | "scan_limit";

export interface BridgeInstallIdentity {
  workspace_id: string;
  install_id: string;
}

export interface ConnectorGrantIdentity {
  workspace_id: string;
  connector_grant_id: string;
}

export type TenantRoutingIdentity = BridgeInstallIdentity | ConnectorGrantIdentity;

export interface TenantDurableObjectRoute {
  purpose: DurableObjectPurpose;
  identity: TenantRoutingIdentity;
}

export interface TenantArgumentViolation {
  path: string;
  key: string;
  reason: TenantArgumentViolationReason;
}

export interface TenantArgumentValidationResult {
  ok: boolean;
  violations: TenantArgumentViolation[];
  message?: string;
}

export interface TenantArgumentScanOptions {
  maxDepth?: number;
  maxNodes?: number;
  maxViolations?: number;
  forbiddenKeys?: Iterable<string>;
}

export interface OfflineResponseMetadataOptions {
  attempted_served_from?: ServedFrom;
  last_seen_at?: Date | string | null;
  hosted_replica_available?: boolean;
  durable_object_name?: string;
  reason?: string;
}

export interface TenantIdentityEnv {
  YUTOME_WORKER_MODE?: string;
  YUTOME_WORKSPACE_ID?: string;
  YUTOME_INSTALL_ID?: string;
}

export interface TenantIdentityHeaders {
  get(name: string): string | null;
}

export interface OfflineResponseMetadata {
  ok: false;
  status: OfflineStatus;
  workspace_id: string;
  install_id?: string;
  connector_grant_id?: string;
  durable_object_name: string;
  served_from: null;
  attempted_served_from: ServedFrom;
  desktop_offline: boolean;
  hosted_replica_available: boolean;
  last_seen_at: string | null;
  reason: string;
}

export class TenantRoutingError extends Error {
  readonly code:
    | "tenant_id_missing"
    | "tenant_id_invalid"
    | "tenant_argument_rejected";

  readonly details?: unknown;

  constructor(code: TenantRoutingError["code"], message: string, details?: unknown) {
    super(message);
    this.name = "TenantRoutingError";
    this.code = code;
    this.details = details;
  }
}

export const MAX_TENANT_ID_LENGTH = 128;
export const MAX_DURABLE_OBJECT_NAME_LENGTH = 192;
export const DEFAULT_ARGUMENT_SCAN_MAX_DEPTH = 12;
export const DEFAULT_ARGUMENT_SCAN_MAX_NODES = 500;
export const DEFAULT_ARGUMENT_SCAN_MAX_VIOLATIONS = 20;

const DURABLE_OBJECT_NAME_PREFIX = "yutome:v1";
const ID_SEGMENT_PREVIEW_LENGTH = 36;
const TENANT_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/;

const DEFAULT_FORBIDDEN_TOOL_ARGUMENT_KEYS = new Set([
  "workspace_id",
  "workspaceId",
  "tenant_id",
  "tenantId",
  "install_id",
  "installId",
  "connector_grant_id",
  "connectorGrantId",
  "grant_id",
  "grantId",
  "oauth_grant_id",
  "oauthGrantId",
  "user_id",
  "userId",
  "assistant_client_id",
  "assistantClientId",
  "client_id",
  "clientId",
]);

export function isHostedWorkerMode(mode: unknown): boolean {
  return typeof mode === "string" && mode.trim().toLowerCase() === "hosted";
}

export function resolveMcpBridgeIdentity(
  props: Partial<BridgeInstallIdentity> | null | undefined,
  env: TenantIdentityEnv,
): BridgeInstallIdentity {
  if (isHostedWorkerMode(env.YUTOME_WORKER_MODE)) {
    return {
      workspace_id: normalizeTenantId(props?.workspace_id, "workspace_id"),
      install_id: normalizeTenantId(props?.install_id, "install_id"),
    };
  }

  return resolveLocalCompatibleBridgeIdentity({
    workspace_id: props?.workspace_id,
    install_id: props?.install_id,
    env,
  });
}

export function resolveBridgeRelayIdentityFromHeaders(
  headers: TenantIdentityHeaders,
  env: TenantIdentityEnv,
): BridgeInstallIdentity {
  if (isHostedWorkerMode(env.YUTOME_WORKER_MODE)) {
    return resolveConfiguredBridgeIdentity(env);
  }

  return resolveLocalCompatibleBridgeIdentity({
    workspace_id: headers.get("x-yutome-workspace-id"),
    install_id: headers.get("x-yutome-install-id"),
    env,
  });
}

export function resolveConfiguredBridgeIdentity(env: TenantIdentityEnv): BridgeInstallIdentity {
  if (isHostedWorkerMode(env.YUTOME_WORKER_MODE)) {
    return {
      workspace_id: normalizeTenantId(env.YUTOME_WORKSPACE_ID, "workspace_id"),
      install_id: normalizeTenantId(env.YUTOME_INSTALL_ID, "install_id"),
    };
  }

  return resolveLocalCompatibleBridgeIdentity({ env });
}

export function normalizeTenantId(value: unknown, field: TenantIdentityField): string {
  if (typeof value !== "string") {
    throw new TenantRoutingError(
      "tenant_id_missing",
      `${field} must come from verified auth context and be a string.`,
    );
  }

  const normalized = value.trim();
  if (!normalized) {
    throw new TenantRoutingError(
      "tenant_id_missing",
      `${field} must come from verified auth context and be non-empty.`,
    );
  }
  if (normalized.length > MAX_TENANT_ID_LENGTH || !TENANT_ID_PATTERN.test(normalized)) {
    throw new TenantRoutingError(
      "tenant_id_invalid",
      `${field} is not a valid hosted tenant identifier.`,
      { field, maxLength: MAX_TENANT_ID_LENGTH },
    );
  }
  return normalized;
}

function resolveLocalCompatibleBridgeIdentity(options: {
  workspace_id?: unknown;
  install_id?: unknown;
  env: TenantIdentityEnv;
}): BridgeInstallIdentity {
  return {
    workspace_id: firstTenantString(
      "workspace_id",
      options.workspace_id,
      options.env.YUTOME_WORKSPACE_ID,
      "local",
    ),
    install_id: firstTenantString(
      "install_id",
      options.install_id,
      options.env.YUTOME_INSTALL_ID,
      "desktop",
    ),
  };
}

function firstTenantString(field: TenantIdentityField, ...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) {
      return normalizeTenantId(value, field);
    }
  }
  throw new TenantRoutingError(
    "tenant_id_missing",
    `${field} must come from local configuration or explicit local bridge context.`,
  );
}

export function deriveBridgeRelayObjectName(identity: BridgeInstallIdentity): string {
  return deriveTenantDurableObjectName({
    purpose: "relay",
    identity,
  });
}

export function deriveConnectorMcpObjectName(identity: ConnectorGrantIdentity): string {
  return deriveTenantDurableObjectName({
    purpose: "mcp-session",
    identity,
  });
}

export function deriveTenantDurableObjectName(route: TenantDurableObjectRoute): string {
  const workspaceId = normalizeTenantId(route.identity.workspace_id, "workspace_id");
  const subject =
    "install_id" in route.identity
      ? `install:${tenantSegment("i", normalizeTenantId(route.identity.install_id, "install_id"))}`
      : `grant:${tenantSegment(
          "g",
          normalizeTenantId(route.identity.connector_grant_id, "connector_grant_id"),
        )}`;
  const name = [
    DURABLE_OBJECT_NAME_PREFIX,
    route.purpose,
    `workspace:${tenantSegment("w", workspaceId)}`,
    subject,
  ].join(":");

  if (name.length > MAX_DURABLE_OBJECT_NAME_LENGTH) {
    throw new TenantRoutingError(
      "tenant_id_invalid",
      "Derived Durable Object name exceeded the hosted routing bound.",
      { maxLength: MAX_DURABLE_OBJECT_NAME_LENGTH },
    );
  }
  return name;
}

export function validateTenantIdsNotInToolArguments(
  args: unknown,
  options: TenantArgumentScanOptions = {},
): TenantArgumentValidationResult {
  const violations = findTenantIdToolArguments(args, options);
  if (violations.length === 0) {
    return { ok: true, violations };
  }
  const paths = violations.map((violation) => violation.path).join(", ");
  return {
    ok: false,
    violations,
    message: `Tool arguments must not include hosted tenant identity fields: ${paths}.`,
  };
}

export function assertNoTenantIdsInToolArguments(
  args: unknown,
  options: TenantArgumentScanOptions = {},
): void {
  const result = validateTenantIdsNotInToolArguments(args, options);
  if (!result.ok) {
    throw new TenantRoutingError(
      "tenant_argument_rejected",
      result.message ?? "Tool arguments include hosted tenant identity fields.",
      { violations: result.violations },
    );
  }
}

export function findTenantIdToolArguments(
  args: unknown,
  options: TenantArgumentScanOptions = {},
): TenantArgumentViolation[] {
  const forbiddenKeys = new Set(options.forbiddenKeys ?? DEFAULT_FORBIDDEN_TOOL_ARGUMENT_KEYS);
  const maxDepth = options.maxDepth ?? DEFAULT_ARGUMENT_SCAN_MAX_DEPTH;
  const maxNodes = options.maxNodes ?? DEFAULT_ARGUMENT_SCAN_MAX_NODES;
  const maxViolations = options.maxViolations ?? DEFAULT_ARGUMENT_SCAN_MAX_VIOLATIONS;
  const violations: TenantArgumentViolation[] = [];
  const seen = new WeakSet<object>();
  let visited = 0;

  const visit = (value: unknown, path: string, depth: number): void => {
    if (violations.length >= maxViolations) {
      return;
    }
    if (value === null || typeof value !== "object") {
      return;
    }
    if (seen.has(value)) {
      return;
    }
    seen.add(value);
    visited += 1;

    if (visited > maxNodes || depth > maxDepth) {
      violations.push({ path, key: "<scan_limit>", reason: "scan_limit" });
      return;
    }

    if (Array.isArray(value)) {
      for (let index = 0; index < value.length; index += 1) {
        visit(value[index], `${path}[${index}]`, depth + 1);
      }
      return;
    }

    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      const childPath = appendObjectPath(path, key);
      if (forbiddenKeys.has(key)) {
        violations.push({ path: childPath, key, reason: "tenant_key" });
        if (violations.length >= maxViolations) {
          return;
        }
      }
      visit(child, childPath, depth + 1);
    }
  };

  visit(args, "$", 0);
  return violations;
}

export function buildOfflineResponseMetadata(
  identity: TenantRoutingIdentity,
  options: OfflineResponseMetadataOptions = {},
): OfflineResponseMetadata {
  const attempted = options.attempted_served_from ?? ("install_id" in identity ? "bridge" : "replica");
  const durableObjectName =
    options.durable_object_name ??
    deriveTenantDurableObjectName({
      purpose: attempted === "bridge" ? "relay" : "mcp-session",
      identity,
    });
  const metadata: OfflineResponseMetadata = {
    ok: false,
    status: "offline",
    workspace_id: normalizeTenantId(identity.workspace_id, "workspace_id"),
    durable_object_name: durableObjectName,
    served_from: null,
    attempted_served_from: attempted,
    desktop_offline: attempted === "bridge",
    hosted_replica_available: options.hosted_replica_available ?? false,
    last_seen_at: normalizeOptionalTimestamp(options.last_seen_at),
    reason: options.reason ?? (attempted === "bridge" ? "bridge_offline" : "replica_unavailable"),
  };

  if ("install_id" in identity) {
    metadata.install_id = normalizeTenantId(identity.install_id, "install_id");
  } else {
    metadata.connector_grant_id = normalizeTenantId(
      identity.connector_grant_id,
      "connector_grant_id",
    );
  }

  return metadata;
}

function tenantSegment(prefix: string, id: string): string {
  const readable = id
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, ID_SEGMENT_PREVIEW_LENGTH);
  return `${prefix}_${readable || "id"}_${fnv1a32(id)}`;
}

function fnv1a32(value: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0");
}

function appendObjectPath(path: string, key: string): string {
  if (/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(key)) {
    return `${path}.${key}`;
  }
  return `${path}[${JSON.stringify(key)}]`;
}

function normalizeOptionalTimestamp(value: Date | string | null | undefined): string | null {
  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : value.toISOString();
  }
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed || null;
  }
  return null;
}
