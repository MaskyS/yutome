import type { AuthRequest } from "@cloudflare/workers-oauth-provider";
import type { OAuthHelpers } from "@cloudflare/workers-oauth-provider";
import type { Env, YutomeAuthProps } from "./env";
import {
  HostedMcpAuthError,
  isHostedWorkerMode,
  normalizeTenantId,
  type HostedMcpAuthContext,
  type ResolveHostedMcpAuthOptions,
} from "./tenant-routing.ts";

export type HostedAccountGrantStatus = "active" | "revoked";

export interface HostedAccountGrant {
  grant_id: string;
  user_id: string;
  workspace_id: string;
  client_id: string;
  scopes: string[];
  audience: string;
  token_version: string;
  status: HostedAccountGrantStatus;
  created_at: string;
  updated_at: string;
  expires_at?: string;
  revoked_at?: string;
}

export interface IssueHostedAccountGrantOptions {
  grantId: string;
  authRequest: AuthRequest;
  workspaceId: string;
  audience: string;
  tokenVersion: string;
  expiresAt: string;
  now?: Date;
}

export class HostedAccountGrantError extends Error {
  readonly code:
    | "hosted_account_user_missing"
    | "hosted_grant_missing"
    | "hosted_grant_revoked"
    | "hosted_grant_expired"
    | "hosted_grant_invalid"
    | "hosted_grant_mismatch";

  readonly status: number;

  constructor(code: HostedAccountGrantError["code"], message: string, status: number) {
    super(message);
    this.name = "HostedAccountGrantError";
    this.code = code;
    this.status = status;
  }
}

const ACCOUNT_GRANT_PREFIX = "yutome:account-grant:";
export const YUTOME_MCP_SCOPE = "yutome.search.read";
export const DEFAULT_MCP_AUDIENCE = "https://mcp.yutome.com/mcp";

export function accountGrantKey(grantId: string): string {
  return `${ACCOUNT_GRANT_PREFIX}${normalizeGrantId(grantId)}`;
}

export async function issueHostedAccountGrant(
  env: Env,
  options: IssueHostedAccountGrantOptions,
): Promise<HostedAccountGrant> {
  const existing = await readHostedAccountGrant(env, options.grantId);
  if (existing) {
    assertGrantActive(existing, options.now);
    return existing;
  }

  const now = options.now ?? new Date();
  const grant: HostedAccountGrant = {
    grant_id: normalizeGrantId(options.grantId),
    user_id: configuredAccountUserId(env),
    workspace_id: normalizeTenantId(options.workspaceId, "workspace_id"),
    client_id: normalizeGrantId(options.authRequest.clientId),
    scopes: normalizeScopes(options.authRequest.scope),
    audience: nonEmptyString(options.audience, "audience"),
    token_version: nonEmptyString(options.tokenVersion, "token_version"),
    status: "active",
    created_at: now.toISOString(),
    updated_at: now.toISOString(),
    expires_at: nonEmptyOptionalString(options.expiresAt),
  };

  await writeHostedAccountGrant(env, grant);
  return grant;
}

export async function resolveActiveHostedAccountGrantFromProps(
  env: Env,
  props: Partial<YutomeAuthProps> | null | undefined,
): Promise<HostedAccountGrant> {
  const grantId = typeof props?.grant_id === "string" ? props.grant_id : props?.connector_grant_id;
  if (typeof grantId !== "string" || !grantId.trim()) {
    throw new HostedAccountGrantError(
      "hosted_grant_missing",
      "Hosted MCP token did not include a Yutome account grant id.",
      401,
    );
  }

  const grant = await readHostedAccountGrant(env, grantId);
  if (!grant) {
    throw new HostedAccountGrantError(
      "hosted_grant_missing",
      "Hosted MCP account grant was not found or has expired.",
      401,
    );
  }
  assertGrantActive(grant);
  return grant;
}

export async function resolveHostedMcpAuthContextFromStoredGrant(
  env: Env,
  props: Partial<YutomeAuthProps> | null | undefined,
  options: ResolveHostedMcpAuthOptions,
): Promise<HostedMcpAuthContext> {
  const grant = await resolveActiveHostedAccountGrantFromProps(env, props);
  assertTokenPropsMatchStoredGrant(props, grant);
  if (!grant.scopes.includes(options.requiredScope)) {
    throw new HostedMcpAuthError(
      "insufficient_scope",
      `Hosted MCP requests require the ${options.requiredScope} scope.`,
      403,
    );
  }
  return withoutUndefined({
    workspace_id: grant.workspace_id,
    scopes: grant.scopes,
    user_id: grant.user_id,
    grant_id: grant.grant_id,
    client_id: grant.client_id,
    session_id: nonEmptyOptionalString(options.sessionId),
    audience: grant.audience,
    expires_at: grant.expires_at,
    token_version: grant.token_version,
  });
}

