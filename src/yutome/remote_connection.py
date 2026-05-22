from __future__ import annotations

import json
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


def remote_state_path(paths: ProjectPaths) -> Path:
    return paths.data_dir / REMOTE_STATE_DIRNAME / REMOTE_STATE_FILENAME


def load_remote_state(paths: ProjectPaths) -> RemoteConnectionState | None:
    path = remote_state_path(paths)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"remote connector state at {path} is not valid JSON ({exc.msg}). "
            "Run `yutome connect --deploy` to recreate it."
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"remote connector state at {path} is not a JSON object. "
            "Run `yutome connect --deploy` to recreate it."
        )
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


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _recent_iso_timestamp(value: str, *, seconds: int) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(UTC) - parsed).total_seconds() <= seconds
