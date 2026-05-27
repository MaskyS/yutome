from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from yutome.hosted.control_plane import Source, SourceRefreshPolicy
from yutome.hosted.ids import input_hash
from yutome.hosted.repositories import SqlStatement
from yutome.hosted.source_registry import hosted_source_id, provider_credentials_in_source


class HostedSourceImportError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int = 400,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.data = dict(data or {})
        super().__init__(message)


class HostedSourceImportDescriptor(BaseModel):
    model_config = ConfigDict(extra="allow")

    source_url: str | None = None
    url: str | None = None
    value: str | None = None
    source_type: str | None = None
    display_name: str | None = None
    title: str | None = None
    channel_id: str | None = None
    playlist_id: str | None = None
    video_id: str | None = None
    import_source: str | None = None
    selected: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class HostedSourcesImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[HostedSourceImportDescriptor]
    cadence_seconds: int = Field(default=900, ge=1)
    max_new_videos: int = Field(default=25, ge=1)
    refresh_enabled: bool = True


class HostedSourceImportActor(BaseModel):
    """Yutome account authorization for a source-import request.

    This actor is distinct from a YouTube grant. It identifies who asked Yutome
    to enqueue public source indexing work.
    """

    workspace_id: str
    seeded_by: str
    user_id: str | None = None
    cli_grant_id: str | None = None
    mcp_grant_id: str | None = None
    mcp_client_id: str | None = None
    mcp_session_id: str | None = None


def import_sources(
    connection: Any,
    *,
    request: HostedSourcesImportRequest,
    actor: HostedSourceImportActor,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not request.sources:
        raise HostedSourceImportError(code="source_import_empty", message="At least one source is required.")
    if len(request.sources) > 250:
        raise HostedSourceImportError(
            code="source_import_too_large",
            message="Import at most 250 sources per request.",
        )

    clock = now or datetime.now(timezone.utc)
    imported: list[dict[str, Any]] = []
    jobs: list[dict[str, Any]] = []
    policies: list[dict[str, Any]] = []
    transaction = getattr(connection, "transaction", None)
    manager = transaction() if callable(transaction) else None
    if manager is None:
        _execute_source_import(
            connection,
            request=request,
            actor=actor,
            now=clock,
            imported=imported,
            jobs=jobs,
            policies=policies,
        )
    else:
        with manager:
            _execute_source_import(
                connection,
                request=request,
                actor=actor,
                now=clock,
                imported=imported,
                jobs=jobs,
                policies=policies,
            )
    return {
        "ok": True,
        "workspace_id": actor.workspace_id,
        "imported": imported,
        "jobs": jobs,
        "refresh_policies": policies,
    }


def list_source_jobs(connection: Any, *, workspace_id: str, limit: int, source_id: str | None = None) -> dict[str, Any]:
    statement = account_jobs_sql(workspace_id=workspace_id, limit=max(1, min(limit, 100)), source_id=source_id)
    rows = _rows_from_result(connection.execute(statement.sql, statement.params))
    return {"ok": True, "workspace_id": workspace_id, "jobs": [job_row_json(row) for row in rows]}


def _execute_source_import(
    connection: Any,
    *,
    request: HostedSourcesImportRequest,
    actor: HostedSourceImportActor,
    now: datetime,
    imported: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    policies: list[dict[str, Any]],
) -> None:
    for descriptor in request.sources:
        payload = descriptor.model_dump(mode="json")
        if _contains_credential_shape(payload):
            raise HostedSourceImportError(
                code="source_import_credentials_rejected",
                message="Hosted source import accepts public source descriptors only, not provider credentials.",
            )
        source = _source_from_import_descriptor(workspace_id=actor.workspace_id, descriptor=descriptor)
        if provider_credentials_in_source(source):
            raise HostedSourceImportError(
                code="source_import_credentials_rejected",
                message="Hosted source import accepts public source descriptors only, not provider credentials.",
            )
        from yutome.hosted.runtime import upsert_hosted_source_sql

        source_statement = upsert_hosted_source_sql(source)
        source_rows = _rows_from_result(connection.execute(source_statement.sql, source_statement.params))
        persisted_source = source_rows[0] if source_rows else {}
        imported.append(
            {
                "source_id": str(persisted_source.get("id") or source.id),
                "source_type": str(persisted_source.get("source_type") or source.source_type),
                "source_url": str(persisted_source.get("source_url") or source.source_url),
                "canonical_video_id": source.canonical_video_id,
                "canonical_channel_id": source.canonical_channel_id,
                "canonical_playlist_id": source.canonical_playlist_id,
            }
        )
        if source.canonical_video_id:
            metadata = _actor_metadata(actor)
            from yutome.hosted.indexing import enqueue_index_video_job_sql

            job_statement = enqueue_index_video_job_sql(
                workspace_id=actor.workspace_id,
                source_id=source.id,
                video_id=source.canonical_video_id,
                priority=100,
                now=now,
                metadata=metadata,
            )
            job_rows = _rows_from_result(connection.execute(job_statement.sql, job_statement.params))
            job_row = job_rows[0] if job_rows else {}
            jobs.append(
                {
                    "job_id": str(job_row.get("id") or job_statement.params["id"]),
                    "job_type": str(job_row.get("job_type") or "index_video"),
                    "status": str(job_row.get("status") or "queued"),
                    "source_id": source.id,
                    "youtube_video_id": source.canonical_video_id,
                }
            )
            continue
        policy_id = f"srp_{input_hash({'workspace_id': actor.workspace_id, 'source_id': source.id}, prefix='').lstrip('_')[:24]}"
        policy = SourceRefreshPolicy(
            id=policy_id,
            workspace_id=actor.workspace_id,
            source_id=source.id,
            enabled=request.refresh_enabled,
            cadence_seconds=request.cadence_seconds,
            next_run_at=now,
            max_new_videos_per_run=request.max_new_videos,
        )
        from yutome.hosted.runtime import upsert_source_refresh_policy_sql

        policy_statement = upsert_source_refresh_policy_sql(policy)
        policy_rows = _rows_from_result(connection.execute(policy_statement.sql, policy_statement.params))
        policy_row = policy_rows[0] if policy_rows else {}
        policies.append(
            {
                "refresh_policy_id": str(policy_row.get("id") or policy.id),
                "source_id": source.id,
                "enabled": bool(policy_row.get("enabled") if "enabled" in policy_row else policy.enabled),
                "cadence_seconds": int(policy_row.get("cadence_seconds") or policy.cadence_seconds),
            }
        )
        job_metadata = {
            **_actor_metadata(actor),
            "source_type": source.source_type,
        }
        from yutome.hosted.indexing import enqueue_discover_source_job_sql

        discover_statement = enqueue_discover_source_job_sql(
            workspace_id=actor.workspace_id,
            source_id=source.id,
            priority=100,
            now=now,
            policy_id=policy.id,
            max_new_videos_per_run=request.max_new_videos,
            trigger="source_import",
            metadata=job_metadata,
        )
        discover_rows = _rows_from_result(connection.execute(discover_statement.sql, discover_statement.params))
        discover_row = discover_rows[0] if discover_rows else {}
        jobs.append(
            {
                "job_id": str(discover_row.get("id") or discover_statement.params["id"]),
                "job_type": str(discover_row.get("job_type") or "discover_source"),
                "status": str(discover_row.get("status") or "queued"),
                "source_id": source.id,
                "youtube_video_id": None,
            }
        )


def _actor_metadata(actor: HostedSourceImportActor) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "seeded_by": actor.seeded_by,
            "user_id": actor.user_id,
            "cli_grant_id": actor.cli_grant_id,
            "mcp_grant_id": actor.mcp_grant_id,
            "mcp_client_id": actor.mcp_client_id,
            "mcp_session_id": actor.mcp_session_id,
        }.items()
        if value
    }


