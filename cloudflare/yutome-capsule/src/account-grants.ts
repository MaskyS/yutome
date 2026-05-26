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

/**
 * The Cloudflare edge's KV cache of a connector grant — the OAuth authorization binding one
 * assistant client to one workspace. See docs/hosted-glossary.md ("connector grant").
 *
 * This is one of two records for the same concept across the language boundary. The Python
 * control-plane record `AccountGrant` (src/yutome/hosted/control_plane.py) is the broader
 * source of truth — it also covers CLI and account-session grant kinds. They are
 * intentionally not identical:
 *  - `grant_id` here is the storage-key form; `connector_grant_id` is the domain field used
 *    in token props (props may carry either name — both resolve to this `grant_id`).
 *  - `token_version` is a string on the edge and an int in the Python record. They never
 *    cross: the edge issues and validates its own OAuth tokens, and the Python API trusts
 *    the edge's signed headers rather than re-validating the token, so no single value is
 *    written by one side and compared by the other.
 */
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
  session_id?: string;
  expires_at?: string;
  revoked_at?: string;
}

export interface HostedAccountSession {
  user_id: string;
  workspace_id: string;
  workspace_ids: string[];
  session_id?: string;
}

export interface IssueHostedAccountGrantOptions {
  grantId: string;
  authRequest: AuthRequest;
  accountSession: HostedAccountSession;
  audience: string;
  tokenVersion: string;
  expiresAt: string;
  now?: Date;
}

