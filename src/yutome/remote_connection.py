from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from yutome.db import catalog_is_initialized, connect_catalog
from yutome.paths import ProjectPaths


SCHEMA_VERSION = 1
REMOTE_STATE_DIRNAME = "remote"
REMOTE_STATE_FILENAME = "connection.json"
CLOUDFLARE_WORKER_DIRNAME = "cloudflare-worker"
DEFAULT_WORKER_NAME = "yutome-remote-mcp"

Provider = Literal["cloudflare"]
RemoteMode = Literal["connector_only", "replica"]
PairingStatus = Literal["not_started", "paired"]

EXCLUDED_SECRET_CLASSES = (
    "local .env secrets file",
    "Google OAuth tokens",
    "Webshare and generic proxy credentials",
    "Gemini API keys",
    "local logs",
    "local caches",
    "local job internals",
)


@dataclass
class RemoteConnectionState:
    schema_version: int
    provider: Provider
    mode: RemoteMode
    endpoint_url: str
    mcp_url: str
    pairing_status: PairingStatus
    created_at: str
    updated_at: str
    last_desktop_seen_at: str | None = None
    relay_token: str | None = None
    pairing_code: str | None = None
    token_secret: str | None = None
    replica_enabled: bool = False
    last_sync_at: str | None = None
    semantic_replica: dict[str, Any] = field(default_factory=dict)
    cloud_resources: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RemoteConnectionState":
        return cls(
            schema_version=int(payload.get("schema_version", SCHEMA_VERSION)),
            provider=payload.get("provider", "cloudflare"),
            mode=payload.get("mode", "connector_only"),
            endpoint_url=str(payload.get("endpoint_url", "")),
            mcp_url=str(payload.get("mcp_url", "")),
            pairing_status=payload.get("pairing_status", "not_started"),
            created_at=str(payload.get("created_at", "")),
            updated_at=str(payload.get("updated_at", "")),
            last_desktop_seen_at=payload.get("last_desktop_seen_at"),
            relay_token=payload.get("relay_token"),
            pairing_code=payload.get("pairing_code"),
            token_secret=payload.get("token_secret"),
            replica_enabled=bool(payload.get("replica_enabled", False)),
            last_sync_at=payload.get("last_sync_at"),
            semantic_replica=dict(payload.get("semantic_replica") or {}),
            cloud_resources=dict(payload.get("cloud_resources") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CloudflareWorkerProject:
    root: Path
    worker_name: str
    deploy_command: list[str]
    files: tuple[Path, ...]
    relay_token: str
    pairing_code: str
    token_secret: str


def remote_state_path(paths: ProjectPaths) -> Path:
    return paths.data_dir / REMOTE_STATE_DIRNAME / REMOTE_STATE_FILENAME


def load_remote_state(paths: ProjectPaths) -> RemoteConnectionState | None:
    path = remote_state_path(paths)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"remote connection state must be a JSON object: {path}")
    return RemoteConnectionState.from_dict(payload)


def save_remote_state(paths: ProjectPaths, state: RemoteConnectionState) -> Path:
    path = remote_state_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def mark_desktop_seen(paths: ProjectPaths, *, when: str | None = None) -> Path | None:
    state = load_remote_state(paths)
    if state is None:
        return None
    now = _utc_now()
    state.last_desktop_seen_at = when or now
    state.updated_at = now
    return save_remote_state(paths, state)


def build_remote_state(
    *,
    endpoint: str,
    mode: RemoteMode = "connector_only",
    worker_name: str | None = None,
    relay_token: str | None = None,
    pairing_code: str | None = None,
    token_secret: str | None = None,
    existing: RemoteConnectionState | None = None,
) -> RemoteConnectionState:
    endpoint_url, mcp_url = normalize_endpoint(endpoint)
    now = _utc_now()
    cloud_resources = dict(existing.cloud_resources) if existing else {}
    if worker_name:
        cloud_resources["cloudflare_worker_name"] = worker_name
    return RemoteConnectionState(
        schema_version=SCHEMA_VERSION,
        provider="cloudflare",
        mode=mode,
        endpoint_url=endpoint_url,
        mcp_url=mcp_url,
        pairing_status=existing.pairing_status if existing else "not_started",
        created_at=existing.created_at if existing else now,
        updated_at=now,
        last_desktop_seen_at=existing.last_desktop_seen_at if existing else None,
        relay_token=relay_token or (existing.relay_token if existing else None),
        pairing_code=pairing_code or (existing.pairing_code if existing else None),
        token_secret=token_secret or (existing.token_secret if existing else None),
        replica_enabled=mode == "replica",
        last_sync_at=existing.last_sync_at if existing else None,
        semantic_replica=existing.semantic_replica if existing else {},
        cloud_resources=cloud_resources,
    )


def normalize_endpoint(endpoint: str) -> tuple[str, str]:
    raw = endpoint.strip()
    if not raw:
        raise ValueError("endpoint URL is required")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise ValueError("endpoint must be an absolute http(s) URL")

    path = parsed.path.rstrip("/")
    if path.endswith("/mcp"):
        base_path = path.removesuffix("/mcp")
        mcp_path = path
    else:
        base_path = path
        mcp_path = f"{path}/mcp" if path else "/mcp"

    endpoint_url = urlunsplit((parsed.scheme, parsed.netloc, base_path or "", "", ""))
    mcp_url = urlunsplit((parsed.scheme, parsed.netloc, mcp_path, "", ""))
    return endpoint_url.rstrip("/"), mcp_url


def remote_status_payload(paths: ProjectPaths) -> dict[str, Any]:
    state = load_remote_state(paths)
    if state is None:
        return {
            "configured": False,
            "mode": None,
            "provider": None,
            "endpoint_url": None,
            "mcp_url": None,
            "pairing_status": None,
            "desktop_connection": "not configured",
            "offline_search": "disabled",
        }
    if state.last_desktop_seen_at:
        desktop_connection = "online" if _recent_iso_timestamp(state.last_desktop_seen_at, seconds=60) else "offline"
    else:
        desktop_connection = "not seen yet"
    return {
        "configured": True,
        "mode": state.mode,
        "provider": state.provider,
        "endpoint_url": state.endpoint_url,
        "mcp_url": state.mcp_url,
        "pairing_status": state.pairing_status,
        "desktop_connection": desktop_connection,
        "last_desktop_seen_at": state.last_desktop_seen_at,
        "relay_token_configured": bool(state.relay_token),
        "pairing_code_configured": bool(state.pairing_code),
        "token_secret_configured": bool(state.token_secret),
        "replica_enabled": state.replica_enabled,
        "offline_search": "enabled" if state.replica_enabled else "disabled",
        "last_sync_at": state.last_sync_at,
        "semantic_replica": state.semantic_replica,
    }


def build_sync_dry_run_manifest(paths: ProjectPaths) -> dict[str, Any]:
    counts = {
        "channels": 0,
        "library_channels": 0,
        "videos": 0,
        "transcript_versions": 0,
        "chunks": 0,
        "embeddings": 0,
        "transcript_artifact_files": 0,
    }
    if catalog_is_initialized(paths.catalog_db):
        with connect_catalog(paths.catalog_db) as connection:
            for table in ("channels", "library_channels", "videos", "transcript_versions", "chunks", "embeddings"):
                row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
                counts[table] = int(row["count"] if row is not None else 0)
    transcript_root = paths.artifacts_dir / "videos"
    if transcript_root.exists():
        counts["transcript_artifact_files"] = sum(
            1 for path in transcript_root.glob("*/transcripts/*/*") if path.is_file()
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry_run",
        "would_sync": counts,
        "excluded_secret_classes": list(EXCLUDED_SECRET_CLASSES),
        "upload_performed": False,
    }


def cloudflare_worker_project_path(paths: ProjectPaths) -> Path:
    return paths.data_dir / REMOTE_STATE_DIRNAME / CLOUDFLARE_WORKER_DIRNAME


def prepare_cloudflare_worker_project(
    paths: ProjectPaths,
    *,
    worker_name: str = DEFAULT_WORKER_NAME,
    relay_token: str | None = None,
    pairing_code: str | None = None,
    token_secret: str | None = None,
) -> CloudflareWorkerProject:
    root = cloudflare_worker_project_path(paths)
    src_dir = root / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    effective_relay_token = relay_token or secrets.token_urlsafe(32)
    effective_pairing_code = pairing_code or secrets.token_hex(5).upper()
    effective_token_secret = token_secret or secrets.token_urlsafe(48)
    files = {
        root / "package.json": _package_json(worker_name),
        root / "wrangler.toml": _wrangler_toml(
            worker_name,
            relay_token=effective_relay_token,
            pairing_code=effective_pairing_code,
            token_secret=effective_token_secret,
        ),
        root / "README.md": _worker_readme(worker_name),
        src_dir / "index.js": _worker_index_js(),
    }
    for path, content in files.items():
        path.write_text(content, encoding="utf-8")
    return CloudflareWorkerProject(
        root=root,
        worker_name=worker_name,
        deploy_command=["npx", "--yes", "wrangler", "deploy"],
        files=tuple(sorted(files)),
        relay_token=effective_relay_token,
        pairing_code=effective_pairing_code,
        token_secret=effective_token_secret,
    )


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _recent_iso_timestamp(value: str, *, seconds: int) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(UTC) - parsed).total_seconds() <= seconds


