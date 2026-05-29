// Server-only client for the hosted FastAPI ("hosted API") on Railway. Holds
// the dashboard service token and forwards the verified-by-the-API session
// token. The Python API derives the workspace from the session token, so this
// BFF never trusts a client-supplied workspace id.
import { z } from "zod";

import type { YutomeWebEnv } from "./env.server";

export class HostedApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = "HostedApiError";
  }
}

async function parseJson(response: Response): Promise<Record<string, unknown>> {
  const text = await response.text();
  if (!text) return {};
  try {
    const parsed = JSON.parse(text);
    return typeof parsed === "object" && parsed !== null ? (parsed as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

function toError(status: number, body: Record<string, unknown>): HostedApiError {
  // FastAPI HTTPException serializes as { detail: { code, message, ... } }.
  const detail = (body.detail ?? body) as Record<string, unknown>;
  const code =
    typeof detail.code === "string"
      ? detail.code
      : typeof body.error === "string"
        ? body.error
        : "hosted_api_error";
  const message =
    typeof detail.message === "string"
      ? detail.message
      : typeof body.message === "string"
        ? body.message
        : "Hosted API request failed.";
  return new HostedApiError(status, code, message);
}

function apiUrl(env: YutomeWebEnv, path: string): string {
  return env.YUTOME_HOSTED_API_URL.replace(/\/+$/, "") + path;
}

export interface LoginSession {
  token: string;
  expires_at: string;
  max_age_seconds: number;
  cookie_name: string;
}

export interface StartLoginResult {
  ok: true;
  email: string;
  email_sent: boolean;
  // Present only when the API runs with YUTOME_AUTH_DEV_RETURN_LINK (local dev);
  // never returned in production, where the link is delivered by email.
  verify_link?: string;
}

// Sends a single-use sign-in link to the email. Does NOT create a session — the
// session is minted only when the emailed token is verified (see verifyLogin).
export async function startLogin(
  env: YutomeWebEnv,
  body: { email: string; name?: string; workspace_name?: string; redirect_path?: string },
): Promise<StartLoginResult> {
  const response = await fetch(apiUrl(env, "/account/login/start"), {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.YUTOME_DASHBOARD_API_TOKEN}`,
      "content-type": "application/json",
      accept: "application/json",
    },
    body: JSON.stringify(body),
  });
  const json = await parseJson(response);
  if (!response.ok || json.ok === false) throw toError(response.status, json);
  return {
    ok: true,
    email: typeof json.email === "string" ? json.email : body.email,
    email_sent: json.email_sent !== false,
    verify_link: typeof json.verify_link === "string" ? json.verify_link : undefined,
  };
}

export interface VerifyLoginResult {
  session: LoginSession;
  redirect_path: string | null;
}

// Redeems a single-use sign-in token and returns the session to set as a cookie.
export async function verifyLogin(env: YutomeWebEnv, token: string): Promise<VerifyLoginResult> {
  const response = await fetch(apiUrl(env, "/account/login/verify"), {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.YUTOME_DASHBOARD_API_TOKEN}`,
      "content-type": "application/json",
      accept: "application/json",
    },
    body: JSON.stringify({ token }),
  });
  const json = await parseJson(response);
  if (!response.ok || json.ok === false) throw toError(response.status, json);
  const session = json.session as LoginSession | undefined;
  if (!session || typeof session.token !== "string" || typeof session.max_age_seconds !== "number") {
    throw new HostedApiError(502, "invalid_hosted_api_response", "Verify response was missing a session token.");
  }
  return { session, redirect_path: typeof json.redirect_path === "string" ? json.redirect_path : null };
}

export interface StartGoogleSignInResult {
  ok: true;
  authorization_url: string;
  scopes: string[];
  expires_at: string;
}

export async function startGoogleSignIn(
  env: YutomeWebEnv,
  body: { redirect_uri: string; redirect_path?: string | null },
): Promise<StartGoogleSignInResult> {
  const response = await fetch(apiUrl(env, "/account/google/authorize"), {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.YUTOME_DASHBOARD_API_TOKEN}`,
      "content-type": "application/json",
      accept: "application/json",
    },
    body: JSON.stringify(body),
  });
  const json = await parseJson(response);
  if (!response.ok || json.ok === false) throw toError(response.status, json);
  if (typeof json.authorization_url !== "string") {
    throw new HostedApiError(502, "invalid_hosted_api_response", "Google authorization response was missing a URL.");
  }
  return {
    ok: true,
    authorization_url: json.authorization_url,
    scopes: Array.isArray(json.scopes) ? json.scopes.filter((scope): scope is string => typeof scope === "string") : [],
    expires_at: typeof json.expires_at === "string" ? json.expires_at : "",
  };
}

export interface CompleteGoogleSignInResult {
  session: LoginSession;
  redirect_path: string | null;
}

export async function completeGoogleSignIn(
  env: YutomeWebEnv,
  body: { code: string; state: string; redirect_uri: string },
): Promise<CompleteGoogleSignInResult> {
  const response = await fetch(apiUrl(env, "/account/google/callback"), {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.YUTOME_DASHBOARD_API_TOKEN}`,
      "content-type": "application/json",
      accept: "application/json",
    },
    body: JSON.stringify(body),
  });
  const json = await parseJson(response);
  if (!response.ok || json.ok === false) throw toError(response.status, json);
  const session = json.session as LoginSession | undefined;
  if (!session || typeof session.token !== "string" || typeof session.max_age_seconds !== "number") {
    throw new HostedApiError(502, "invalid_hosted_api_response", "Google sign-in response was missing a session token.");
  }
  return { session, redirect_path: typeof json.redirect_path === "string" ? json.redirect_path : null };
}