export class HostedAccountGrantError extends Error {
  readonly code:
    | "hosted_account_session_missing"
    | "hosted_account_session_invalid"
    | "hosted_account_workspace_mismatch"
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
export const DEFAULT_ACCOUNT_SESSION_AUDIENCE = "yutome:hosted-oauth";
export const DEFAULT_ACCOUNT_SESSION_MAX_AGE_SECONDS = 60 * 60;
export const DEFAULT_ACCOUNT_SESSION_CLOCK_SKEW_SECONDS = 60;
export const ACCOUNT_SESSION_COOKIE_NAME = "yutome_account_session";
export const ACCOUNT_SESSION_HEADER = "x-yutome-account-session";
const ACCOUNT_SESSION_REPLAY_PREFIX = "yutome:account-session-replay:";

type AccountSessionEnv = Pick<Env, "YUTOME_ACCOUNT_SESSION_HMAC_SECRET" | "YUTOME_ACCOUNT_SESSION_AUDIENCE"> &
  Partial<Pick<Env, "OAUTH_KV" | "YUTOME_ACCOUNT_SESSION_MAX_AGE_SECONDS" | "YUTOME_ACCOUNT_SESSION_CLOCK_SKEW_SECONDS">>;

export function accountGrantKey(grantId: string): string {
  return `${ACCOUNT_GRANT_PREFIX}${normalizeGrantId(grantId)}`;
}

export async function issueHostedAccountGrant(
  env: Env,
  options: IssueHostedAccountGrantOptions,
): Promise<HostedAccountGrant> {
  const requestedGrant = {
    user_id: normalizeAccountId(options.accountSession.user_id, "user_id"),
    workspace_id: normalizeTenantId(options.accountSession.workspace_id, "workspace_id"),
    client_id: normalizeGrantId(options.authRequest.clientId),
    scopes: normalizeRequiredScopes(options.authRequest.scope),
    audience: nonEmptyString(options.audience, "audience"),
    token_version: nonEmptyString(options.tokenVersion, "token_version"),
    session_id: nonEmptyOptionalString(options.accountSession.session_id),
  };
  const existing = await readHostedAccountGrant(env, options.grantId);
  if (existing) {
    assertGrantActive(existing, options.now);
    assertGrantMatchesAuthorization(existing, requestedGrant);
    return existing;
  }

  const now = options.now ?? new Date();
  const grant: HostedAccountGrant = {
    grant_id: normalizeGrantId(options.grantId),
    user_id: requestedGrant.user_id,
    workspace_id: requestedGrant.workspace_id,
    client_id: requestedGrant.client_id,
    scopes: requestedGrant.scopes,
    audience: requestedGrant.audience,
    token_version: requestedGrant.token_version,
    status: "active",
    created_at: now.toISOString(),
    updated_at: now.toISOString(),
    session_id: requestedGrant.session_id,
    expires_at: nonEmptyOptionalString(options.expiresAt),
  };

  await writeHostedAccountGrant(env, grant);
  return grant;
}

export async function resolveHostedAccountSessionFromRequest(
  request: Request,
  env: AccountSessionEnv,
  options: {
    selectedWorkspaceId?: string;
    allowWorkspaceSelection?: boolean;
    allowHeaderFallback?: boolean;
    consumeReplay?: boolean;
    now?: Date;
  } = {},
): Promise<HostedAccountSession> {
  const secret = env.YUTOME_ACCOUNT_SESSION_HMAC_SECRET?.trim();
  if (!secret) {
    throw new HostedAccountGrantError(
      "hosted_account_session_missing",
      "Hosted OAuth requires a configured account session verifier.",
      500,
    );
  }

  const token = accountSessionTokenFromRequest(request, { allowHeaderFallback: options.allowHeaderFallback === true });
  if (!token) {
    throw new HostedAccountGrantError(
      "hosted_account_session_missing",
      "Sign in to Yutome before approving hosted MCP access.",
      401,
    );
  }

  const payload = await verifyAccountSessionToken(token, secret);
  const audience = accountSessionAudience(env);
  assertAccountSessionAudience(payload.aud, audience);
  assertAccountSessionFresh(payload, env, options.now);
  if (options.consumeReplay) {
    await consumeAccountSessionReplay(env, payload, options.now);
  }

  const user_id = normalizeAccountId(payload.user_id ?? payload.sub, "user_id");
  const session_id = nonEmptyOptionalString(payload.session_id ?? payload.sid);
  const workspaceIds = normalizedAccountWorkspaceIds(payload);
  const selectedWorkspace = selectedWorkspaceId(options.selectedWorkspaceId);
  const workspace_id = resolveSelectedAccountWorkspace(workspaceIds, selectedWorkspace, options.allowWorkspaceSelection);

  return withoutUndefined({
    user_id,
    workspace_id,
    workspace_ids: workspaceIds,
    session_id,
  });
}

export function accountSessionTokenFromRequest(
  request: Request,
  options: { allowHeaderFallback?: boolean } = {},
): string | null {
  // Browser OAuth redirects can only carry the hosted account session via cookie.
  // The explicit header remains a dev/test adapter and is used only when the cookie is absent.
  const cookieToken = readCookieValue(request.headers.get("cookie"), ACCOUNT_SESSION_COOKIE_NAME);
  if (cookieToken) {
    return cookieToken;
  }
  if (options.allowHeaderFallback) {
    const headerToken = request.headers.get(ACCOUNT_SESSION_HEADER)?.trim();
    return headerToken || null;
  }
  return null;
}

export function readCookieValue(cookieHeader: string | null | undefined, name: string): string | null {
  if (!cookieHeader || !isCookieName(name)) {
    return null;
  }
  for (const part of cookieHeader.split(";")) {
    const trimmed = part.trim();
    if (!trimmed) {
      continue;
    }
    const separatorIndex = trimmed.indexOf("=");
    const rawName = separatorIndex >= 0 ? trimmed.slice(0, separatorIndex).trim() : trimmed;
    if (rawName !== name) {
      continue;
    }
    const rawValue = separatorIndex >= 0 ? trimmed.slice(separatorIndex + 1).trim() : "";
    if (!rawValue || /[\r\n;]/.test(rawValue) || rawValue.length > 4096) {
      return null;
    }
    let decoded = rawValue;
    try {
      decoded = decodeURIComponent(rawValue);
    } catch {
      decoded = rawValue;
    }
    return decoded && !/[\r\n;]/.test(decoded) && decoded.length <= 4096 ? decoded : null;
  }
  return null;
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
    session_id: nonEmptyOptionalString(options.sessionId ?? grant.session_id),
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

interface AccountSessionPayload extends Record<string, unknown> {
  aud?: unknown;
  exp?: unknown;
  iat?: unknown;
  jti?: unknown;
  nonce?: unknown;
  user_id?: unknown;
  sub?: unknown;
  workspace_id?: unknown;
  workspace_ids?: unknown;
  session_id?: unknown;
  sid?: unknown;
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

export function hostedGrantProps(grant: HostedAccountGrant): YutomeAuthProps {
  return withoutUndefined({
    capsule: "hosted",
    workspace_id: grant.workspace_id,
    connector_grant_id: grant.grant_id,
    grant_id: grant.grant_id,
    user_id: grant.user_id,
    client_id: grant.client_id,
    session_id: grant.session_id,
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
  await env.OAUTH_KV.put(accountGrantKey(grant.grant_id), JSON.stringify(grant), kvTtlOptionsFromExpiresAt(grant.expires_at));
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
    session_id: nonEmptyOptionalString(value.session_id),
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

function assertGrantMatchesAuthorization(
  grant: HostedAccountGrant,
  requested: {
    user_id: string;
    workspace_id: string;
    client_id: string;
    scopes: string[];
    audience: string;
    token_version: string;
    session_id?: string;
  },
): void {
  const mismatches: string[] = [];
  compareOptionalString(requested.workspace_id, grant.workspace_id, "workspace_id", mismatches);
  compareOptionalString(requested.user_id, grant.user_id, "user_id", mismatches);
  compareOptionalString(requested.client_id, grant.client_id, "client_id", mismatches);
  compareOptionalString(requested.audience, grant.audience, "audience", mismatches);
  compareOptionalString(requested.token_version, grant.token_version, "token_version", mismatches);
  if ((requested.session_id ?? "") !== (grant.session_id ?? "")) {
    mismatches.push("session_id");
  }
  if (!sameStringSet(requested.scopes, grant.scopes)) {
    mismatches.push("scopes");
  }

  if (mismatches.length > 0) {
    throw new HostedAccountGrantError(
      "hosted_grant_mismatch",
      `Hosted MCP authorization does not match stored account grant: ${mismatches.join(", ")}.`,
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

async function verifyAccountSessionToken(token: string, secret: string): Promise<AccountSessionPayload> {
  const parts = token.split(".");
  if (parts.length !== 3 || parts[0] !== "v1" || !parts[1] || !parts[2]) {
    throw new HostedAccountGrantError(
      "hosted_account_session_invalid",
      "Hosted account session was not a signed v1 token.",
      401,
    );
  }
  const signed = `${parts[0]}.${parts[1]}`;
  const expected = await hmacSha256(signed, secret);
  const actual = decodeBase64Url(parts[2]);
  if (!constantTimeEqual(actual, expected)) {
    throw new HostedAccountGrantError(
      "hosted_account_session_invalid",
      "Hosted account session signature was invalid.",
      401,
    );
  }
  try {
    const parsed = JSON.parse(decodeUtf8(decodeBase64Url(parts[1])));
    if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
      return parsed as AccountSessionPayload;
    }
  } catch {
    // Normalize below so session parsing failures do not look like stored-grant failures.
  }
  throw new HostedAccountGrantError(
    "hosted_account_session_invalid",
    "Hosted account session payload was not valid JSON.",
    401,
  );
}

async function hmacSha256(value: string, secret: string): Promise<Uint8Array> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(value)));
}

function constantTimeEqual(actual: Uint8Array, expected: Uint8Array): boolean {
  let diff = actual.length ^ expected.length;
  const length = Math.max(actual.length, expected.length);
  for (let index = 0; index < length; index += 1) {
    diff |= (actual[index] ?? 0) ^ (expected[index] ?? 0);
  }
  return diff === 0;
}

function decodeBase64Url(value: string): Uint8Array {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) {
    throw new HostedAccountGrantError(
      "hosted_account_session_invalid",
      "Hosted account session contained invalid base64url data.",
      401,
    );
  }
  const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  const binary = atob(padded);
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

function decodeUtf8(value: Uint8Array): string {
  try {
    return new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(value);
  } catch {
    throw new HostedAccountGrantError(
      "hosted_account_session_invalid",
      "Hosted account session payload was not valid UTF-8.",
      401,
    );
  }
}

function accountSessionAudience(
  env: Pick<Env, "YUTOME_ACCOUNT_SESSION_AUDIENCE">,
): string {
  return env.YUTOME_ACCOUNT_SESSION_AUDIENCE?.trim() || DEFAULT_ACCOUNT_SESSION_AUDIENCE;
}

function accountSessionMaxAgeSeconds(env: AccountSessionEnv): number {
  return positiveIntegerEnv(env.YUTOME_ACCOUNT_SESSION_MAX_AGE_SECONDS, DEFAULT_ACCOUNT_SESSION_MAX_AGE_SECONDS);
}

function accountSessionClockSkewSeconds(env: AccountSessionEnv): number {
  return positiveIntegerEnv(env.YUTOME_ACCOUNT_SESSION_CLOCK_SKEW_SECONDS, DEFAULT_ACCOUNT_SESSION_CLOCK_SKEW_SECONDS);
}

async function consumeAccountSessionReplay(
  env: AccountSessionEnv,
  payload: AccountSessionPayload,
  now: Date = new Date(),
): Promise<void> {
  const replayId = nonEmptyOptionalString(payload.jti) ?? nonEmptyOptionalString(payload.nonce);
  if (!replayId) {
    throw new HostedAccountGrantError(
      "hosted_account_session_invalid",
      "Hosted account session replay id is required.",
      401,
    );
  }
  if (!env.OAUTH_KV) {
    throw new HostedAccountGrantError(
      "hosted_account_session_invalid",
      "Hosted account session replay protection is not configured.",
      500,
    );
  }
  const key = `${ACCOUNT_SESSION_REPLAY_PREFIX}${replayId}`;
  const existing = await env.OAUTH_KV.get(key);
  if (existing) {
    throw new HostedAccountGrantError(
      "hosted_account_session_invalid",
      "Hosted account session has already been used.",
      401,
    );
  }
  await env.OAUTH_KV.put(key, now.toISOString(), {
    expirationTtl: accountSessionReplayTtlSeconds(payload, env, now),
  });
}

function accountSessionReplayTtlSeconds(payload: AccountSessionPayload, env: AccountSessionEnv, now: Date): number {
  const nowSeconds = Math.floor(now.getTime() / 1000);
  const expSeconds = typeof payload.exp === "number" && Number.isFinite(payload.exp) ? payload.exp : nowSeconds;
  const maxAgeSeconds = accountSessionMaxAgeSeconds(env) + accountSessionClockSkewSeconds(env);
  const untilExpiry = Math.max(1, Math.ceil(expSeconds - nowSeconds));
  return Math.max(60, Math.min(untilExpiry, maxAgeSeconds));
}

function kvTtlOptionsFromExpiresAt(expiresAt: string | undefined): { expirationTtl: number } | undefined {
  if (!expiresAt) {
    return undefined;
  }
  const expiresAtMs = Date.parse(expiresAt);
  if (!Number.isFinite(expiresAtMs)) {
    return undefined;
  }
  const seconds = Math.ceil((expiresAtMs - Date.now()) / 1000);
  return { expirationTtl: Math.max(60, seconds) };
}

function positiveIntegerEnv(value: string | undefined, fallback: number): number {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function assertAccountSessionAudience(value: unknown, expected: string): void {
  const audiences = Array.isArray(value)
    ? value.map((entry) => nonEmptyOptionalString(entry)).filter(isString)
    : typeof value === "string"
      ? [value.trim()]
      : [];
  if (!audiences.includes(expected)) {
    throw new HostedAccountGrantError(
      "hosted_account_session_invalid",
      `Hosted account session audience must be ${expected}.`,
      401,
    );
  }
}

function assertAccountSessionFresh(payload: AccountSessionPayload, env: AccountSessionEnv, now: Date = new Date()): void {
  if (typeof payload.exp !== "number" || !Number.isFinite(payload.exp)) {
    throw new HostedAccountGrantError(
      "hosted_account_session_invalid",
      "Hosted account session expiry is required.",
      401,
    );
  }
  if (typeof payload.iat !== "number" || !Number.isFinite(payload.iat)) {
    throw new HostedAccountGrantError(
      "hosted_account_session_invalid",
      "Hosted account session issued-at time is required.",
      401,
    );
  }
  const nowSeconds = Math.floor(now.getTime() / 1000);
  const skewSeconds = accountSessionClockSkewSeconds(env);
  const maxAgeSeconds = accountSessionMaxAgeSeconds(env);
  if (payload.exp <= nowSeconds - skewSeconds) {
    throw new HostedAccountGrantError(
      "hosted_account_session_invalid",
      "Hosted account session has expired.",
      401,
    );
  }
  if (payload.iat > nowSeconds + skewSeconds) {
    throw new HostedAccountGrantError(
      "hosted_account_session_invalid",
      "Hosted account session was issued in the future.",
      401,
    );
  }
  if (nowSeconds - payload.iat > maxAgeSeconds + skewSeconds) {
    throw new HostedAccountGrantError(
      "hosted_account_session_invalid",
      "Hosted account session is older than the allowed max age.",
      401,
    );
  }
}

function normalizedAccountWorkspaceIds(payload: AccountSessionPayload): string[] {
  const candidates = [
    payload.workspace_id,
    ...(Array.isArray(payload.workspace_ids) ? payload.workspace_ids : []),
  ];
  return [...new Set(candidates.map((value) => optionalTenantId(value, "workspace_id")).filter(isString))];
}

function selectedWorkspaceId(value: unknown): string | undefined {
  return optionalTenantId(value, "workspace_id");
}

function resolveSelectedAccountWorkspace(
  workspaceIds: string[],
  selected: string | undefined,
  allowWorkspaceSelection = false,
): string {
  if (workspaceIds.length === 0) {
    throw new HostedAccountGrantError(
      "hosted_account_session_invalid",
      "Hosted account session did not include any workspaces.",
      401,
    );
  }
  if (selected) {
    if (!workspaceIds.includes(selected)) {
      throw new HostedAccountGrantError(
        "hosted_account_workspace_mismatch",
        "Selected workspace is not available in the signed Yutome account session.",
        403,
      );
    }
    return selected;
  }
  if (workspaceIds.length === 1) {
    return workspaceIds[0];
  }
  if (allowWorkspaceSelection) {
    return workspaceIds[0];
  }
  throw new HostedAccountGrantError(
    "hosted_account_workspace_mismatch",
    "Choose a Yutome workspace before approving hosted MCP access.",
    400,
  );
}

function optionalTenantId(value: unknown, field: "workspace_id"): string | undefined {
  if (value === undefined || value === null || String(value).trim() === "") {
    return undefined;
  }
  try {
    return normalizeTenantId(value, field);
  } catch (err) {
    if (err instanceof HostedAccountGrantError) {
      throw err;
    }
    throw new HostedAccountGrantError(
      "hosted_account_session_invalid",
      err instanceof Error ? err.message : `Hosted account session ${field} is invalid.`,
      401,
    );
  }
}

function normalizeAccountId(value: unknown, field: string): string {
  return nonEmptyString(value, field);
}

function normalizeGrantId(value: unknown): string {
  return nonEmptyString(value, "grant_id");
}

function normalizeRequiredScopes(value: unknown): string[] {
  const scopes = normalizeScopes(value);
  if (!scopes.includes(YUTOME_MCP_SCOPE)) {
    throw new HostedAccountGrantError(
      "hosted_grant_invalid",
      `Hosted MCP authorization requests must include the ${YUTOME_MCP_SCOPE} scope.`,
      400,
    );
  }
  return scopes;
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

function isCookieName(value: string): boolean {
  return /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/.test(value);
}

function withoutUndefined<T extends Record<string, unknown>>(value: T): T {
  return Object.fromEntries(
    Object.entries(value).filter(([, entryValue]) => entryValue !== undefined),
  ) as T;
}
