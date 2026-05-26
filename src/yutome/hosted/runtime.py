from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from yutome.config import AppConfig
from yutome.hosted.control_plane import Job, Source, TERMINAL_JOB_STATUSES
from yutome.hosted.ids import input_hash
from yutome.hosted.billing import billing_debug_snapshot_from_rows, billing_debug_snapshot_sql
from yutome.hosted.indexing import (
    HostedIndexingExecutor,
    HostedVideoInput,
    TranscriptChunkInput,
    mock_embedding_vector,
    plan_mock_hosted_public_indexing,
    source_from_public_youtube_input,
)
from yutome.hosted.gate import UsageGate
from yutome.hosted.jobs import claim_jobs_sql
from yutome.hosted.ledger import PostgresUsageGate, PostgresUsageLedger
from yutome.hosted.postgres import apply_hosted_schema, apply_phase1_schema, apply_phase4_schema, apply_schema
from yutome.hosted.repositories import SqlStatement, usage_repository_constraint_statements
from yutome.hosted.search_store import PostgresVectorChordSearchStore


MigrationPhase = Literal["phase1", "phase4", "hosted"]


class HostedRuntimeError(RuntimeError):
    pass


class HostedDbCheck(BaseModel):
    ok: bool
    url_env: str
    url_configured: bool
    database_reachable: bool = False
    extensions: dict[str, bool] = Field(default_factory=dict)
    error: str | None = None


class HostedTickResult(BaseModel):
    tick: str
    attempted: bool
    affected_rows: int | None = None
    sql: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class HostedIndexingSmokeResult(BaseModel):
    ok: bool
    dev_only: bool = True
    migrated: bool
    migration_phase: MigrationPhase | None = None
    applied_migrations: int = 0
    workspace_id: str
    source_id: str
    job_id: str
    youtube_video_id: str
    hosted_video_id: str
    transcript_version_id: str
    query: str
    operations_executed: int
    operation_names: list[str]
    rows: list[dict[str, Any]]
    usage: dict[str, Any]


@dataclass(frozen=True)
class HostedPostgresSettings:
    url_env: str = "YUTOME_POSTGRES_URL"
    fallback_envs: tuple[str, ...] = ("DATABASE_URL",)