export function configuredMcpAudience(env: Pick<Env, "YUTOME_MCP_AUDIENCE">): string {
  const configured = env.YUTOME_MCP_AUDIENCE?.trim();
  return configured || DEFAULT_MCP_AUDIENCE;
}

export function assertAuthRequestTargetsMcpAudience(
  authRequest: AuthRequest,
  audience: string,
): void {
  const requested = Array.isArray(authRequest.resource)
    ? authRequest.resource
    : typeof authRequest.resource === "string"
      ? [authRequest.resource]
      : [];
  if (!requested.some((resource) => resource === audience)) {
    throw new HostedAccountGrantError(
      "hosted_grant_invalid",
      `Hosted MCP authorization requests must include resource=${audience}.`,
      400,
    );
  }
}

export function assertTokenTargetsMcpAudience(
  audienceValue: string | string[] | undefined,
  audience: string,
): void {
  const audiences = Array.isArray(audienceValue)
    ? audienceValue
    : typeof audienceValue === "string"
      ? [audienceValue]
      : [];
  if (!audiences.some((candidate) => candidate === audience)) {
    throw new HostedAccountGrantError(
      "hosted_grant_invalid",
      `Hosted MCP token audience must be ${audience}.`,
      401,
    );
  }
}

export async function revokeHostedAccountGrant(
  env: Env,
  grantId: string,
  now: Date = new Date(),
): Promise<HostedAccountGrant> {
  const grant = await readHostedAccountGrant(env, grantId);
  if (!grant) {
    throw new HostedAccountGrantError(
      "hosted_grant_missing",
      "Hosted MCP account grant was not found or has expired.",
      404,
    );
  }
  const revoked: HostedAccountGrant = {
    ...grant,
    status: "revoked",
    revoked_at: now.toISOString(),
    updated_at: now.toISOString(),
  };
  await writeHostedAccountGrant(env, revoked);
  return revoked;
}

interface TokenSummaryLike {
  userId: string;
  grantId: string;
  audience?: string | string[];
  grant: {
    props: YutomeAuthProps;
  };
}

export async function handleRevokeRequest(ctx: {
  request: Request;
  env: Env;
  oauthHelpers: OAuthHelpers;
}): Promise<Response> {
  const { request, env, oauthHelpers } = ctx;
  if (!isHostedWorkerMode(env.YUTOME_WORKER_MODE)) {
    return new Response("Not Found", {
      status: 404,
      headers: { "cache-control": "no-store" },
    });
  }
  if (request.method !== "POST") {
    return new Response("Method Not Allowed", {
      status: 405,
      headers: {
        allow: "POST",
        "cache-control": "no-store",
      },
    });
  }

  const audience = configuredMcpAudience(env);
  const token = bearerToken(request.headers);
  if (!token) {
    return revokeAuthError("invalid_token", "Missing bearer token", audience);
  }

  const tokenSummary = await oauthHelpers.unwrapToken<YutomeAuthProps>(token) as TokenSummaryLike | null;
  if (!tokenSummary) {
    return revokeAuthError("invalid_token", "Invalid bearer token", audience);
  }

  try {
    assertTokenTargetsMcpAudience(tokenSummary.audience, audience);
    const auth = await resolveHostedMcpAuthContextFromStoredGrant(env, tokenSummary.grant.props, {
      requiredScope: YUTOME_MCP_SCOPE,
    });
    const grantId = auth.grant_id;
    if (!grantId) {
      throw new HostedAccountGrantError(
        "hosted_grant_missing",
        "Hosted MCP token did not include a Yutome account grant id.",
        401,
      );
    }
    await revokeHostedAccountGrant(env, grantId);
    await oauthHelpers.revokeGrant(tokenSummary.grantId, auth.user_id ?? tokenSummary.userId);
    return Response.json(
      { ok: true, revoked: true, grant_id: grantId },
      { headers: { "cache-control": "no-store" } },
    );
  } catch (err) {
    if (err instanceof HostedMcpAuthError) {
      return Response.json(
        { error: err.code, message: err.message },
        {
          status: err.status,
          headers: revokeErrorHeaders(err.status, audience, err.code),
        },
      );
    }
    if (err instanceof HostedAccountGrantError) {
      return Response.json(
        { error: err.code, message: err.message },
        {
          status: err.status,
          headers: revokeErrorHeaders(err.status, audience, err.code),
        },
      );
    }
    throw err;
  }
}