export interface CliAuthorizeResult {
  code: string;
  state?: string | null;
  workspace_id: string;
  expires_at: string;
}

export async function authorizeCli(
  env: YutomeWebEnv,
  sessionToken: string,
  body: {
    code_challenge: string;
    code_challenge_method: "S256";
    redirect_uri: string;
    state?: string | null;
    scopes?: string[];
    client_id?: string;
  },
): Promise<CliAuthorizeResult> {
  const response = await fetch(apiUrl(env, "/account/cli/authorize"), {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.YUTOME_DASHBOARD_API_TOKEN}`,
      "x-yutome-account-session": sessionToken,
      "content-type": "application/json",
      accept: "application/json",
    },
    body: JSON.stringify(body),
  });
  const json = await parseJson(response);
  if (!response.ok || json.ok === false) throw toError(response.status, json);
  if (typeof json.code !== "string" || typeof json.workspace_id !== "string" || typeof json.expires_at !== "string") {
    throw new HostedApiError(502, "invalid_hosted_api_response", "CLI authorization response was missing a code.");
  }
  return {
    code: json.code,
    state: typeof json.state === "string" ? json.state : null,
    workspace_id: json.workspace_id,
    expires_at: json.expires_at,
  };
}

async function authedGet(env: YutomeWebEnv, sessionToken: string, path: string): Promise<Record<string, unknown>> {
  const response = await fetch(apiUrl(env, path), {
    headers: {
      authorization: `Bearer ${env.YUTOME_DASHBOARD_API_TOKEN}`,
      "x-yutome-account-session": sessionToken,
      accept: "application/json",
    },
  });
  const json = await parseJson(response);
  if (!response.ok || json.ok === false) throw toError(response.status, json);
  return json;
}

async function authedPost(
  env: YutomeWebEnv,
  sessionToken: string,
  path: string,
  body: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const response = await fetch(apiUrl(env, path), {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.YUTOME_DASHBOARD_API_TOKEN}`,
      "x-yutome-account-session": sessionToken,
      "content-type": "application/json",
      accept: "application/json",
    },
    body: JSON.stringify(body),
  });
  const json = await parseJson(response);
  if (!response.ok || json.ok === false) throw toError(response.status, json);
  return json;
}