@dataclass
class HostedCommandRunner:
    config: AppConfig
    connection: Any | None = None
    settings: HostedPostgresSettings = field(default_factory=HostedPostgresSettings)

    def connect(self) -> Any:
        if self.connection is not None:
            return self.connection
        self.connection = connect_postgres(url_env=self.config.hosted.postgres_url_env)
        return self.connection

    def migrate(self, phase: MigrationPhase = "hosted") -> int:
        connection = self.connect()
        if phase == "phase1":
            applied = apply_phase1_schema(connection)
            return applied + apply_schema(connection, statements=usage_repository_constraint_statements())
        if phase == "phase4":
            applied = apply_phase4_schema(connection)
            return applied + apply_schema(connection, statements=usage_repository_constraint_statements())
        applied = apply_hosted_schema(connection)
        return applied + apply_schema(connection, statements=usage_repository_constraint_statements())

    def usage_gate(self, *, gate: UsageGate | None = None) -> PostgresUsageGate:
        return PostgresUsageGate(self.connect(), gate=gate)

    def usage_ledger(self) -> PostgresUsageLedger:
        return PostgresUsageLedger(self.connect())

    def db_check(self) -> HostedDbCheck:
        url_env = self.config.hosted.postgres_url_env
        url = postgres_url_from_env(url_env=url_env)
        if url is None:
            return HostedDbCheck(ok=False, url_env=url_env, url_configured=False, error="postgres_url_missing")
        try:
            store = PostgresVectorChordSearchStore(self.connect())
            extensions = store.extension_check()
        except Exception:  # pragma: no cover - live connection path
            return HostedDbCheck(
                ok=False,
                url_env=url_env,
                url_configured=True,
                database_reachable=False,
                error="database_unreachable",
            )
        return HostedDbCheck(
            ok=all(extensions.values()),
            url_env=url_env,
            url_configured=True,
            database_reachable=True,
            extensions=extensions,
        )

    def search_smoke(self, *, workspace_id: str, query: str, limit: int = 3) -> dict[str, Any]:
        store = PostgresVectorChordSearchStore(self.connect())
        rows, usage = store.lexical_search(workspace_id=workspace_id, query=query, limit=limit)
        return {"rows": rows, "usage": usage.model_dump(mode="json")}

    def mock_indexing_smoke(
        self,
        *,
        workspace_id: str,
        migrate: bool = False,
        migration_phase: MigrationPhase = "hosted",
        query: str | None = None,
        limit: int = 3,
        source_url: str = "https://www.youtube.com/watch?v=OEDoJyhQhXs",
    ) -> HostedIndexingSmokeResult:
        """Development-only smoke that writes deterministic fake transcript rows."""
        _validate_positive("limit", limit)
        connection = self.connect()
        applied_migrations = self.migrate(phase=migration_phase) if migrate else 0
        plan = mock_hosted_public_indexing_plan(workspace_id=workspace_id, source_url=source_url, query=query)
        for statement in mock_hosted_public_indexing_bootstrap_statements(plan.source, plan.job):
            connection.execute(statement.sql, statement.params)
        for operation in plan.sql_operations:
            connection.execute(operation.statement.sql, operation.statement.params)

        search_query = query or str(plan.search_operations[0].statement.params["query"])
        store = PostgresVectorChordSearchStore(connection, index_profile_ref=plan.index_profile.id)
        rows, usage = store.hybrid_search(
            workspace_id=workspace_id,
            query=search_query,
            query_vector=mock_embedding_vector(search_query, dimension=plan.index_profile.embedding_dimension),
            limit=limit,
        )
        return HostedIndexingSmokeResult(
            ok=True,
            migrated=migrate,
            migration_phase=migration_phase if migrate else None,
            applied_migrations=applied_migrations,
            workspace_id=workspace_id,
            source_id=plan.source.id,
            job_id=plan.job.id,
            youtube_video_id=plan.video.youtube_video_id,
            hosted_video_id=plan.hosted_video_id,
            transcript_version_id=plan.transcript_version_id,
            query=search_query,
            operations_executed=len(plan.sql_operations),
            operation_names=[operation.name for operation in plan.sql_operations],
            rows=rows,
            usage=usage.model_dump(mode="json"),
        )

    def worker_once(
        self,
        *,
        lease_owner: str,
        limit: int = 1,
        lease_seconds: int = 900,
        workspace_id: str | None = None,
    ) -> HostedTickResult:
        statement = claim_jobs_sql(
            lease_owner=lease_owner,
            now=datetime.now(timezone.utc),
            lease_seconds=lease_seconds,
            limit=limit,
            workspace_id=workspace_id,
            job_types=["index_video"],
            executor_kind="railway",
            executor_ref=lease_owner,
        )
        result = self.connect().execute(statement.sql, statement.params)
        rows = _rows_from_result(result)
        executor = HostedIndexingExecutor(connection=self.connect(), config=self.config, gate=self.usage_gate(), ledger=self.usage_ledger())
        executions = []
        for row in rows:
            job = _job_from_row(row)
            if job.job_type != "index_video":
                continue
            executions.append(_execution_result_dict(executor.execute(job, lease_owner=lease_owner)))
        return HostedTickResult(
            tick="worker_once",
            attempted=True,
            affected_rows=len(rows),
            sql=statement.sql,
            params={**statement.params, "executions": executions},
        )

    def source_refresh_tick(
        self,
        *,
        lease_owner: str,
        limit: int = 25,
        lock_seconds: int = 900,
    ) -> HostedTickResult:
        statement = source_refresh_tick_sql(
            lease_owner=lease_owner,
            now=datetime.now(timezone.utc),
            limit=limit,
            lock_seconds=lock_seconds,
        )
        result = self.connect().execute(statement.sql, statement.params)
        rows = _rows_from_result(result)
        return HostedTickResult(
            tick="source_refresh_tick",
            attempted=True,
            affected_rows=len(rows),
            sql=statement.sql,
            params=statement.params,
        )

    def maintenance_tick(self, *, limit: int = 100) -> HostedTickResult:
        statement = maintenance_tick_sql(now=datetime.now(timezone.utc), limit=limit)
        result = self.connect().execute(statement.sql, statement.params)
        rows = _rows_from_result(result)
        return HostedTickResult(
            tick="maintenance_tick",
            attempted=True,
            affected_rows=len(rows),
            sql=statement.sql,
            params=statement.params,
        )

    def billing_status(
        self,
        *,
        workspace_id: str,
        limit: int = 20,
        operation: str | None = None,
    ) -> dict[str, Any]:
        statement = billing_debug_snapshot_sql(workspace_id=workspace_id, limit=limit, operation=operation)
        result = self.connect().execute(statement.sql, statement.params)
        rows = _rows_from_result(result)
        snapshot = billing_debug_snapshot_from_rows(
            rows,
            workspace_id=workspace_id,
            limit=limit,
            operation=operation,
        )
        return snapshot.model_dump(mode="json")