export function hostedGrantProps(grant: HostedAccountGrant, installId?: string): YutomeAuthProps {
  return withoutUndefined({
    capsule: "hosted",
    workspace_id: grant.workspace_id,
    install_id: installId,
    connector_grant_id: grant.grant_id,
    grant_id: grant.grant_id,
    user_id: grant.user_id,
    client_id: grant.client_id,
    scopes: grant.scopes,
    audience: grant.audience,
    token_version: grant.token_version,
    paired_at: grant.created_at,
    expires_at: grant.expires_at,
  });
}

export async function readHostedAccountGrant(env: Env, grantId: string): Promise<HostedAccountGrant | null> {
  const raw = await env.OAUTH_KV.get(accountGrantKey(grantId));
  if (!raw) {
    return null;
  }
  const parsed = parseJsonObject(raw);
  return normalizeStoredGrant(parsed);
}

async function writeHostedAccountGrant(env: Env, grant: HostedAccountGrant): Promise<void> {
  await env.OAUTH_KV.put(accountGrantKey(grant.grant_id), JSON.stringify(grant));
}

function assertGrantActive(grant: HostedAccountGrant, now: Date = new Date()): void {
  if (grant.status === "revoked") {
    throw new HostedAccountGrantError(
      "hosted_grant_revoked",
      "Hosted MCP account grant has been revoked.",
      403,
    );
  }
  if (grant.expires_at) {
    const expiresAtMs = Date.parse(grant.expires_at);
    if (!Number.isFinite(expiresAtMs)) {
      throw new HostedAccountGrantError(
        "hosted_grant_invalid",
        "Hosted MCP account grant expiry is invalid.",
        403,
      );
    }
    if (expiresAtMs <= now.getTime()) {
      throw new HostedAccountGrantError(
        "hosted_grant_expired",
        "Hosted MCP account grant has expired.",
        401,
      );
    }
  }
  if (grant.status !== "active") {
    throw new HostedAccountGrantError(
      "hosted_grant_invalid",
      "Hosted MCP account grant is not active.",
      403,
    );
  }
}

function normalizeStoredGrant(value: Record<string, unknown>): HostedAccountGrant {
  const status: HostedAccountGrantStatus | null =
    value.status === "revoked" ? "revoked" : value.status === "active" ? "active" : null;
  if (!status) {
    throw new HostedAccountGrantError(
      "hosted_grant_invalid",
      "Hosted MCP account grant record has an invalid status.",
      403,
    );
  }
  return withoutUndefined({
    grant_id: normalizeGrantId(value.grant_id),
    user_id: normalizeGrantId(value.user_id),
    workspace_id: normalizeTenantId(value.workspace_id, "workspace_id"),
    client_id: normalizeGrantId(value.client_id),
    scopes: normalizeScopes(value.scopes),
    audience: nonEmptyString(value.audience, "audience"),
    token_version: nonEmptyString(value.token_version, "token_version"),
    status,
    created_at: nonEmptyString(value.created_at, "created_at"),
    updated_at: nonEmptyString(value.updated_at, "updated_at"),
    expires_at: nonEmptyOptionalString(value.expires_at),
    revoked_at: nonEmptyOptionalString(value.revoked_at),
  });
}

function assertTokenPropsMatchStoredGrant(
  props: Partial<YutomeAuthProps> | null | undefined,
  grant: HostedAccountGrant,
): void {
  const rawProps = recordFromUnknown(props);
  const mismatches: string[] = [];

  compareOptionalString(rawProps.workspace_id, grant.workspace_id, "workspace_id", mismatches);
  compareOptionalString(rawProps.user_id, grant.user_id, "user_id", mismatches);
  compareOptionalString(rawProps.client_id, grant.client_id, "client_id", mismatches);
  compareOptionalString(rawProps.grant_id, grant.grant_id, "grant_id", mismatches);
  compareOptionalString(rawProps.connector_grant_id, grant.grant_id, "connector_grant_id", mismatches);
  compareOptionalString(rawProps.audience, grant.audience, "audience", mismatches);
  compareOptionalString(rawProps.token_version, grant.token_version, "token_version", mismatches);

  const propsScopes = rawProps.scopes ?? rawProps.scope;
  if (propsScopes !== undefined && !sameStringSet(normalizeScopes(propsScopes), grant.scopes)) {
    mismatches.push("scopes");
  }

  if (mismatches.length > 0) {
    throw new HostedAccountGrantError(
      "hosted_grant_mismatch",
      `Hosted MCP token props do not match stored account grant: ${mismatches.join(", ")}.`,
      401,
    );
  }
}