export interface WorkspaceUnit {
  unit: string;
  included: number | string | null;
  used: number | string | null;
  reserved: number | string | null;
  remaining: number | string | null;
  unlimited: boolean;
}

export interface WorkspaceEntitlement {
  key: string;
  label: string;
  description: string;
  format: "count" | "minutes" | "bytes" | "ratio";
  included: number | null;
  used: number;
  remaining: number | null;
  unlimited: boolean;
  percent: number | null;
}

export interface WorkspaceSummary {
  ok: true;
  state: "active" | "no_active_plan";
  plan_key: string | null;
  workspace: { id: string; name: string | null };
  period: { start_at: string; end_at: string } | null;
  units: WorkspaceUnit[];
  entitlements: WorkspaceEntitlement[];
  ai_spend_usd: number | null;
}

export interface LibraryOverview {
  ok: true;
  counts: { videos: number; channels: number; sources: number };
  recent: Array<{
    video_id: string;
    title: string | null;
    channel_id: string | null;
    published_at: string | null;
    duration_seconds: number | null;
  }>;
}

export interface ConnectedAssistant {
  grant_id: string;
  client_id: string | null;
  scopes: string[];
  audience: string | null;
  status: string;
  token_version: number | null;
  created_at: string | null;
  last_used_at: string | null;
  expires_at: string | null;
}

export interface SourceImportDescriptor {
  source_url?: string;
  url?: string;
  value?: string;
  source_type?: string;
  display_name?: string;
  title?: string;
  channel_id?: string;
  playlist_id?: string;
  video_id?: string;
  import_source?: string;
  selected?: boolean;
  metadata?: Record<string, unknown>;
}

export interface SourceImportResult {
  ok: true;
  workspace_id: string;
  imported: Array<{
    source_id: string;
    source_type: string;
    source_url: string;
    canonical_video_id: string | null;
    canonical_channel_id: string | null;
    canonical_playlist_id: string | null;
  }>;
  jobs: SourceJob[];
  refresh_policies: Array<{
    refresh_policy_id: string;
    source_id: string;
    enabled: boolean;
    cadence_seconds: number;
  }>;
}

export interface SourceJob {
  job_id: string;
  workspace_id?: string;
  source_id: string | null;
  job_type: string;
  status: string;
  priority: number | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  cancelled_at: string | null;
  error_code: string | null;
  error_message: string | null;
  metadata: Record<string, unknown>;
  // Human context joined in by /account/source-jobs (sources + videos) for the
  // Activity feed; null when the job has no source row or isn't an index_video.
  source_display_name?: string | null;
  source_type?: string | null;
  source_url?: string | null;
  video_title?: string | null;
}

export interface YoutubeGrantSummary {
  grant_id: string;
  status: string;
  scopes: string[];
  created_at: string | null;
  updated_at: string | null;
  last_used_at: string | null;
  expires_at: string | null;
  connected_at: string | null;
  access_token_expires_at: string | null;
}

export interface YoutubeConnectionStatus {
  ok: true;
  configured: boolean;
  connected: boolean;
  scope: string;
  grant: YoutubeGrantSummary | null;
}

export interface YoutubeSubscriptionChannel {
  channel_id: string;
  title: string | null;
  source_url: string;
  selected: boolean;
}

export interface YoutubeSubscriptionsResult {
  ok: true;
  workspace_id: string;
  grant: YoutubeGrantSummary;
  channels: YoutubeSubscriptionChannel[];
}

export interface YoutubeAuthorizationResult {
  ok: true;
  authorization_url: string;
  grant_id: string;
  scope: string;
  expires_at: string;
}

export function getSummary(env: YutomeWebEnv, sessionToken: string): Promise<WorkspaceSummary> {
  return authedGet(env, sessionToken, "/account/summary") as unknown as Promise<WorkspaceSummary>;
}