def build_hosted_api_app(
    runner: HostedCommandRunner,
    *,
    readiness_check: Callable[[], Any] | None = None,
    index_profile_ref: str | None = None,
) -> Any:
    from yutome.hosted.http_api import _api_token_from_env, build_postgres_app

    connection = runner.connect()
    return build_postgres_app(
        connection=connection,
        readiness_check=readiness_check or runner.db_check,
        gate=runner.usage_gate(),
        ledger=runner.usage_ledger(),
        index_profile_ref=index_profile_ref,
        expected_api_token=_api_token_from_env(),
    )


def source_refresh_tick_sql(
    *,
    lease_owner: str,
    now: datetime,
    limit: int = 25,
    lock_seconds: int = 900,
) -> SqlStatement:
    _validate_positive("limit", limit)
    _validate_positive("lock_seconds", lock_seconds)
    return SqlStatement(
        sql="""
WITH due AS (
    SELECT id
    FROM source_refresh_policies
    WHERE enabled = true
      AND next_run_at <= %(now)s
      AND (locked_by IS NULL OR locked_until <= %(now)s)
    ORDER BY next_run_at ASC, id ASC
    LIMIT %(limit)s
    FOR UPDATE SKIP LOCKED
)
UPDATE source_refresh_policies AS policy
SET last_started_at = %(now)s,
    locked_by = %(lease_owner)s,
    locked_until = %(locked_until)s,
    next_run_at = GREATEST(policy.next_run_at, %(now)s) + make_interval(secs => policy.cadence_seconds),
    updated_at = %(now)s
FROM due
WHERE policy.id = due.id
RETURNING policy.*;
""".strip(),
        params={
            "lease_owner": lease_owner,
            "now": now,
            "locked_until": now + timedelta(seconds=lock_seconds),
            "limit": limit,
        },
    )


def maintenance_tick_sql(*, now: datetime, limit: int = 100) -> SqlStatement:
    _validate_positive("limit", limit)
    return SqlStatement(
        sql="""
WITH expired_jobs AS (
    SELECT id
    FROM jobs
    WHERE lease_owner IS NOT NULL
      AND lease_expires_at <= %(now)s
      AND status <> ALL(%(terminal_statuses)s)
    ORDER BY lease_expires_at ASC, id ASC
    LIMIT %(limit)s
    FOR UPDATE SKIP LOCKED
),
released_jobs AS (
    UPDATE jobs AS job
    SET status = 'retry_wait',
        retry_after = COALESCE(job.retry_after, %(now)s),
        lease_owner = NULL,
        leased_at = NULL,
        lease_expires_at = NULL
    FROM expired_jobs
    WHERE job.id = expired_jobs.id
    RETURNING job.id
),
expired_source_locks AS (
    SELECT id
    FROM source_refresh_policies
    WHERE locked_by IS NOT NULL
      AND locked_until <= %(now)s
    ORDER BY locked_until ASC, id ASC
    LIMIT %(limit)s
    FOR UPDATE SKIP LOCKED
),
released_source_locks AS (
    UPDATE source_refresh_policies AS policy
    SET locked_by = NULL,
        locked_until = NULL,
        updated_at = %(now)s
    FROM expired_source_locks
    WHERE policy.id = expired_source_locks.id
    RETURNING policy.id
)
SELECT 'job' AS target, id FROM released_jobs
UNION ALL
SELECT 'source_refresh_policy' AS target, id FROM released_source_locks;
""".strip(),
        params={"now": now, "limit": limit, "terminal_statuses": sorted(TERMINAL_JOB_STATUSES)},
    )


