from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from yutome.config import AppConfig
from yutome.hosted.control_plane import Job, Source, SourceRefreshPolicy, TERMINAL_JOB_STATUSES
from yutome.hosted.errors import redact_sensitive_failure_text
from yutome.hosted.ids import input_hash
from yutome.hosted.billing import (
    StripeMeterExportWorkerResult,
    balance_reconciliation_input_sql,
    billing_debug_snapshot_from_rows,
    billing_debug_snapshot_sql,
    claim_stripe_meter_exports_sql,
    derive_workspace_balance_snapshot_from_rows,
    finish_stripe_meter_export_sql,
    stripe_meter_event_payload,
    stripe_meter_export_event_from_row,
    upsert_workspace_balance_sql,
)
from yutome.hosted.indexing import (
    HostedIndexingExecutor,
    HostedSourceDiscoveryExecutor,
    enqueue_index_video_job_sql,
    source_from_public_youtube_input,
)
from yutome.hosted.gate import UsageGate
from yutome.hosted.jobs import claim_jobs_sql
from yutome.hosted.ledger import PostgresUsageGate, PostgresUsageLedger, release_stale_unknown_usage_reservations
from yutome.hosted.postgres import apply_hosted_schema, apply_phase1_schema, apply_phase4_schema, apply_schema
from yutome.hosted.repositories import SqlStatement, usage_repository_constraint_statements
from yutome.hosted.schema import source_refresh_policies, sources, workspaces
from yutome.hosted.search_store import PostgresVectorChordSearchStore
from yutome.hosted.sqlalchemy_core import compile_postgres_statement


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


class HostedJobSeedResult(BaseModel):
    ok: bool = True
    workspace_id: str
    source_id: str
    source_type: str
    source_url: str
    job_id: str | None = None
    job_type: str | None = None
    youtube_video_id: str | None = None
    refresh_policy_id: str | None = None
    cadence_seconds: int | None = None