export function getLibrary(env: YutomeWebEnv, sessionToken: string): Promise<LibraryOverview> {
  return authedGet(env, sessionToken, "/account/library") as unknown as Promise<LibraryOverview>;
}

export async function getAssistants(env: YutomeWebEnv, sessionToken: string): Promise<ConnectedAssistant[]> {
  const json = await authedGet(env, sessionToken, "/account/assistants");
  return Array.isArray(json.assistants) ? (json.assistants as ConnectedAssistant[]) : [];
}

export function createSources(
  env: YutomeWebEnv,
  sessionToken: string,
  body: {
    sources: SourceImportDescriptor[];
    cadence_seconds?: number;
    max_new_videos?: number;
    refresh_enabled?: boolean;
  },
): Promise<SourceImportResult> {
  return authedPost(env, sessionToken, "/account/sources", body) as unknown as Promise<SourceImportResult>;
}

export async function getSourceJobs(
  env: YutomeWebEnv,
  sessionToken: string,
  limit = 25,
): Promise<SourceJob[]> {
  const json = await authedGet(env, sessionToken, `/account/source-jobs?limit=${encodeURIComponent(String(limit))}`);
  return Array.isArray(json.jobs) ? (json.jobs as SourceJob[]) : [];
}

export function getYoutubeStatus(
  env: YutomeWebEnv,
  sessionToken: string,
): Promise<YoutubeConnectionStatus> {
  return authedGet(env, sessionToken, "/account/youtube/status") as unknown as Promise<YoutubeConnectionStatus>;
}

export function startYoutubeAuthorization(
  env: YutomeWebEnv,
  sessionToken: string,
  redirectUri: string,
): Promise<YoutubeAuthorizationResult> {
  return authedPost(env, sessionToken, "/account/youtube/authorize", {
    redirect_uri: redirectUri,
  }) as unknown as Promise<YoutubeAuthorizationResult>;
}

export function completeYoutubeAuthorization(
  env: YutomeWebEnv,
  sessionToken: string,
  body: { code: string; state: string; redirect_uri: string },
): Promise<YoutubeConnectionStatus> {
  return authedPost(env, sessionToken, "/account/youtube/callback", body) as unknown as Promise<YoutubeConnectionStatus>;
}

export async function getYoutubeSubscriptions(
  env: YutomeWebEnv,
  sessionToken: string,
  limit = 250,
): Promise<YoutubeSubscriptionChannel[]> {
  const json = (await authedGet(
    env,
    sessionToken,
    `/account/youtube/subscriptions?limit=${encodeURIComponent(String(limit))}`,
  )) as unknown as YoutubeSubscriptionsResult;
  return Array.isArray(json.channels) ? json.channels : [];
}

export function importYoutubeSubscriptions(
  env: YutomeWebEnv,
  sessionToken: string,
  body: {
    channel_ids: string[];
    cadence_seconds?: number;
    max_new_videos?: number;
    refresh_enabled?: boolean;
  },
): Promise<SourceImportResult> {
  return authedPost(env, sessionToken, "/account/youtube/subscriptions/import", body) as unknown as Promise<SourceImportResult>;
}

export function revokeYoutubeConnection(
  env: YutomeWebEnv,
  sessionToken: string,
): Promise<{ ok: true; revoked: boolean; grant_id?: string }> {
  return authedPost(env, sessionToken, "/account/youtube/revoke", {}) as unknown as Promise<{
    ok: true;
    revoked: boolean;
    grant_id?: string;
  }>;
}

// --- Billing (Stripe Checkout + Customer Portal) ----------------------------
// Both endpoints take no body, are scoped to the session's workspace by the
// hosted API, and return a Stripe-hosted URL the browser is then redirected to.

export interface BillingRedirect {
  ok: true;
  url: string;
}