def mock_hosted_public_indexing_plan(
    *,
    workspace_id: str,
    source_url: str = "https://www.youtube.com/watch?v=OEDoJyhQhXs",
    query: str | None = None,
    now: datetime | None = None,
):
    created_at = now or datetime.now(timezone.utc)
    source = source_from_public_youtube_input(
        workspace_id=workspace_id,
        source_id="src_mock_pending",
        value=source_url,
        import_source="cli",
        display_name="Hosted mock indexing smoke",
    )
    source_hash = input_hash({"workspace_id": workspace_id, "source_url": source.source_url}, prefix="").lstrip("_")[:16]
    source = source.model_copy(update={"id": f"src_mock_{source_hash}"})
    video_id = source.canonical_video_id or "OEDoJyhQhXs"
    job_hash = input_hash({"workspace_id": workspace_id, "source_id": source.id, "video_id": video_id}, prefix="").lstrip("_")[:16]
    job = Job(
        id=f"job_mock_{job_hash}",
        workspace_id=workspace_id,
        source_id=source.id,
        job_type="index_video",
        status="queued",
        idempotency_key=f"{workspace_id}:{source.id}:index_video:mock",
        created_at=created_at,
        metadata_jsonb={"smoke": "hosted_mock_indexing"},
    )
    video = HostedVideoInput(
        youtube_video_id=video_id,
        title="Mocked hosted public indexing smoke",
        url=f"https://www.youtube.com/watch?v={video_id}",
        channel_id="UCleoandlongevity",
        duration_seconds=1200,
        metadata={"source": "hosted_mock_indexing_smoke"},
    )
    chunks = (
        TranscriptChunkInput(
            chunk_index=0,
            start_seconds=0,
            end_seconds=12.5,
            text="Hosted indexing should write transcript chunks and embeddings.",
        ),
        TranscriptChunkInput(
            chunk_index=1,
            start_seconds=12.5,
            end_seconds=25,
            text="Hybrid search should be queryable from generated Postgres operations.",
        ),
    )
    return plan_mock_hosted_public_indexing(source=source, job=job, video=video, chunks=chunks, search_query=query)


def mock_hosted_public_indexing_bootstrap_statements(source: Source, job: Job) -> tuple[SqlStatement, ...]:
    return (
        SqlStatement(
            sql="""
INSERT INTO workspaces (id, name, status)
VALUES (%(workspace_id)s, %(name)s, 'active')
ON CONFLICT (id) DO UPDATE
SET name = EXCLUDED.name,
    status = EXCLUDED.status
RETURNING *;
""".strip(),
            params={"workspace_id": source.workspace_id, "name": f"Hosted smoke {source.workspace_id}"},
        ),
        SqlStatement(
            sql="""
INSERT INTO sources (
    id, workspace_id, source_type, source_url, canonical_channel_id,
    canonical_playlist_id, canonical_video_id, display_name, selected,
    auto_index_allowed, import_source, auth_grant_id, metadata_json, status
)
VALUES (
    %(id)s, %(workspace_id)s, %(source_type)s, %(source_url)s,
    %(canonical_channel_id)s, %(canonical_playlist_id)s, %(canonical_video_id)s,
    %(display_name)s, %(selected)s, %(auto_index_allowed)s, %(import_source)s,
    %(auth_grant_id)s, %(metadata_json)s::jsonb, %(status)s
)
ON CONFLICT (id) DO UPDATE
SET source_type = EXCLUDED.source_type,
    source_url = EXCLUDED.source_url,
    canonical_channel_id = EXCLUDED.canonical_channel_id,
    canonical_playlist_id = EXCLUDED.canonical_playlist_id,
    canonical_video_id = EXCLUDED.canonical_video_id,
    display_name = EXCLUDED.display_name,
    selected = EXCLUDED.selected,
    auto_index_allowed = EXCLUDED.auto_index_allowed,
    import_source = EXCLUDED.import_source,
    auth_grant_id = EXCLUDED.auth_grant_id,
    metadata_json = EXCLUDED.metadata_json,
    status = EXCLUDED.status,
    updated_at = now()
RETURNING *;
""".strip(),
            params={
                "id": source.id,
                "workspace_id": source.workspace_id,
                "source_type": source.source_type,
                "source_url": source.source_url,
                "canonical_channel_id": source.canonical_channel_id,
                "canonical_playlist_id": source.canonical_playlist_id,
                "canonical_video_id": source.canonical_video_id,
                "display_name": source.display_name,
                "selected": source.selected,
                "auto_index_allowed": source.auto_index_allowed,
                "import_source": source.import_source,
                "auth_grant_id": source.auth_grant_id,
                "metadata_json": _json_param(source.metadata_jsonb),
                "status": source.status,
            },
        ),
        SqlStatement(
            sql="""
INSERT INTO jobs (
    id, workspace_id, source_id, job_type, status, priority,
    idempotency_key, run_after, executor_kind, executor_ref, metadata_json, created_at
)
VALUES (
    %(id)s, %(workspace_id)s, %(source_id)s, %(job_type)s, %(status)s,
    %(priority)s, %(idempotency_key)s, %(run_after)s, %(executor_kind)s,
    %(executor_ref)s, %(metadata_json)s::jsonb, %(created_at)s
)
ON CONFLICT (workspace_id, idempotency_key) DO UPDATE
SET status = EXCLUDED.status,
    priority = EXCLUDED.priority,
    source_id = EXCLUDED.source_id,
    metadata_json = EXCLUDED.metadata_json
RETURNING *;
""".strip(),
            params={
                "id": job.id,
                "workspace_id": job.workspace_id,
                "source_id": job.source_id,
                "job_type": job.job_type,
                "status": job.status,
                "priority": job.priority,
                "idempotency_key": job.idempotency_key,
                "run_after": job.run_after,
                "executor_kind": job.executor_kind,
                "executor_ref": job.executor_ref,
                "metadata_json": _json_param(job.metadata_jsonb),
                "created_at": job.created_at,
            },
        ),
    )