def _source_from_import_descriptor(*, workspace_id: str, descriptor: HostedSourceImportDescriptor) -> Source:
    value = _descriptor_value(descriptor)
    import_source = _public_import_source(descriptor.import_source)
    display_name = _optional_text(descriptor.display_name) or _optional_text(descriptor.title)
    metadata = dict(descriptor.metadata)
    if descriptor.import_source:
        metadata.setdefault("local_import_source", descriptor.import_source)
    if descriptor.channel_id:
        source_url = (
            descriptor.source_url or descriptor.url or f"https://www.youtube.com/channel/{descriptor.channel_id}"
        )
        return Source(
            id=hosted_source_id(workspace_id=workspace_id, source_url=source_url),
            workspace_id=workspace_id,
            source_type="channel",
            source_url=source_url,
            canonical_channel_id=descriptor.channel_id,
            display_name=display_name or descriptor.channel_id,
            selected=descriptor.selected,
            auto_index_allowed=descriptor.selected,
            import_source=import_source,
            metadata_jsonb=metadata,
        )
    if descriptor.playlist_id and not descriptor.video_id:
        source_url = (
            descriptor.source_url or descriptor.url or f"https://www.youtube.com/playlist?list={descriptor.playlist_id}"
        )
        return Source(
            id=hosted_source_id(workspace_id=workspace_id, source_url=source_url),
            workspace_id=workspace_id,
            source_type="playlist",
            source_url=source_url,
            canonical_playlist_id=descriptor.playlist_id,
            display_name=display_name or descriptor.playlist_id,
            selected=descriptor.selected,
            auto_index_allowed=descriptor.selected,
            import_source=import_source,
            metadata_jsonb=metadata,
        )
    try:
        from yutome.hosted.indexing import source_from_public_youtube_input

        source = source_from_public_youtube_input(
            workspace_id=workspace_id,
            source_id=hosted_source_id(workspace_id=workspace_id, source_url=value),
            value=value,
            import_source=import_source,
            display_name=display_name,
        )
    except ValueError as exc:
        raise HostedSourceImportError(
            code="source_import_invalid",
            message=str(exc),
            data={"source": value},
        ) from exc
    return source.model_copy(
        update={
            "selected": descriptor.selected,
            "auto_index_allowed": descriptor.selected,
            "metadata_jsonb": metadata,
        }
    )