class HostedRealIndexingSmokeResult(BaseModel):
    ok: bool
    dev_only: bool = False
    migrated: bool
    migration_phase: MigrationPhase | None = None
    applied_migrations: int = 0
    workspace_id: str
    source_id: str
    job_id: str
    youtube_video_id: str
    worker: dict[str, Any]


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
        # Per-thread connections: the hosted API serves concurrent requests from a
        # threadpool over this handle, so a single shared psycopg connection would
        # let transactions from different requests interleave on one connection.
        url_env = self.config.database.postgres_url_env
        self.connection = ThreadLocalConnection(lambda: connect_postgres(url_env=url_env))
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
        url_env = self.config.database.postgres_url_env
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

    def source_add(
        self,
        *,
        workspace_id: str,
        source_url: str,
        display_name: str | None = None,
        cadence_seconds: int = 900,
        max_new_videos_per_run: int = 25,
        refresh_enabled: bool = True,
    ) -> HostedJobSeedResult:
        _validate_positive("cadence_seconds", cadence_seconds)
        _validate_positive("max_new_videos_per_run", max_new_videos_per_run)
        source = _source_from_cli_input(workspace_id=workspace_id, source_url=source_url, display_name=display_name)
        policy_id = f"srp_{input_hash({'workspace_id': workspace_id, 'source_id': source.id}, prefix='').lstrip('_')[:24]}"
        policy = SourceRefreshPolicy(
            id=policy_id,
            workspace_id=workspace_id,
            source_id=source.id,
            enabled=refresh_enabled,
            cadence_seconds=cadence_seconds,
            next_run_at=datetime.now(timezone.utc),
            max_new_videos_per_run=max_new_videos_per_run,
        )
        self.connect().execute(ensure_workspace_sql(workspace_id=workspace_id).sql, ensure_workspace_sql(workspace_id=workspace_id).params)
        self.connect().execute(upsert_hosted_source_sql(source).sql, upsert_hosted_source_sql(source).params)
        self.connect().execute(upsert_source_refresh_policy_sql(policy).sql, upsert_source_refresh_policy_sql(policy).params)
        return HostedJobSeedResult(
            workspace_id=workspace_id,
            source_id=source.id,
            source_type=source.source_type,
            source_url=source.source_url,
            refresh_policy_id=policy.id,
            cadence_seconds=cadence_seconds,
        )

    def enqueue_index_video(
        self,
        *,
        workspace_id: str,
        source_url: str,
        display_name: str | None = None,
        priority: int = 100,
    ) -> HostedJobSeedResult:
        _validate_positive("priority", priority)
        source = _source_from_cli_input(workspace_id=workspace_id, source_url=source_url, display_name=display_name)
        video_id = source.canonical_video_id
        if not video_id:
            raise HostedRuntimeError("enqueue-index-video requires a concrete YouTube video URL or 11-character video id.")
        now = datetime.now(timezone.utc)
        workspace_statement = ensure_workspace_sql(workspace_id=workspace_id)
        source_statement = upsert_hosted_source_sql(source)
        job_statement = enqueue_index_video_job_sql(
            workspace_id=workspace_id,
            source_id=source.id,
            video_id=video_id,
            priority=priority,
            now=now,
            metadata={"seeded_by": "hosted_cli"},
        )
        self.connect().execute(workspace_statement.sql, workspace_statement.params)
        self.connect().execute(source_statement.sql, source_statement.params)
        rows = _rows_from_result(self.connect().execute(job_statement.sql, job_statement.params))
        row = rows[0] if rows else {}
        return HostedJobSeedResult(
            workspace_id=workspace_id,
            source_id=source.id,
            source_type=source.source_type,
            source_url=source.source_url,
            job_id=str(row.get("id") or job_statement.params["id"]),
            job_type="index_video",
            youtube_video_id=video_id,
        )

    def real_indexing_smoke(
        self,
        *,
        workspace_id: str,
        source_url: str = "https://www.youtube.com/watch?v=OEDoJyhQhXs",
        migrate: bool = False,
        migration_phase: MigrationPhase = "hosted",
        lease_owner: str = "hosted-real-indexing-smoke",
    ) -> HostedRealIndexingSmokeResult:
        applied_migrations = self.migrate(phase=migration_phase) if migrate else 0
        seeded = self.enqueue_index_video(workspace_id=workspace_id, source_url=source_url)
        worker = self.worker_once(lease_owner=lease_owner, limit=1, workspace_id=workspace_id)
        executions = worker.params.get("executions") if isinstance(worker.params, dict) else None
        ok = bool(executions and executions[0].get("status") == "succeeded")
        return HostedRealIndexingSmokeResult(
            ok=ok,
            migrated=migrate,
            migration_phase=migration_phase if migrate else None,
            applied_migrations=applied_migrations,
            workspace_id=workspace_id,
            source_id=seeded.source_id,
            job_id=seeded.job_id or "",
            youtube_video_id=seeded.youtube_video_id or "",
            worker=worker.model_dump(mode="json"),
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
            job_types=["index_video", "discover_source"],
            executor_kind="railway",
            executor_ref=lease_owner,
        )
        result = self.connect().execute(statement.sql, statement.params)
        rows = _rows_from_result(result)
        executor = HostedIndexingExecutor(connection=self.connect(), config=self.config, gate=self.usage_gate(), ledger=self.usage_ledger())
        discovery_executor = HostedSourceDiscoveryExecutor(
            connection=self.connect(),
            config=self.config,
            gate=self.usage_gate(),
            ledger=self.usage_ledger(),
        )
        executions = []
        for row in rows:
            job = _job_from_row(row)
            if job.job_type == "index_video":
                executions.append(_execution_result_dict(executor.execute(job, lease_owner=lease_owner, lease_seconds=lease_seconds)))
            elif job.job_type == "discover_source":
                executions.append(
                    _execution_result_dict(discovery_executor.execute(job, lease_owner=lease_owner, lease_seconds=lease_seconds))
                )
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
        connection = self.connect()
        clock = datetime.now(timezone.utc)
        statement = maintenance_tick_sql(now=clock, limit=limit)
        result = connection.execute(statement.sql, statement.params)
        rows = _rows_from_result(result)
        released_unknown = release_stale_unknown_usage_reservations(connection, now=clock, limit=limit)
        return HostedTickResult(
            tick="maintenance_tick",
            attempted=True,
            affected_rows=len(rows) + released_unknown,
            sql=statement.sql,
            params={**statement.params, "released_unknown_usage_reservations": released_unknown},
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

    def reconcile_balance(
        self,
        *,
        workspace_id: str,
        entitlement_policy_id: str,
        period_start_at: datetime,
        period_end_at: datetime,
        starting_units: dict[str, Any] | None = None,
        unlimited_units: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        statement = balance_reconciliation_input_sql(
            workspace_id=workspace_id,
            period_start_at=period_start_at,
            period_end_at=period_end_at,
        )
        rows = _rows_from_result(self.connect().execute(statement.sql, statement.params))
        usage_rows = tuple(row for row in rows if row.get("row_kind") == "usage")
        reserved_rows = tuple(row for row in rows if row.get("row_kind") == "reservation")
        snapshot = derive_workspace_balance_snapshot_from_rows(
            workspace_id=workspace_id,
            entitlement_policy_id=entitlement_policy_id,
            period_start_at=period_start_at,
            period_end_at=period_end_at,
            usage_rows=usage_rows,
            reserved_rows=reserved_rows,
            starting_units=starting_units,
            unlimited_units=unlimited_units,
            updated_at=datetime.now(timezone.utc),
        )
        upsert = upsert_workspace_balance_sql(snapshot)
        self.connect().execute(upsert.sql, upsert.params)
        return snapshot.model_dump(mode="json")

    def stripe_meter_export_once(
        self,
        *,
        lease_owner: str,
        limit: int = 100,
        secret_key: str | None = None,
        stripe_api_base: str | None = None,
    ) -> StripeMeterExportWorkerResult:
        key = secret_key if secret_key is not None else os.environ.get("STRIPE_SECRET_KEY")
        if not key:
            return StripeMeterExportWorkerResult(
                attempted=False,
                affected_rows=0,
                skipped=1,
                secret_key_configured=False,
                rows=[
                    {
                        "status": "skipped",
                        "reason": "STRIPE_SECRET_KEY is not configured; no Stripe meter exports were claimed.",
                    }
                ],
            )
        now = datetime.now(timezone.utc)
        claim = claim_stripe_meter_exports_sql(lease_owner=lease_owner, now=now, limit=limit)
        rows = _rows_from_result(self.connect().execute(claim.sql, claim.params))
        api_base = (stripe_api_base or os.environ.get("STRIPE_API_BASE") or "https://api.stripe.com").rstrip("/")
        result = StripeMeterExportWorkerResult(
            attempted=True,
            affected_rows=len(rows),
            secret_key_configured=True,
            rows=[],
        )
        for row in rows:
            export_id = str(row["id"])
            try:
                export = stripe_meter_export_event_from_row(row)
                if not export.stripe_customer_id:
                    # No Stripe Customer yet (usage recorded before subscribe): cannot
                    # bill; mark skipped (terminal) rather than POST a null customer.
                    finish = finish_stripe_meter_export_sql(
                        export_id=export_id,
                        now=datetime.now(timezone.utc),
                        replay_status="skipped",
                        error_code="stripe_customer_missing",
                        error_message="No active Stripe customer for the workspace at export time.",
                    )
                    self.connect().execute(finish.sql, finish.params)
                    result.skipped += 1
                    result.rows.append({"id": export_id, "status": "skipped", "reason": "stripe_customer_missing"})
                    continue
                payload = stripe_meter_event_payload(export)
                response = _post_stripe_meter_event(
                    payload,
                    secret_key=key,
                    api_base=api_base,
                    idempotency_key=export.idempotency_key,
                )
                if str(response.get("object") or "") != "billing.meter_event":
                    raise HostedRuntimeError(
                        f"Stripe did not acknowledge the meter event: object={response.get('object')!r}"
                    )
                finish = finish_stripe_meter_export_sql(
                    export_id=export_id,
                    now=datetime.now(timezone.utc),
                    replay_status="succeeded",
                    stripe_meter_event_identifier=str(response.get("identifier") or export.stripe_identifier),
                )
                self.connect().execute(finish.sql, finish.params)
                result.succeeded += 1
                result.rows.append({"id": export_id, "status": "succeeded", "stripe_response": response})
            except Exception as exc:  # billing mirror failure must not affect authorization paths
                error_message = redact_sensitive_failure_text(str(exc))
                replay_status = "skipped" if _stripe_timestamp_out_of_range(error_message) else "failed"
                finish = finish_stripe_meter_export_sql(
                    export_id=export_id,
                    now=datetime.now(timezone.utc),
                    replay_status=replay_status,
                    error_code="stripe_meter_event_failed",
                    error_message=error_message,
                )
                self.connect().execute(finish.sql, finish.params)
                if replay_status == "skipped":
                    result.skipped += 1
                else:
                    result.failed += 1
                result.rows.append({"id": export_id, "status": replay_status, "error": error_message})
        return result


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
    SELECT
        policy.id AS policy_id,
        policy.workspace_id,
        policy.source_id,
        policy.next_run_at,
        policy.cadence_seconds,
        policy.jitter_seconds,
        source.source_type,
        source.canonical_video_id,
        policy.max_new_videos_per_run
    FROM source_refresh_policies AS policy
    JOIN sources AS source
      ON source.id = policy.source_id
     AND source.workspace_id = policy.workspace_id
    WHERE policy.enabled = true
      AND policy.next_run_at <= %(now)s
      AND (policy.locked_by IS NULL OR policy.locked_until <= %(now)s)
      AND source.status = 'active'
      AND source.auto_index_allowed = true
    ORDER BY policy.next_run_at ASC, policy.id ASC
    LIMIT %(limit)s
    FOR UPDATE OF policy SKIP LOCKED
),
enqueued AS (
    INSERT INTO jobs (
        id,
        workspace_id,
        source_id,
        job_type,
        status,
        priority,
        idempotency_key,
        run_after,
        executor_kind,
        executor_ref,
        metadata_json,
        created_at
    )
    SELECT
        'job_' || md5(due.workspace_id || ':' || due.source_id || ':' || due.next_run_at::text || ':source_refresh'),
        due.workspace_id,
        due.source_id,
        CASE WHEN due.source_type = 'video' THEN 'index_video' ELSE 'discover_source' END,
        'queued',
        100,
        due.workspace_id || ':' || due.source_id || ':source_refresh:' || due.next_run_at::text,
        %(now)s,
        'railway',
        %(lease_owner)s,
        jsonb_build_object(
            'source_refresh_policy_id', due.policy_id,
            'scheduled_for', due.next_run_at,
            'source_type', due.source_type,
            'canonical_video_id', due.canonical_video_id,
            'max_new_videos_per_run', due.max_new_videos_per_run
        ),
        %(now)s
    FROM due
    ON CONFLICT (workspace_id, idempotency_key) DO UPDATE
    SET idempotency_key = jobs.idempotency_key
    RETURNING id, workspace_id, source_id, job_type, idempotency_key
),
advanced AS (
    UPDATE source_refresh_policies AS policy
    SET last_started_at = %(now)s,
        locked_by = %(lease_owner)s,
        locked_until = %(locked_until)s,
        next_run_at = GREATEST(policy.next_run_at, %(now)s)
            + make_interval(
                secs => policy.cadence_seconds
                    + CASE
                        WHEN policy.jitter_seconds > 0
                        THEN floor(random() * ((policy.jitter_seconds * 2) + 1))::integer - policy.jitter_seconds
                        ELSE 0
                      END
            ),
        updated_at = %(now)s
    FROM due
    WHERE policy.id = due.policy_id
    RETURNING policy.*
)
SELECT
    advanced.*,
    enqueued.id AS job_id,
    enqueued.job_type AS job_type,
    enqueued.idempotency_key AS job_idempotency_key
FROM advanced
JOIN enqueued
  ON enqueued.workspace_id = advanced.workspace_id
 AND enqueued.source_id = advanced.source_id;
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
      AND status <> ALL(%(terminal_statuses)s::text[])
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


def _sql_statement(statement: Any) -> SqlStatement:
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def ensure_workspace_sql(*, workspace_id: str, name: str | None = None) -> SqlStatement:
    statement = (
        insert(workspaces)
        .values(id=workspace_id, name=name or workspace_id, status="active")
        .on_conflict_do_nothing(index_elements=[workspaces.c.id])
        .returning(workspaces)
    )
    return _sql_statement(statement)


def upsert_hosted_source_sql(source: Source) -> SqlStatement:
    statement = insert(sources).values(
        id=source.id,
        workspace_id=source.workspace_id,
        source_type=source.source_type,
        source_url=source.source_url,
        canonical_channel_id=source.canonical_channel_id,
        canonical_playlist_id=source.canonical_playlist_id,
        canonical_video_id=source.canonical_video_id,
        display_name=source.display_name,
        selected=source.selected,
        auto_index_allowed=source.auto_index_allowed,
        import_source=source.import_source,
        auth_grant_id=source.auth_grant_id,
        metadata_json=_json_param(source.metadata_jsonb),
        status=source.status,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[sources.c.workspace_id, sources.c.source_url],
        set_={
            "source_type": statement.excluded.source_type,
            "canonical_channel_id": func.coalesce(statement.excluded.canonical_channel_id, sources.c.canonical_channel_id),
            "canonical_playlist_id": func.coalesce(statement.excluded.canonical_playlist_id, sources.c.canonical_playlist_id),
            "canonical_video_id": func.coalesce(statement.excluded.canonical_video_id, sources.c.canonical_video_id),
            "display_name": func.coalesce(statement.excluded.display_name, sources.c.display_name),
            "selected": statement.excluded.selected,
            "auto_index_allowed": statement.excluded.auto_index_allowed,
            "import_source": statement.excluded.import_source,
            "auth_grant_id": statement.excluded.auth_grant_id,
            # EXCLUDED.metadata_json is the jsonb column value, so `jsonb || jsonb` needs no cast.
            "metadata_json": sources.c.metadata_json.op("||")(statement.excluded.metadata_json),
            "status": statement.excluded.status,
            "updated_at": func.now(),
        },
    ).returning(sources)
    return _sql_statement(statement)


def upsert_source_refresh_policy_sql(policy: SourceRefreshPolicy) -> SqlStatement:
    statement = insert(source_refresh_policies).values(
        id=policy.id,
        workspace_id=policy.workspace_id,
        source_id=policy.source_id,
        enabled=policy.enabled,
        cadence_seconds=policy.cadence_seconds,
        jitter_seconds=policy.jitter_seconds,
        next_run_at=policy.next_run_at,
        max_new_videos_per_run=policy.max_new_videos_per_run,
        max_index_jobs_per_day=policy.max_index_jobs_per_day,
        policy_snapshot_json=_json_param(policy.policy_snapshot_jsonb),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[source_refresh_policies.c.workspace_id, source_refresh_policies.c.source_id],
        set_={
            "enabled": statement.excluded.enabled,
            "cadence_seconds": statement.excluded.cadence_seconds,
            "jitter_seconds": statement.excluded.jitter_seconds,
            "max_new_videos_per_run": statement.excluded.max_new_videos_per_run,
            "max_index_jobs_per_day": statement.excluded.max_index_jobs_per_day,
            "policy_snapshot_json": statement.excluded.policy_snapshot_json,
            "updated_at": func.now(),
        },
    ).returning(source_refresh_policies)
    return _sql_statement(statement)


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


class ThreadLocalConnection:
    """A connection handle that hands each thread its own psycopg connection.

    The hosted API runs sync request handlers in FastAPI's threadpool, and the
    gate/ledger wrap work in ``connection.transaction()``. A single shared
    psycopg connection is not safe for concurrent use across threads — overlapping
    transactions raise ``OutOfOrderTransactionNesting`` and an error on one request
    can poison the next. This proxy keeps the connection object stable for the
    adapter/gate/ledger/search-store (so nothing else changes) while routing every
    call to a connection owned by the calling thread. Concurrency is bounded by the
    threadpool, so the open-connection count is bounded too. Single-threaded callers
    (CLI migrate/worker) transparently get exactly one connection, as before.
    """

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._local = threading.local()

    def _connection(self) -> Any:
        conn = getattr(self._local, "conn", None)
        if conn is None or getattr(conn, "closed", False):
            conn = self._factory()
            self._local.conn = conn
        return conn

    def __getattr__(self, name: str) -> Any:
        # Only reached for names not defined on the proxy itself, so every real
        # connection method/attribute (execute, transaction, cursor, pgconn, ...)
        # resolves against the calling thread's connection.
        return getattr(self._connection(), name)


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


def _source_from_cli_input(*, workspace_id: str, source_url: str, display_name: str | None = None) -> Source:
    source_hash = input_hash({"workspace_id": workspace_id, "source_url": source_url.strip()}, prefix="").lstrip("_")[:24]
    return source_from_public_youtube_input(
        workspace_id=workspace_id,
        source_id=f"src_{source_hash}",
        value=source_url,
        import_source="cli",
        display_name=display_name,
    )


def _execution_result_dict(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return dict(getattr(value, "__dict__", {}))


def _post_stripe_meter_event(
    payload: dict[str, Any],
    *,
    secret_key: str,
    api_base: str,
    idempotency_key: str,
) -> dict[str, Any]:
    form = {
        "event_name": payload["event_name"],
        "payload[stripe_customer_id]": payload["payload"]["stripe_customer_id"],
        "payload[value]": payload["payload"]["value"],
        "identifier": payload["identifier"],
        "timestamp": str(payload["timestamp"]),
    }
    body = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(
        f"{api_base}/v1/billing/meter_events",
        data=body,
        headers={
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Idempotency-Key": idempotency_key,
            "User-Agent": "yutome-hosted-billing/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            text = response.read().decode("utf-8")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        raise HostedRuntimeError(f"Stripe meter event failed with HTTP {exc.code}: {error_text[:500]}") from exc


def _stripe_timestamp_out_of_range(message: str) -> bool:
    lowered = message.lower()
    return "timestamp" in lowered and ("range" in lowered or "older" in lowered or "future" in lowered)


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
    "HostedJobSeedResult",
    "HostedPostgresSettings",
    "HostedRealIndexingSmokeResult",
    "HostedRuntimeError",
    "HostedTickResult",
    "build_hosted_api_app",
    "connect_postgres",
    "ensure_workspace_sql",
    "maintenance_tick_sql",
    "postgres_url_from_env",
    "redact_postgres_url",
    "source_refresh_tick_sql",
    "upsert_hosted_source_sql",
    "upsert_source_refresh_policy_sql",
]