def postgres_url_from_env(
    *,
    url_env: str = "YUTOME_POSTGRES_URL",
    environ: Mapping[str, str] | None = None,
    fallback_envs: tuple[str, ...] = ("DATABASE_URL",),
) -> str | None:
    env = os.environ if environ is None else environ
    for name in (url_env, *fallback_envs):
        value = env.get(name)
        if value and value.strip():
            return value.strip()
    return None


def redact_postgres_url(url: str | None) -> str | None:
    if not url:
        return None
    if "@" not in url:
        return url
    prefix, suffix = url.rsplit("@", 1)
    scheme, _, _credentials = prefix.partition("://")
    return f"{scheme}://***@{suffix}" if scheme else f"***@{suffix}"


def connect_postgres(*, url: str | None = None, url_env: str = "YUTOME_POSTGRES_URL") -> Any:
    resolved = url or postgres_url_from_env(url_env=url_env)
    if resolved is None:
        raise HostedRuntimeError(f"Set {url_env} or DATABASE_URL to use hosted Postgres.")
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(resolved, autocommit=True, row_factory=dict_row)


def _rows_from_result(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if hasattr(result, "mappings"):
        return [dict(row) for row in result.mappings()]
    if hasattr(result, "fetchall"):
        return [dict(row) for row in result.fetchall()]
    try:
        return [dict(row) for row in result]
    except TypeError:
        return []


def _job_from_row(row: Mapping[str, Any]) -> Job:
    return Job(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        source_id=str(row["source_id"]) if row.get("source_id") is not None else None,
        job_type=row["job_type"],
        status=row.get("status", "queued"),
        priority=int(row.get("priority", 100)),
        idempotency_key=str(row["idempotency_key"]),
        run_after=row.get("run_after"),
        executor_kind=row.get("executor_kind"),
        executor_ref=row.get("executor_ref"),
        lease_owner=row.get("lease_owner"),
        leased_at=row.get("leased_at"),
        lease_expires_at=row.get("lease_expires_at"),
        retry_after=row.get("retry_after"),
        created_at=row.get("created_at") or datetime.now(timezone.utc),
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        cancelled_at=row.get("cancelled_at"),
        error_code=row.get("error_code"),
        error_message=row.get("error_message"),
        metadata_jsonb=dict(_json_value(row.get("metadata_json"))),
    )


def _execution_result_dict(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(getattr(value, "__dict__", {}))


def _validate_positive(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _json_param(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_value(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, bytes):
        return json.loads(value.decode("utf-8"))
    return value


__all__ = [
    "HostedCommandRunner",
    "HostedDbCheck",
    "HostedIndexingSmokeResult",
    "HostedPostgresSettings",
    "HostedRuntimeError",
    "HostedTickResult",
    "build_hosted_api_app",
    "connect_postgres",
    "maintenance_tick_sql",
    "mock_hosted_public_indexing_bootstrap_statements",
    "mock_hosted_public_indexing_plan",
    "postgres_url_from_env",
    "redact_postgres_url",
    "source_refresh_tick_sql",
]