async function billingUrl(
  env: YutomeWebEnv,
  sessionToken: string,
  path: "/billing/checkout" | "/billing/portal",
): Promise<BillingRedirect> {
  const json = await authedPost(env, sessionToken, path, {});
  if (typeof json.url !== "string") {
    throw new HostedApiError(502, "invalid_hosted_api_response", "Billing response was missing a Stripe url.");
  }
  return { ok: true, url: json.url };
}

// Creates a Stripe Checkout session for the $4/mo Personal plan (14-day trial).
export function startBillingCheckout(env: YutomeWebEnv, sessionToken: string): Promise<BillingRedirect> {
  return billingUrl(env, sessionToken, "/billing/checkout");
}

// Opens the Stripe Customer Portal so an already-subscribed workspace can manage
// billing. The hosted API returns 409 (stripe_customer_not_found) if the
// workspace has never subscribed.
export function openBillingPortal(env: YutomeWebEnv, sessionToken: string): Promise<BillingRedirect> {
  return billingUrl(env, sessionToken, "/billing/portal");
}

// --- Retrieval & browse (session-authenticated dashboard search/read) -------
// These call /account/{search,show,list}, served from the same query adapter as
// the MCP endpoint, scoped to the session's workspace. Responses are parsed with
// zod into inferred types, so hosted-API contract drift fails here with a clear
// 502 instead of surfacing as `undefined` deep inside a route. Requests stay
// plain typed objects — the Python API is the authoritative validator for inputs.

export type SearchMode = "lexical" | "semantic" | "hybrid";

function parseResult<S extends z.ZodTypeAny>(
  schema: S,
  json: Record<string, unknown>,
  context: string,
): z.infer<S> {
  const parsed = schema.safeParse(json.result);
  if (!parsed.success) {
    throw new HostedApiError(502, "invalid_hosted_api_response", `Unexpected ${context} response from the hosted API.`);
  }
  return parsed.data;
}

const searchHitSchema = z.object({
  chunk_id: z.string(),
  resource_uri: z.string().optional(),
  video_id: z.string(),
  youtube_url: z.string(),
  start_ms: z.number().optional(),
  end_ms: z.number().optional(),
  snippet: z.string().optional(),
  transcript_version_id: z.string().optional(),
  match_type: z.string().optional(),
  scores: z.record(z.string(), z.number()).optional(),
  title: z.string().optional(),
  channel_id: z.string().optional(),
  channel_handle: z.string().optional(),
  channel_title: z.string().optional(),
  published_at: z.string().optional(),
  duration_seconds: z.number().optional(),
  thumbnail_url: z.string().optional(),
});

const searchResultSchema = z.object({
  rows: z.array(searchHitSchema).default([]),
  notes: z.unknown().optional(),
  total: z.number().nullish(),
});

const videoResourceSchema = z.object({
  video_id: z.string(),
  youtube_video_id: z.string().optional(),
  youtube_url: z.string().optional(),
  active_transcript_version_id: z.string().optional(),
  channel_id: z.string().optional(),
  channel_title: z.string().optional(),
  channel_handle: z.string().optional(),
  title: z.string().optional(),
  description: z.string().optional(),
  published_at: z.string().optional(),
  duration_seconds: z.number().optional(),
  thumbnail_url: z.string().optional(),
  active_chunk_count: z.number().optional(),
});

// `.default()` so a `_compact`-stripped field (the hosted API drops nulls)
// becomes a stable value the transcript reader can rely on.
const transcriptResourceSchema = z.object({
  transcript_version_id: z.string().optional(),
  video_id: z.string().optional(),
  youtube_video_id: z.string().optional(),
  language: z.string().optional(),
  segment_count: z.number().default(0),
  offset: z.number().default(0),
  limit: z.number().nullable().default(null),
  returned_segments: z.number().default(0),
  next_offset: z.number().nullable().default(null),
  text: z.string().default(""),
  text_truncated: z.boolean().optional(),
});

