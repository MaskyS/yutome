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

export interface BootstrapResult {
  session: { token: string; expires_at: string; max_age_seconds: number; cookie_name: string };
  principal: unknown;
}

export async function bootstrapAccount(
  env: YutomeWebEnv,
  body: { email: string; name?: string; workspace_name?: string },
): Promise<BootstrapResult> {
  const response = await fetch(apiUrl(env, "/account/bootstrap"), {
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
  const session = json.session as BootstrapResult["session"] | undefined;
  if (!session || typeof session.token !== "string" || typeof session.max_age_seconds !== "number") {
    throw new HostedApiError(502, "invalid_hosted_api_response", "Bootstrap response was missing a session token.");
  }
  return { session, principal: json.principal };
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
