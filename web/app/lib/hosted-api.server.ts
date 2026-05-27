// Server-only client for the hosted FastAPI ("hosted API") on Railway. Holds
// the dashboard service token and forwards the verified-by-the-API session
// token. The Python API derives the workspace from the session token, so this
// BFF never trusts a client-supplied workspace id.
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

export interface WorkspaceSummary {
  ok: true;
  state: "active" | "no_active_plan";
  plan_key: string | null;
  workspace: { id: string; name: string | null };
  period: { start_at: string; end_at: string } | null;
  units: WorkspaceUnit[];
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

// --- Retrieval (session-authenticated dashboard search/read) ---------------
// These call /account/search and /account/show, which the hosted API serves
// from the same query adapter as the MCP endpoint, scoped to the session's
// workspace. The agent-facing /tools/call contract is untouched.

export type SearchMode = "lexical" | "semantic" | "hybrid";

export interface SearchHit {
  chunk_id: string;
  resource_uri: string;
  video_id: string;
  youtube_url: string;
  start_ms?: number;
  end_ms?: number;
  snippet?: string;
  transcript_version_id?: string;
  match_type?: string;
  scores?: Record<string, number>;
  title?: string;
  channel_id?: string;
  channel_handle?: string;
  channel_title?: string;
  published_at?: string;
  duration_seconds?: number;
  thumbnail_url?: string;
}

export interface SearchResult {
  rows: SearchHit[];
  notes?: unknown;
  total?: number | null;
}

export interface VideoResource {
  video_id: string;
  youtube_video_id?: string;
  youtube_url: string;
  active_transcript_version_id?: string;
  channel_id?: string;
  channel_title?: string;
  channel_handle?: string;
  title?: string;
  description?: string;
  published_at?: string;
  duration_seconds?: number;
  thumbnail_url?: string;
  active_chunk_count?: number;
}

export interface TranscriptResource {
  transcript_version_id: string;
  video_id?: string;
  youtube_video_id?: string;
  language?: string;
  segment_count: number;
  offset: number;
  limit: number | null;
  returned_segments: number;
  next_offset: number | null;
  text: string;
  text_truncated?: boolean;
}

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
  const result = (json.result ?? {}) as Partial<SearchResult>;
  return { rows: Array.isArray(result.rows) ? result.rows : [], notes: result.notes, total: result.total ?? null };
}

export async function showVideo(env: YutomeWebEnv, sessionToken: string, videoId: string): Promise<VideoResource> {
  const json = await authedPost(env, sessionToken, "/account/show", { kind: "video", id: videoId });
  return json.result as VideoResource;
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
  const result = (json.result ?? {}) as Partial<TranscriptResource>;
  return {
    ...result,
    transcript_version_id:
      typeof result.transcript_version_id === "string" ? result.transcript_version_id : transcriptVersionId,
    segment_count: typeof result.segment_count === "number" ? result.segment_count : 0,
    offset: typeof result.offset === "number" ? result.offset : opts.offset ?? 0,
    limit: typeof result.limit === "number" ? result.limit : null,
    returned_segments: typeof result.returned_segments === "number" ? result.returned_segments : 0,
    next_offset: typeof result.next_offset === "number" ? result.next_offset : null,
    text: typeof result.text === "string" ? result.text : "",
  };
}