const channelListItemSchema = z.object({
  channel_id: z.string(),
  resource_uri: z.string().optional(),
  title: z.string().nullish(),
  channel_handle: z.string().nullish(),
  selected: z.boolean().nullish(),
  video_count: z.number().nullish(),
  latest_published_at: z.string().nullish(),
  source_count: z.number().nullish(),
});

const channelResourceSchema = z.object({
  channel_id: z.string(),
  resource_uri: z.string().optional(),
  title: z.string().nullish(),
  channel_handle: z.string().nullish(),
  video_count: z.number().nullish(),
  latest_published_at: z.string().nullish(),
  source_ids: z.array(z.string()).optional(),
});

const videoListSchema = z.object({ rows: z.array(videoResourceSchema).default([]) });
const channelListSchema = z.object({ rows: z.array(channelListItemSchema).default([]) });

export type SearchHit = z.infer<typeof searchHitSchema>;
export type SearchResult = z.infer<typeof searchResultSchema>;
export type VideoResource = z.infer<typeof videoResourceSchema>;
export type TranscriptResource = z.infer<typeof transcriptResourceSchema>;
export type ChannelListItem = z.infer<typeof channelListItemSchema>;
export type ChannelResource = z.infer<typeof channelResourceSchema>;

export async function searchFind(
  env: YutomeWebEnv,
  sessionToken: string,
  args: {
    text: string;
    mode?: SearchMode;
    in?: string;
    channel?: string;
    since?: string;
    until?: string;
    source?: string;
    language?: string;
    group_by?: string;
    project?: string;
    limit?: number;
    offset?: number;
  },
): Promise<SearchResult> {
  const json = await authedPost(env, sessionToken, "/account/search", args as Record<string, unknown>);
  return parseResult(searchResultSchema, json, "search");
}

export async function showVideo(env: YutomeWebEnv, sessionToken: string, videoId: string): Promise<VideoResource> {
  const json = await authedPost(env, sessionToken, "/account/show", { kind: "video", id: videoId });
  return parseResult(videoResourceSchema, json, "video");
}

export async function showTranscript(
  env: YutomeWebEnv,
  sessionToken: string,
  transcriptVersionId: string,
  opts: { offset?: number; limit?: number } = {},
): Promise<TranscriptResource> {
  const json = await authedPost(env, sessionToken, "/account/show", {
    kind: "transcript",
    id: transcriptVersionId,
    transcript_offset: opts.offset,
    transcript_limit: opts.limit,
  });
  const transcript = parseResult(transcriptResourceSchema, json, "transcript");
  return { ...transcript, transcript_version_id: transcript.transcript_version_id ?? transcriptVersionId };
}

// `list` has no total count, so callers fetch limit+1 to detect a next page.
// The hosted adapter caps limit at 200.
export async function listVideos(
  env: YutomeWebEnv,
  sessionToken: string,
  opts: { channel?: string; order_by?: string; limit?: number; offset?: number } = {},
): Promise<VideoResource[]> {
  const json = await authedPost(env, sessionToken, "/account/list", {
    entity: "videos",
    channel: opts.channel,
    order_by: opts.order_by,
    limit: opts.limit,
    offset: opts.offset,
  });
  return parseResult(videoListSchema, json, "video list").rows;
}

export async function listChannels(
  env: YutomeWebEnv,
  sessionToken: string,
  opts: { channel?: string; selected?: boolean; limit?: number; offset?: number } = {},
): Promise<ChannelListItem[]> {
  const json = await authedPost(env, sessionToken, "/account/list", {
    entity: "channels",
    channel: opts.channel,
    selected: opts.selected,
    limit: opts.limit,
    offset: opts.offset,
  });
  return parseResult(channelListSchema, json, "channel list").rows;
}

export async function showChannel(
  env: YutomeWebEnv,
  sessionToken: string,
  channelId: string,
): Promise<ChannelResource> {
  const json = await authedPost(env, sessionToken, "/account/show", { kind: "channel", id: channelId });
  return parseResult(channelResourceSchema, json, "channel");
}