function compareOptionalString(
  value: unknown,
  expected: string,
  field: string,
  mismatches: string[],
): void {
  const actual = nonEmptyOptionalString(value);
  if (actual !== undefined && actual !== expected) {
    mismatches.push(field);
  }
}

function sameStringSet(left: string[], right: string[]): boolean {
  if (left.length !== right.length) {
    return false;
  }
  const rightSet = new Set(right);
  return left.every((value) => rightSet.has(value));
}

function configuredAccountUserId(env: Env): string {
  const userId = env.YUTOME_ACCOUNT_USER_ID;
  if (typeof userId !== "string" || !userId.trim()) {
    throw new HostedAccountGrantError(
      "hosted_account_user_missing",
      "YUTOME_ACCOUNT_USER_ID is required for hosted OAuth grants.",
      500,
    );
  }
  return normalizeGrantId(userId);
}

function normalizeGrantId(value: unknown): string {
  return nonEmptyString(value, "grant_id");
}

function normalizeScopes(value: unknown): string[] {
  const rawScopes = Array.isArray(value)
    ? value
    : typeof value === "string"
      ? value.replace(",", " ").split(/\s+/)
      : [];
  return [...new Set(rawScopes.map((scope) => nonEmptyOptionalString(scope)).filter(isString))];
}

function nonEmptyString(value: unknown, field: string): string {
  const normalized = nonEmptyOptionalString(value);
  if (!normalized) {
    throw new HostedAccountGrantError(
      "hosted_grant_invalid",
      `Hosted account grant ${field} must be a non-empty string.`,
      403,
    );
  }
  return normalized;
}

function nonEmptyOptionalString(value: unknown): string | undefined {
  if (value === undefined || value === null) {
    return undefined;
  }
  const normalized = String(value).trim();
  if (!normalized) {
    return undefined;
  }
  if (/[\r\n]/.test(normalized) || normalized.length > 512) {
    throw new HostedAccountGrantError(
      "hosted_grant_invalid",
      "Hosted account grant field contained an invalid header value.",
      403,
    );
  }
  return normalized;
}

function parseJsonObject(raw: string): Record<string, unknown> {
  try {
    const parsed = JSON.parse(raw);
    if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    // Fall through to a normalized grant error below.
  }
  throw new HostedAccountGrantError(
    "hosted_grant_invalid",
    "Hosted MCP account grant record was not valid JSON.",
    403,
  );
}

function recordFromUnknown(value: unknown): Record<string, unknown> {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
}

function bearerToken(headers: Headers): string | null {
  const header = headers.get("authorization");
  if (!header?.startsWith("Bearer ")) {
    return null;
  }
  const token = header.slice("Bearer ".length).trim();
  return token || null;
}

function revokeAuthError(error: string, description: string, audience: string): Response {
  return Response.json(
    { error, message: description },
    {
      status: 401,
      headers: revokeErrorHeaders(401, audience, error, description),
    },
  );
}

function revokeErrorHeaders(status: number, audience: string, error: string, description?: string): Headers {
  const headers = new Headers({ "cache-control": "no-store" });
  if (status === 401 || status === 403) {
    const parts = [
      `Bearer realm="OAuth"`,
      `resource_metadata="${resourceMetadataUrl(audience)}"`,
      `error="${headerQuoted(error)}"`,
      `scope="${YUTOME_MCP_SCOPE}"`,
    ];
    if (description) {
      parts.push(`error_description="${headerQuoted(description)}"`);
    }
    headers.set("www-authenticate", parts.join(", "));
  }
  return headers;
}

function resourceMetadataUrl(audience: string): string {
  const url = new URL(audience);
  return `${url.origin}/.well-known/oauth-protected-resource${url.pathname}`;
}

function headerQuoted(value: string): string {
  return value.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function withoutUndefined<T extends Record<string, unknown>>(value: T): T {
  return Object.fromEntries(
    Object.entries(value).filter(([, entryValue]) => entryValue !== undefined),
  ) as T;
}