def _package_json(worker_name: str) -> str:
    return json.dumps(
        {
            "name": worker_name,
            "private": True,
            "type": "module",
            "scripts": {
                "deploy": "wrangler deploy",
                "dev": "wrangler dev",
            },
            "devDependencies": {
                "wrangler": "^4.0.0",
            },
        },
        indent=2,
    ) + "\n"


def _wrangler_toml(worker_name: str, *, relay_token: str, pairing_code: str, token_secret: str) -> str:
    return f"""name = "{worker_name}"
main = "src/index.js"
compatibility_date = "2025-06-18"
workers_dev = true
keep_vars = true

[vars]
YUTOME_WORKER_MODE = "connector_only"
YUTOME_RELAY_TOKEN = "{relay_token}"
YUTOME_PAIRING_CODE = "{pairing_code}"
YUTOME_TOKEN_SECRET = "{token_secret}"
YUTOME_DEV_NO_AUTH = "false"

[[durable_objects.bindings]]
name = "RELAY"
class_name = "YutomeRelay"

[[migrations]]
tag = "v1"
new_sqlite_classes = [ "YutomeRelay" ]
"""


def _worker_readme(worker_name: str) -> str:
    return f"""# Yutome Remote MCP Worker

This Worker is generated by `yutome connect`.

Deploy it from this directory:

```bash
npx --yes wrangler deploy
```

After deployment, save the Worker URL in yutome:

```bash
uv run yutome connect --endpoint https://{worker_name}.<your-subdomain>.workers.dev
```

The generated Worker exposes:

- `GET /healthz`
- `GET /readyz`
- `GET /pair`
- `GET /.well-known/oauth-protected-resource`
- `GET /.well-known/oauth-authorization-server`
- `POST /register`
- `GET|POST /authorize`
- `POST /token`
- `POST /mcp`
- `GET /bridge/next`
- `POST /bridge/result`

This Worker is a deployable remote MCP endpoint with a laptop-backed relay. Run:

```bash
uv run yutome remote bridge
```

While the bridge is running, Claude/ChatGPT tool calls are answered from the local Yutome corpus.
If the bridge is not running, the Worker returns a clear Yutome Desktop offline response.

The MCP endpoint is OAuth-protected by default. Pair your browser once:

```text
https://{worker_name}.<your-subdomain>.workers.dev/pair
```

Use the pairing code printed by `yutome connect`.
"""