def _descriptor_value(descriptor: HostedSourceImportDescriptor) -> str:
    for value in (
        descriptor.source_url,
        descriptor.url,
        descriptor.value,
        descriptor.video_id,
        descriptor.playlist_id,
        descriptor.channel_id,
    ):
        if value and value.strip():
            return value.strip()
    raise HostedSourceImportError(
        code="source_import_value_required", message="Source descriptor is missing a URL or id."
    )


def _public_import_source(raw: str | None) -> str:
    value = (raw or "cli").strip().replace("-", "_")
    if value in {"public_api", "public_scrape", "yt_dlp", "manual_url", "manual", "cli"}:
        return value
    # Local OAuth/cookie subscription imports upload public channel rows for v1;
    # hosted YouTube grants are intentionally not created here.
    return "cli"


def _contains_credential_shape(value: Any) -> bool:
    credential_fragments = (
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "client_secret",
        "secret",
        "password",
        "credential",
    )
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered != "credential_mode" and any(fragment in lowered for fragment in credential_fragments):
                return True
            if _contains_credential_shape(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_credential_shape(item) for item in value)
    if isinstance(value, str):
        lowered = value.lower()
        return any(f"{fragment}=" in lowered or f"{fragment}%3d" in lowered for fragment in credential_fragments)
    return False


def account_jobs_sql(*, workspace_id: str, limit: int, source_id: str | None = None) -> SqlStatement:
    # Enrich each job with human context the dashboard Activity feed needs.
    return SqlStatement(
        sql="""
SELECT j.id, j.workspace_id, j.source_id, j.job_type, j.status, j.priority, j.created_at,
       j.started_at, j.finished_at, j.cancelled_at, j.error_code, j.error_message, j.metadata_json,
       s.display_name AS source_display_name,
       s.source_type AS source_type,
       s.source_url AS source_url,
       v.title AS video_title
FROM jobs AS j
LEFT JOIN sources AS s ON s.id = j.source_id
LEFT JOIN videos AS v
       ON v.workspace_id = j.workspace_id
      AND v.youtube_video_id = j.metadata_json->>'youtube_video_id'
WHERE j.workspace_id = %(workspace_id)s
  AND (%(source_id)s::text IS NULL OR j.source_id = %(source_id)s)
ORDER BY j.created_at DESC
LIMIT %(limit)s;
""".strip(),
        params={"workspace_id": workspace_id, "source_id": source_id, "limit": limit},
    )


def job_row_json(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "job_id": row.get("id"),
        "workspace_id": row.get("workspace_id"),
        "source_id": row.get("source_id"),
        "job_type": row.get("job_type"),
        "status": row.get("status"),
        "priority": row.get("priority"),
        "created_at": _datetime_json(row.get("created_at")),
        "started_at": _datetime_json(row.get("started_at")),
        "finished_at": _datetime_json(row.get("finished_at")),
        "cancelled_at": _datetime_json(row.get("cancelled_at")),
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
        "metadata": _json_object(row.get("metadata_json")),
        "source_display_name": row.get("source_display_name"),
        "source_type": row.get("source_type"),
        "source_url": row.get("source_url"),
        "video_title": row.get("video_title"),
    }


def _rows_from_result(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, list):
        return [dict(row) for row in result]
    if hasattr(result, "mappings"):
        return [dict(row) for row in result.mappings().all()]
    if hasattr(result, "fetchall"):
        return [dict(row) for row in result.fetchall()]
    try:
        return [dict(row) for row in result]
    except TypeError:
        return []


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _row_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _datetime_json(value: Any) -> str | None:
    parsed = _row_datetime(value)
    return parsed.isoformat() if parsed is not None else None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


__all__ = [
    "HostedSourceImportActor",
    "HostedSourceImportDescriptor",
    "HostedSourceImportError",
    "HostedSourcesImportRequest",
    "account_jobs_sql",
    "import_sources",
    "job_row_json",
    "list_source_jobs",
]