def _worker_index_js() -> str:
    return r'''const SERVER_INFO = {
  name: "yutome-remote-mcp",
  version: "0.1.0",
};

const SERVER_INSTRUCTIONS =
  "Yutome is a local-first YouTube channel knowledge base. Use find when the user asks a topic or semantic search question. " +
  "Use list when the user asks for newest videos, channels, corpus status, or attention rows; for newest videos, call list with entity=videos and order_by=newest. " +
  "Use show when the user asks for a specific video, transcript, source, citation, or surrounding context. " +
  "Use q only for advanced structured queries that do not fit find/list/show. " +
  "When the laptop bridge is offline, say Yutome Desktop is offline instead of inventing results.";

const READ_ONLY_TOOL_ANNOTATIONS = {
  readOnlyHint: true,
  openWorldHint: false,
};

const AUTH_SCOPE = "yutome.search.read";
const ACCESS_TOKEN_TTL_SECONDS = 12 * 60 * 60;
const REFRESH_TOKEN_TTL_SECONDS = 90 * 24 * 60 * 60;

const OAUTH_SECURITY_SCHEMES = [
  {
    type: "oauth2",
    scopes: [AUTH_SCOPE],
  },
];

const NOAUTH_SECURITY_SCHEMES = [
  {
    type: "noauth",
  },
];

const BASE_TOOL_SCHEMAS = [
  {
    name: "find",
    description: "Use this when the user asks to search their Yutome YouTube corpus by topic, phrase, meaning, channel, date, source, or transcript content. Do not use it for newest-video lists; use list instead.",
    securitySchemes: NOAUTH_SECURITY_SCHEMES,
    annotations: READ_ONLY_TOOL_ANNOTATIONS,
    inputSchema: {
      type: "object",
      properties: {
        text: { type: "string", description: "Search text, topic, question, or phrase." },
        in_: { type: "string", enum: ["chunks", "titles", "descriptions"], description: "Limit search to transcript chunks, video titles, or descriptions." },
        mode: { type: "string", enum: ["lexical", "semantic", "hybrid", "none"], description: "Search mode. Use hybrid by default when available." },
        channel: { type: "string", description: "Optional channel title or handle filter." },
        since: { type: "string", description: "Optional start date filter in YYYY-MM-DD form." },
        until: { type: "string", description: "Optional end date filter in YYYY-MM-DD form." },
        source: { type: "string", description: "Optional transcript/source filter." },
        language: { type: "string", description: "Optional language code filter." },
        group_by: { type: "string", enum: ["video", "channel", "transcript_source"], description: "Optional grouping for summarized results." },
        limit: { type: "integer", minimum: 1, maximum: 200, description: "Maximum result count." },
        offset: { type: "integer", minimum: 0, description: "Result offset for pagination." },
        project: { type: "string", description: "Optional project/corpus namespace." },
      },
      required: ["text"],
    },
  },
  {
    name: "list",
    description: "Use this when the user asks to list newest videos, channels, corpus status, selected items, or attention rows. For newest videos, use entity=videos, order_by=newest, and a small limit.",
    securitySchemes: NOAUTH_SECURITY_SCHEMES,
    annotations: READ_ONLY_TOOL_ANNOTATIONS,
    inputSchema: {
      type: "object",
      properties: {
        entity: { type: "string", enum: ["video", "videos", "channel", "channels", "attention", "status"], description: "Thing to list." },
        channel: { type: "string", description: "Optional channel title or handle filter." },
        since: { type: "string", description: "Optional start date filter in YYYY-MM-DD form." },
        until: { type: "string", description: "Optional end date filter in YYYY-MM-DD form." },
        status: { type: "string", description: "Optional indexing or attention status filter." },
        source: { type: "string", description: "Optional transcript/source filter." },
        language: { type: "string", description: "Optional language code filter." },
        selected: { type: "boolean", description: "Filter to selected library items where supported." },
        order_by: { type: "string", description: "Ordering hint such as newest, oldest, title, or updated." },
        limit: { type: "integer", minimum: 1, maximum: 200, description: "Maximum result count." },
        offset: { type: "integer", minimum: 0, description: "Result offset for pagination." },
        project: { type: "string", description: "Optional project/corpus namespace." },
      },
      required: ["entity"],
    },
  },
  {
    name: "show",
    description: "Use this when the user asks to open or inspect a specific Yutome chunk, video, channel, transcript, source, citation, or surrounding context.",
    securitySchemes: NOAUTH_SECURITY_SCHEMES,
    annotations: READ_ONLY_TOOL_ANNOTATIONS,
    inputSchema: {
      type: "object",
      properties: {
        kind: { type: "string", enum: ["chunk", "video", "channel", "transcript", "context", "source"], description: "Resource kind to show." },
        id: { type: "string", description: "Yutome resource id when known." },
        token_budget: { type: "integer", minimum: 200, maximum: 8000, description: "Maximum context size for transcript/context results." },
        video_id: { type: "string", description: "YouTube video id when showing a video/transcript/context." },
        time_seconds: { type: "integer", minimum: 0, description: "Timestamp in seconds for context lookup." },
        youtube_url: { type: "string", description: "YouTube URL when the user supplies a link instead of an id." },
      },
      required: ["kind"],
    },
  },
  {
    name: "q",
    description: "Use this only for advanced raw Yutome QueryRequest JSON when find, list, and show cannot express the request.",
    securitySchemes: NOAUTH_SECURITY_SCHEMES,
    annotations: READ_ONLY_TOOL_ANNOTATIONS,
    inputSchema: {
      type: "object",
      additionalProperties: true,
    },
  },
];

function toolSchemas(env) {
  const schemes = devNoAuth(env) ? NOAUTH_SECURITY_SCHEMES : OAUTH_SECURITY_SCHEMES;
  return BASE_TOOL_SCHEMAS.map((tool) => ({
    ...tool,
    securitySchemes: schemes,
  }));
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/healthz") {
      return json({
        ok: true,
        service: SERVER_INFO.name,
        mode: env.YUTOME_WORKER_MODE || "connector_only",
        mcp: "/mcp",
        bridge: "/bridge/next",
      });
    }
    if (request.method === "GET" && url.pathname === "/readyz") {
      return relayFetch(env, "/readyz", request);
    }
    if (request.method === "GET" && url.pathname === "/.well-known/oauth-protected-resource") {
      return protectedResourceMetadata(request);
    }
    if (request.method === "GET" && url.pathname === "/.well-known/oauth-authorization-server") {
      return authorizationServerMetadata(request);
    }
    if (url.pathname === "/") {
      return new Response("Yutome Remote MCP is deployed. Use /mcp as the connector URL. Use /pair to approve access.\n", {
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }
    if (
      url.pathname === "/pair" ||
      url.pathname === "/register" ||
      url.pathname === "/authorize" ||
      url.pathname === "/token"
    ) {
      return relayFetch(env, url.pathname, request);
    }
    if (url.pathname === "/bridge/next" || url.pathname === "/bridge/result") {
      return relayFetch(env, url.pathname, request);
    }
    if (url.pathname === "/mcp") {
      if (!devNoAuth(env)) {
        const unauthorized = await verifyMcpAuthorization(request, env);
        if (unauthorized) return unauthorized;
      }
      return handleMcp(request, env);
    }
    return json({ error: "not found" }, { status: 404 });
  },
};

export class YutomeRelay {
  constructor(state, env) {
    this.state = state;
    this.env = env;
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/readyz") {
      return this.readyz();
    }
    if (request.method === "POST" && url.pathname === "/enqueue") {
      return this.enqueue(request);
    }
    if (request.method === "GET" && url.pathname === "/bridge/next") {
      return this.next(request);
    }
    if (request.method === "POST" && url.pathname === "/bridge/result") {
      return this.result(request);
    }
    if (url.pathname === "/pair") {
      return this.pair(request);
    }
    if (request.method === "POST" && url.pathname === "/register") {
      return this.register(request);
    }
    if (url.pathname === "/authorize") {
      return this.authorize(request);
    }
    if (request.method === "POST" && url.pathname === "/token") {
      return this.token(request);
    }
    return json({ error: "not found" }, { status: 404 });
  }

  async readyz() {
    const lastSeen = await this.state.storage.get("last_desktop_seen_at");
    const online = Boolean(lastSeen && Date.now() - Date.parse(lastSeen) < 45_000);
    return json({
      ...offlinePayload({ last_desktop_seen_at: lastSeen || null }),
      ok: online,
      desktop_connection: online ? "online" : "offline",
      status: online ? "desktop_online" : "desktop_offline",
      bridge: "polling",
    });
  }

  async enqueue(request) {
    const params = await request.json();
    const jobId = crypto.randomUUID();
    const createdAt = new Date().toISOString();
    await this.state.storage.put(`job:${jobId}`, {
      job_id: jobId,
      params,
      created_at: createdAt,
    });

    const waitMs = clampInt(this.env.YUTOME_RELAY_WAIT_MS, 20_000, 1_000, 55_000);
    const deadline = Date.now() + waitMs;
    while (Date.now() < deadline) {
      const result = await this.state.storage.get(`result:${jobId}`);
      if (result) {
        await this.state.storage.delete(`result:${jobId}`);
        return json(result);
      }
      await sleep(250);
    }

    const lastSeen = await this.state.storage.get("last_desktop_seen_at");
    return json(offlineToolResult(params, {
      status: "desktop_timeout",
      last_desktop_seen_at: lastSeen || null,
      message: "Yutome Desktop did not answer this tool call before the connector timed out.",
    }));
  }

  async next(request) {
    const unauthorized = this.verifyRelay(request);
    if (unauthorized) return unauthorized;

    const now = new Date().toISOString();
    await this.state.storage.put("last_desktop_seen_at", now);
    const jobs = await this.state.storage.list({ prefix: "job:" });
    for (const [key, job] of jobs) {
      await this.state.storage.delete(key);
      return json({
        job_id: job.job_id,
        params: job.params,
        created_at: job.created_at,
      });
    }
    return new Response(null, { status: 204, headers: { "cache-control": "no-store" } });
  }

  async result(request) {
    const unauthorized = this.verifyRelay(request);
    if (unauthorized) return unauthorized;

    let payload;
    try {
      payload = await request.json();
    } catch {
      return json({ error: "invalid JSON" }, { status: 400 });
    }
    if (!payload || typeof payload.job_id !== "string" || !payload.result) {
      return json({ error: "job_id and result are required" }, { status: 400 });
    }
    await this.state.storage.put("last_desktop_seen_at", new Date().toISOString());
    await this.state.storage.put(`result:${payload.job_id}`, payload.result);
    return json({ ok: true });
  }

  async pair(request) {
    const url = new URL(request.url);
    const supplied = (url.searchParams.get("code") || "").trim().toUpperCase();
    if (supplied && this.verifyPairingCode(supplied)) {
      return await this.createOwnerSessionResponse("Yutome Remote MCP is paired. Return to Claude or ChatGPT and finish connecting.");
    }
    return html(pairingPage({ error: supplied ? "That pairing code was not accepted." : "" }));
  }

  async register(request) {
    let payload;
    try {
      payload = await request.json();
    } catch {
      return oauthError("invalid_client_metadata", "Registration metadata must be JSON.", 400);
    }
    const clientId = `yutome-client-${crypto.randomUUID()}`;
    await this.state.storage.put(`client:${clientId}`, {
      client_id: clientId,
      client_name: String(payload.client_name || "Yutome MCP client"),
      redirect_uris: Array.isArray(payload.redirect_uris) ? payload.redirect_uris : [],
      created_at: new Date().toISOString(),
    });
    return json(
      {
        client_id: clientId,
        client_id_issued_at: Math.floor(Date.now() / 1000),
        client_name: payload.client_name || "Yutome MCP client",
        redirect_uris: Array.isArray(payload.redirect_uris) ? payload.redirect_uris : [],
        grant_types: ["authorization_code", "refresh_token"],
        response_types: ["code"],
        token_endpoint_auth_method: "none",
      },
      { status: 201 },
    );
  }

  async authorize(request) {
    const url = new URL(request.url);
    if (request.method === "POST") {
      const form = await request.formData();
      const supplied = String(form.get("pairing_code") || "").trim().toUpperCase();
      if (!this.verifyPairingCode(supplied)) {
        return html(authorizePage(url, { error: "That pairing code was not accepted." }), { status: 401 });
      }
      return await this.createOwnerSessionResponse("Pairing accepted. Continuing to Claude/ChatGPT.", {
        redirectTo: authorizationUrlFromForm(url, form),
      });
    }
    if (request.method !== "GET") {
      return oauthError("invalid_request", "Unsupported authorization method.", 405);
    }
    const session = await this.ownerSession(request);
    if (!session) {
      return html(authorizePage(url));
    }
    const validation = await this.validateAuthorizeRequest(url);
    if (validation.error) {
      return oauthRedirectError(url, validation.error, validation.description);
    }
    const code = crypto.randomUUID();
    await this.state.storage.put(`code:${code}`, {
      client_id: validation.client_id,
      redirect_uri: validation.redirect_uri,
      code_challenge: validation.code_challenge,
      code_challenge_method: validation.code_challenge_method,
      scope: validation.scope,
      resource: validation.resource,
      expires_at: Date.now() + 5 * 60 * 1000,
    });
    const redirect = new URL(validation.redirect_uri);
    redirect.searchParams.set("code", code);
    if (validation.state) redirect.searchParams.set("state", validation.state);
    return Response.redirect(redirect.toString(), 302);
  }

  async token(request) {
    const form = await request.formData();
    const grantType = String(form.get("grant_type") || "");
    if (grantType === "refresh_token") {
      const refreshToken = String(form.get("refresh_token") || "");
      const payload = await verifySignedToken(refreshToken, this.env, { expectedType: "refresh" });
      if (!payload || !scopeIncludes(payload.scope, AUTH_SCOPE)) {
        return oauthError("invalid_grant", "Refresh token is invalid or expired.", 400);
      }
      return tokenResponse(this.env, {
        client_id: payload.client_id || "refresh",
        resource: payload.aud,
        scope: AUTH_SCOPE,
      });
    }
    if (grantType !== "authorization_code") {
      return oauthError("unsupported_grant_type", "Use authorization_code with PKCE.", 400);
    }
    const code = String(form.get("code") || "");
    const verifier = String(form.get("code_verifier") || "");
    const redirectUri = String(form.get("redirect_uri") || "");
    const clientId = String(form.get("client_id") || "");
    const record = await this.state.storage.get(`code:${code}`);
    if (!record || record.expires_at < Date.now()) {
      return oauthError("invalid_grant", "Authorization code is invalid or expired.", 400);
    }
    await this.state.storage.delete(`code:${code}`);
    if (record.client_id !== clientId || record.redirect_uri !== redirectUri) {
      return oauthError("invalid_grant", "Authorization code does not match this client.", 400);
    }
    const ok = await verifyPkce(verifier, record.code_challenge, record.code_challenge_method);
    if (!ok) {
      return oauthError("invalid_grant", "PKCE verification failed.", 400);
    }
    return tokenResponse(this.env, {
      client_id: clientId,
      resource: record.resource,
      scope: record.scope,
    });
  }

  async validateAuthorizeRequest(url) {
    const responseType = url.searchParams.get("response_type");
    const clientId = url.searchParams.get("client_id") || "";
    const redirectUri = url.searchParams.get("redirect_uri") || "";
    const codeChallenge = url.searchParams.get("code_challenge") || "";
    const codeChallengeMethod = url.searchParams.get("code_challenge_method") || "";
    const requestedScope = url.searchParams.get("scope") || AUTH_SCOPE;
    if (responseType !== "code") {
      return { error: "unsupported_response_type", description: "Yutome supports authorization code flow." };
    }
    if (!clientId || !redirectUri || !codeChallenge || codeChallengeMethod !== "S256") {
      return { error: "invalid_request", description: "client_id, redirect_uri, and PKCE S256 are required." };
    }
    if (!scopeIncludes(requestedScope, AUTH_SCOPE)) {
      return { error: "invalid_scope", description: `Scope ${AUTH_SCOPE} is required.` };
    }
    const registered = await this.state.storage.get(`client:${clientId}`);
    if (registered && Array.isArray(registered.redirect_uris) && registered.redirect_uris.length > 0) {
      if (!registered.redirect_uris.includes(redirectUri)) {
        return { error: "invalid_request", description: "redirect_uri is not registered for this client." };
      }
    }
    return {
      client_id: clientId,
      redirect_uri: redirectUri,
      code_challenge: codeChallenge,
      code_challenge_method: codeChallengeMethod,
      scope: AUTH_SCOPE,
      resource: normalizeResource(url.searchParams.get("resource"), url),
      state: url.searchParams.get("state") || "",
    };
  }

  async ownerSession(request) {
    const cookie = parseCookie(request.headers.get("cookie") || "");
    const sessionId = cookie.yutome_owner_session;
    if (!sessionId) return null;
    const session = await this.state.storage.get(`owner_session:${sessionId}`);
    if (!session || session.expires_at < Date.now()) return null;
    return session;
  }

  async createOwnerSessionResponse(message, options = {}) {
    const sessionId = crypto.randomUUID();
    const expiresAt = Date.now() + 365 * 24 * 60 * 60 * 1000;
    const response = options.redirectTo
      ? new Response(null, { status: 302, headers: { location: options.redirectTo } })
      : html(successPage(message));
    await this.state.storage.put(`owner_session:${sessionId}`, {
      created_at: new Date().toISOString(),
      expires_at: expiresAt,
    });
    response.headers.append(
      "set-cookie",
      `yutome_owner_session=${sessionId}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${365 * 24 * 60 * 60}`,
    );
    return response;
  }

  verifyPairingCode(code) {
    const expected = String(this.env.YUTOME_PAIRING_CODE || "").trim().toUpperCase();
    return Boolean(expected && code && code === expected);
  }

  verifyRelay(request) {
    const expected = this.env.YUTOME_RELAY_TOKEN;
    if (!expected) {
      return json({ error: "YUTOME_RELAY_TOKEN is not configured" }, { status: 500 });
    }
    const header = request.headers.get("authorization") || "";
    const token = header.startsWith("Bearer ") ? header.slice(7).trim() : "";
    if (token !== expected) {
      return json({ error: "unauthorized" }, { status: 401 });
    }
    return null;
  }
}

async function handleMcp(request, env) {
  if (request.method === "GET") {
    return new Response(null, {
      status: 405,
      headers: {
        "allow": "POST",
        "cache-control": "no-store",
      },
    });
  }
  if (request.method !== "POST") {
    return json({ error: "method not allowed" }, { status: 405 });
  }

  let message;
  try {
    message = await request.json();
  } catch {
    return json(rpcError(null, -32700, "Parse error"));
  }

  if (Array.isArray(message)) {
    const responses = (await Promise.all(message.map((item) => handleRpcMessage(item, env)))).filter(Boolean);
    return json(responses);
  }
  const response = await handleRpcMessage(message, env);
  return response ? json(response) : new Response(null, { status: 202 });
}

function protectedResourceMetadata(request) {
  const origin = new URL(request.url).origin;
  return json({
    resource: `${origin}/mcp`,
    resource_name: "Yutome Remote MCP",
    authorization_servers: [origin],
    bearer_methods_supported: ["header"],
    scopes_supported: [AUTH_SCOPE],
  });
}

function authorizationServerMetadata(request) {
  const origin = new URL(request.url).origin;
  return json({
    issuer: origin,
    authorization_endpoint: `${origin}/authorize`,
    token_endpoint: `${origin}/token`,
    registration_endpoint: `${origin}/register`,
    response_types_supported: ["code"],
    grant_types_supported: ["authorization_code", "refresh_token"],
    code_challenge_methods_supported: ["S256"],
    token_endpoint_auth_methods_supported: ["none"],
    scopes_supported: [AUTH_SCOPE],
  });
}

async function verifyMcpAuthorization(request, env) {
  const header = request.headers.get("authorization") || "";
  const token = header.startsWith("Bearer ") ? header.slice(7).trim() : "";
  if (!token) return mcpUnauthorized(request, "missing_token");
  const payload = await verifySignedToken(token, env, { expectedType: "access" });
  if (!payload) return mcpUnauthorized(request, "invalid_token");
  const origin = new URL(request.url).origin;
  const validAudience = payload.aud === `${origin}/mcp` || payload.aud === origin;
  if (!validAudience || !scopeIncludes(payload.scope, AUTH_SCOPE)) {
    return mcpUnauthorized(request, "insufficient_scope");
  }
  return null;
}

function mcpUnauthorized(request, error = "invalid_token") {
  const origin = new URL(request.url).origin;
  return json(
    {
      error,
      error_description: "Connect and approve Yutome before calling this remote MCP endpoint.",
    },
    {
      status: 401,
      headers: {
        "www-authenticate": `Bearer resource_metadata="${origin}/.well-known/oauth-protected-resource", scope="${AUTH_SCOPE}"`,
      },
    },
  );
}

async function handleRpcMessage(message, env) {
  const id = Object.prototype.hasOwnProperty.call(message, "id") ? message.id : null;
  if (!message || message.jsonrpc !== "2.0" || typeof message.method !== "string") {
    return rpcError(id, -32600, "Invalid Request");
  }

  if (message.method.startsWith("notifications/")) {
    return null;
  }

  switch (message.method) {
    case "initialize":
      return rpcResult(id, {
        protocolVersion: "2025-06-18",
        capabilities: {
          tools: {},
        },
        serverInfo: SERVER_INFO,
        instructions: SERVER_INSTRUCTIONS,
      });
    case "tools/list":
      return rpcResult(id, { tools: toolSchemas(env) });
    case "tools/call":
      return rpcResult(id, await callThroughRelay(env, message.params || {}));
    case "ping":
      return rpcResult(id, {});
    default:
      return rpcError(id, -32601, `Method not found: ${message.method}`);
  }
}

async function callThroughRelay(env, params) {
  try {
    const response = await relayFetch(env, "/enqueue", new Request("https://relay/enqueue", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(params || {}),
    }));
    if (!response.ok) {
      return offlineToolResult(params, { status: "relay_error", message: `Relay error: HTTP ${response.status}` });
    }
    return await response.json();
  } catch (error) {
    return offlineToolResult(params, { status: "relay_error", message: `Relay error: ${error.message}` });
  }
}

function relayFetch(env, path, request) {
  if (!env.RELAY) {
    return json(offlinePayload({ status: "relay_not_configured" }), { status: 503 });
  }
  const id = env.RELAY.idFromName("default");
  const relay = env.RELAY.get(id);
  const forwarded = new Request(`https://relay${path}`, request);
  return relay.fetch(forwarded);
}

function offlineToolResult(params, overrides = {}) {
  const tool = params.name || "unknown";
  const payload = offlinePayload(overrides);
  return {
    isError: true,
    content: [
      {
        type: "text",
        text:
          `Yutome is not connected to this remote MCP endpoint, so '${tool}' cannot read the local corpus yet. ` +
          "Open Yutome on this computer and connect it to this endpoint, then try again.",
      },
    ],
    structuredContent: payload,
  };
}

function offlinePayload(overrides = {}) {
  return {
    ok: false,
    status: overrides.status || "desktop_offline",
    desktop_connection: "offline",
    offline_search: "disabled",
    replica_enabled: false,
    last_desktop_seen_at: overrides.last_desktop_seen_at || null,
    message: overrides.message || "Yutome Remote MCP is deployed, but Yutome Desktop is not connected yet.",
  };
}

async function tokenResponse(env, { client_id, resource, scope }) {
  const now = Math.floor(Date.now() / 1000);
  const accessPayload = {
    typ: "access",
    iss: issuerFromResource(resource),
    aud: resource,
    sub: "yutome-owner",
    client_id,
    scope,
    iat: now,
    exp: now + ACCESS_TOKEN_TTL_SECONDS,
    jti: crypto.randomUUID(),
  };
  const refreshPayload = {
    typ: "refresh",
    iss: issuerFromResource(resource),
    aud: resource,
    sub: "yutome-owner",
    client_id,
    scope,
    iat: now,
    exp: now + REFRESH_TOKEN_TTL_SECONDS,
    jti: crypto.randomUUID(),
  };
  return json({
    access_token: await signToken(accessPayload, env),
    refresh_token: await signToken(refreshPayload, env),
    token_type: "Bearer",
    expires_in: ACCESS_TOKEN_TTL_SECONDS,
    scope,
  });
}

async function signToken(payload, env) {
  const header = { alg: "HS256", typ: "JWT" };
  const body = `${base64urlJson(header)}.${base64urlJson(payload)}`;
  const signature = await hmacSha256(body, env);
  return `${body}.${base64urlBytes(signature)}`;
}

async function verifySignedToken(token, env, { expectedType }) {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  const body = `${parts[0]}.${parts[1]}`;
  const expected = base64urlBytes(await hmacSha256(body, env));
  if (parts[2] !== expected) return null;
  let payload;
  try {
    payload = JSON.parse(textFromBase64url(parts[1]));
  } catch {
    return null;
  }
  const now = Math.floor(Date.now() / 1000);
  if (payload.typ !== expectedType || !payload.exp || payload.exp <= now) return null;
  return payload;
}

async function hmacSha256(value, env) {
  const secret = env.YUTOME_TOKEN_SECRET;
  if (!secret) throw new Error("YUTOME_TOKEN_SECRET is not configured");
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return crypto.subtle.sign("HMAC", key, new TextEncoder().encode(value));
}

async function verifyPkce(verifier, challenge, method) {
  if (!verifier || !challenge || method !== "S256") return false;
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64urlBytes(digest) === challenge;
}

function base64urlJson(value) {
  return base64urlString(JSON.stringify(value));
}

function base64urlString(value) {
  return btoa(unescape(encodeURIComponent(value))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function base64urlBytes(value) {
  let binary = "";
  for (const byte of new Uint8Array(value)) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function textFromBase64url(value) {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  return decodeURIComponent(escape(atob(padded)));
}

function scopeIncludes(scope, required) {
  return String(scope || "").split(/\s+/).includes(required);
}

function normalizeResource(resource, url) {
  const origin = url.origin;
  if (!resource) return `${origin}/mcp`;
  try {
    const parsed = new URL(resource, origin);
    if (parsed.origin !== origin) return `${origin}/mcp`;
    return parsed.pathname.endsWith("/mcp") ? `${origin}/mcp` : origin;
  } catch {
    return `${origin}/mcp`;
  }
}

function issuerFromResource(resource) {
  try {
    return new URL(resource).origin;
  } catch {
    return resource;
  }
}

function authorizationUrlFromForm(url, form) {
  const next = new URL(url.toString());
  for (const key of [
    "response_type",
    "client_id",
    "redirect_uri",
    "scope",
    "state",
    "code_challenge",
    "code_challenge_method",
    "resource",
  ]) {
    const value = form.get(key);
    if (value !== null) next.searchParams.set(key, String(value));
  }
  return next.toString();
}

function parseCookie(value) {
  const result = {};
  for (const part of value.split(";")) {
    const [rawName, ...rest] = part.trim().split("=");
    if (!rawName) continue;
    result[rawName] = decodeURIComponent(rest.join("=") || "");
  }
  return result;
}

function oauthError(error, description, status = 400) {
  return json({ error, error_description: description }, { status });
}

function oauthRedirectError(url, error, description) {
  const redirectUri = url.searchParams.get("redirect_uri");
  if (!redirectUri) return oauthError(error, description, 400);
  const redirect = new URL(redirectUri);
  redirect.searchParams.set("error", error);
  redirect.searchParams.set("error_description", description);
  const state = url.searchParams.get("state");
  if (state) redirect.searchParams.set("state", state);
  return Response.redirect(redirect.toString(), 302);
}

function devNoAuth(env) {
  return String(env.YUTOME_DEV_NO_AUTH || "").toLowerCase() === "true";
}

function pairingPage({ error = "" } = {}) {
  return page("Pair Yutome Remote MCP", `
    <p>Enter the pairing code printed by <code>yutome connect</code>.</p>
    ${error ? `<p class="error">${escapeHtml(error)}</p>` : ""}
    <form method="get" action="/pair">
      <label>Pairing code <input name="code" autocomplete="one-time-code" autofocus /></label>
      <button type="submit">Pair Yutome</button>
    </form>
  `);
}

function authorizePage(url, { error = "" } = {}) {
  const hidden = Array.from(url.searchParams.entries())
    .map(([key, value]) => `<input type="hidden" name="${escapeHtml(key)}" value="${escapeHtml(value)}" />`)
    .join("\n");
  return page("Approve Yutome Remote MCP", `
    <p>Claude/ChatGPT wants permission to search this Yutome library while your computer is online.</p>
    <p>No Yutome account is needed. Enter the pairing code printed by <code>yutome connect</code>.</p>
    ${error ? `<p class="error">${escapeHtml(error)}</p>` : ""}
    <form method="post" action="/authorize">
      ${hidden}
      <label>Pairing code <input name="pairing_code" autocomplete="one-time-code" autofocus /></label>
      <button type="submit">Approve</button>
    </form>
  `);
}

function successPage(message) {
  return page("Yutome Remote MCP", `<p>${escapeHtml(message)}</p>`);
}

function page(title, body) {
  return `<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>${escapeHtml(title)}</title>
  <style>
    body { font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; line-height: 1.45; margin: 2rem; max-width: 42rem; }
    code, input { font: inherit; }
    label { display: grid; gap: 0.4rem; margin: 1rem 0; }
    input { padding: 0.65rem; border: 1px solid #999; border-radius: 6px; }
    button { padding: 0.7rem 0.9rem; border: 0; border-radius: 6px; background: #111; color: white; font-weight: 600; }
    .error { color: #9f1239; }
  </style>
</head>
<body>
  <h1>${escapeHtml(title)}</h1>
  ${body}
</body>
</html>`;
}

function html(markup, init = {}) {
  return new Response(markup, {
    ...init,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      ...(init.headers || {}),
    },
  });
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  })[char]);
}

function rpcResult(id, result) {
  return { jsonrpc: "2.0", id, result };
}

function rpcError(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

function json(payload, init = {}) {
  return new Response(JSON.stringify(payload, null, 2) + "\n", {
    ...init,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
      ...(init.headers || {}),
    },
  });
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function clampInt(raw, fallback, min, max) {
  const parsed = Number.parseInt(raw || "", 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(min, Math.min(max, parsed));
}
'''
