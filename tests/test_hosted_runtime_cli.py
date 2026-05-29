from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from yutome.config import AppConfig, HostedConfig
from yutome.hosted.control_plane import Job
from yutome.hosted.http_api import TOKEN_ENV_VAR
from yutome.hosted.runtime import (
    HostedCommandRunner,
    HostedDbCheck,
    HostedTickResult,
    balance_rollover_tick_sql,
    build_hosted_api_app,
    maintenance_tick_sql,
    postgres_url_from_env,
    source_refresh_tick_sql,
)
from yutome.hosted.models import EntitlementPolicy, ProviderAllocation, UsageEvent, WorkspaceBalance


class RecordingConnection:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((statement, dict(params or {})))
        return self.rows


class FakeHostedRunner:
    def __init__(self) -> None:
        self.config = AppConfig(hosted=HostedConfig(workspace_id="ws_default"))
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def migrate(self, *, phase: str = "hosted") -> int:
        self.calls.append(("migrate", {"phase": phase}))
        return 7

    def db_check(self) -> HostedDbCheck:
        self.calls.append(("db_check", {}))
        return HostedDbCheck(
            ok=True,
            url_env="YUTOME_POSTGRES_URL",
            url_configured=True,
            database_reachable=True,
            extensions={"vector": True, "vchord": True, "pg_tokenizer": True, "vchord_bm25": True},
        )

    def search_smoke(self, *, workspace_id: str, query: str, limit: int = 3) -> dict[str, Any]:
        self.calls.append(("search_smoke", {"workspace_id": workspace_id, "query": query, "limit": limit}))
        return {"rows": [{"chunk_id": "chunk_1", "score": 1.0}], "usage": {"operation": "lexical_query"}}

    def source_add(
        self,
        *,
        workspace_id: str,
        source_url: str,
        display_name: str | None = None,
        cadence_seconds: int = 900,
        max_new_videos_per_run: int = 25,
        refresh_enabled: bool = True,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "source_add",
                {
                    "workspace_id": workspace_id,
                    "source_url": source_url,
                    "display_name": display_name,
                    "cadence_seconds": cadence_seconds,
                    "max_new_videos_per_run": max_new_videos_per_run,
                    "refresh_enabled": refresh_enabled,
                },
            )
        )
        return {
            "ok": True,
            "workspace_id": workspace_id,
            "source_id": "src_cli",
            "source_type": "handle",
            "source_url": source_url,
            "refresh_policy_id": "srp_cli",
            "cadence_seconds": cadence_seconds,
        }

    def enqueue_index_video(
        self,
        *,
        workspace_id: str,
        source_url: str,
        display_name: str | None = None,
        priority: int = 100,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "enqueue_index_video",
                {
                    "workspace_id": workspace_id,
                    "source_url": source_url,
                    "display_name": display_name,
                    "priority": priority,
                },
            )
        )
        return {
            "ok": True,
            "workspace_id": workspace_id,
            "source_id": "src_video",
            "source_type": "video",
            "source_url": source_url,
            "job_id": "job_video",
            "job_type": "index_video",
            "youtube_video_id": "OEDoJyhQhXs",
        }

    def real_indexing_smoke(
        self,
        *,
        workspace_id: str,
        source_url: str = "https://www.youtube.com/watch?v=OEDoJyhQhXs",
        migrate: bool = False,
        migration_phase: str = "hosted",
        lease_owner: str = "hosted-real-indexing-smoke",
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "real_indexing_smoke",
                {
                    "workspace_id": workspace_id,
                    "source_url": source_url,
                    "migrate": migrate,
                    "migration_phase": migration_phase,
                    "lease_owner": lease_owner,
                },
            )
        )
        return {
            "ok": True,
            "dev_only": False,
            "migrated": migrate,
            "migration_phase": migration_phase if migrate else None,
            "applied_migrations": 7 if migrate else 0,
            "workspace_id": workspace_id,
            "source_id": "src_video",
            "job_id": "job_video",
            "youtube_video_id": "OEDoJyhQhXs",
            "worker": {"tick": "worker_once", "params": {"executions": [{"status": "succeeded"}]}},
        }

    def billing_status(
        self,
        *,
        workspace_id: str,
        limit: int = 20,
        operation: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(("billing_status", {"workspace_id": workspace_id, "limit": limit, "operation": operation}))
        return {
            "workspace_id": workspace_id,
            "limit": limit,
            "operation": operation,
            "rows": [
                {
                    "reservation_id": "res_denied",
                    "workspace_id": workspace_id,
                    "job_id": "job_1",
                    "job_status": "retry_wait",
                    "job_error_code": "usage_limit_exceeded",
                    "job_error_message": "Fallback paused by policy.",
                    "operation_id": "op_1",
                    "job_operation": "gemini.transcribe_media",
                    "operation_status": "denied",
                    "video_id": "vid_1",
                    "subject": "gemini",
                    "operation": "transcribe_media",
                    "operation_key": "gemini.transcribe_media",
                    "allocation_id": "alloc_gemini",
                    "credential_mode": "hosted",
                    "reservation_status": "denied",
                    "entitlement_decision": {
                        "allowed": False,
                        "reason": "usage_limit_exceeded",
                        "message": "Estimated media_seconds exceeds the operation limit.",
                    },
                    "estimated_units": {"media_seconds": 14400},
                    "idempotency_key": "idem_denied",
                    "created_at": "2026-05-26T04:00:00Z",
                    "metadata": {},
                    "usage_events": [
                        {
                            "id": "evt_denied",
                            "event_type": "reservation_created",
                            "status": "denied",
                            "actual_units": {},
                            "error_code": "usage_limit_exceeded",
                            "provider_request_id": None,
                            "created_at": "2026-05-26T04:00:00Z",
                            "metadata": {},
                        }
                    ],
                    "meter_exports": [
                        {
                            "id": "stripe:ws:evt_denied:credits",
                            "usage_event_id": "evt_denied",
                            "replay_status": "skipped",
                            "stripe_customer_id": None,
                            "meter_unit": "credits",
                            "value": "0",
                            "stripe_meter_event_identifier": None,
                            "source_event_dedupe_key": "stripe:ws:evt_denied:credits",
                            "attempt_count": 0,
                            "last_error": {},
                            "exported_at": None,
                            "updated_at": "2026-05-26T04:00:00Z",
                        }
                    ],
                }
            ],
        }

    def stripe_meter_export_once(self, *, lease_owner: str, limit: int = 100) -> HostedTickResult:
        self.calls.append(("stripe_meter_export_once", {"lease_owner": lease_owner, "limit": limit}))
        class Result:
            tick = "stripe_meter_export_once"
            attempted = True
            affected_rows = 1
            succeeded = 1
            failed = 0

            def model_dump(self, mode: str = "json") -> dict[str, Any]:
                return {
                    "tick": self.tick,
                    "attempted": self.attempted,
                    "affected_rows": self.affected_rows,
                    "succeeded": self.succeeded,
                    "failed": self.failed,
                }

        return Result()  # type: ignore[return-value]

    def reconcile_balance(
        self,
        *,
        workspace_id: str,
        entitlement_policy_id: str,
        period_start_at: datetime,
        period_end_at: datetime,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "reconcile_balance",
                {
                    "workspace_id": workspace_id,
                    "entitlement_policy_id": entitlement_policy_id,
                    "period_start_at": period_start_at,
                    "period_end_at": period_end_at,
                },
            )
        )
        return {
            "workspace_id": workspace_id,
            "entitlement_policy_id": entitlement_policy_id,
            "remaining_units": {"credits": "7.0"},
        }

    def worker_once(
        self,
        *,
        lease_owner: str,
        limit: int = 1,
        lease_seconds: int = 900,
        workspace_id: str | None = None,
    ) -> HostedTickResult:
        self.calls.append(
            (
                "worker_once",
                {
                    "lease_owner": lease_owner,
                    "limit": limit,
                    "lease_seconds": lease_seconds,
                    "workspace_id": workspace_id,
                },
            )
        )
        return HostedTickResult(tick="worker_once", attempted=True, affected_rows=1)

    def source_refresh_tick(self, *, lease_owner: str, limit: int = 25, lock_seconds: int = 900) -> HostedTickResult:
        self.calls.append(
            ("source_refresh_tick", {"lease_owner": lease_owner, "limit": limit, "lock_seconds": lock_seconds})
        )
        return HostedTickResult(tick="source_refresh_tick", attempted=True, affected_rows=2)

    def maintenance_tick(self, *, limit: int = 100) -> HostedTickResult:
        self.calls.append(("maintenance_tick", {"limit": limit}))
        return HostedTickResult(tick="maintenance_tick", attempted=True, affected_rows=3)


def test_postgres_url_from_env_prefers_hosted_env_then_database_url() -> None:
    assert (
        postgres_url_from_env(
            url_env="YUTOME_POSTGRES_URL",
            environ={"YUTOME_POSTGRES_URL": " postgres://hosted ", "DATABASE_URL": "postgres://fallback"},
        )
        == "postgres://hosted"
    )
    assert (
        postgres_url_from_env(url_env="YUTOME_POSTGRES_URL", environ={"DATABASE_URL": "postgres://fallback"})
        == "postgres://fallback"
    )


def test_db_check_missing_url_is_structured_and_does_not_connect(monkeypatch) -> None:
    monkeypatch.delenv("YUTOME_POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    runner = HostedCommandRunner(AppConfig())

    result = runner.db_check()

    assert result.ok is False
    assert result.url_configured is False
    assert result.database_reachable is False
    assert result.error == "postgres_url_missing"


def test_db_check_live_connection_errors_are_sanitized(monkeypatch) -> None:
    monkeypatch.setenv("YUTOME_POSTGRES_URL", "postgresql://user:secret@db.internal/yutome")

    class FailingStore:
        def __init__(self, _connection: Any) -> None:
            pass

        def extension_check(self) -> dict[str, bool]:
            raise RuntimeError("psycopg OperationalError for postgresql://user:secret@db.internal/yutome")

    monkeypatch.setattr("yutome.hosted.runtime.PostgresVectorChordSearchStore", FailingStore)
    runner = HostedCommandRunner(AppConfig(), connection=RecordingConnection())

    result = runner.db_check()
    payload = result.model_dump(mode="json")

    assert result.ok is False
    assert result.database_reachable is False
    assert result.error == "database_unreachable"
    assert "secret" not in json.dumps(payload)
    assert "postgresql://" not in json.dumps(payload)


def test_source_refresh_tick_sql_claims_due_policies_with_skip_locked() -> None:
    now = datetime(2026, 5, 26, 3, 30, tzinfo=timezone.utc)

    statement = source_refresh_tick_sql(lease_owner="worker-1", now=now, limit=5, lock_seconds=60)

    assert "FROM source_refresh_policies" in statement.sql
    assert "INSERT INTO jobs" in statement.sql
    assert "CASE WHEN due.source_type = 'video' THEN 'index_video' ELSE 'discover_source' END" in statement.sql
    assert "'max_new_videos_per_run', due.max_new_videos_per_run" in statement.sql
    assert "ON CONFLICT (workspace_id, idempotency_key)" in statement.sql
    assert "FOR UPDATE OF policy SKIP LOCKED" in statement.sql
    assert "next_run_at <= %(now)s" in statement.sql
    assert "policy.jitter_seconds" in statement.sql
    assert statement.params["lease_owner"] == "worker-1"
    assert statement.params["limit"] == 5


def test_maintenance_tick_sql_releases_expired_job_and_source_locks() -> None:
    now = datetime(2026, 5, 26, 3, 30, tzinfo=timezone.utc)

    statement = maintenance_tick_sql(now=now, limit=10)

    assert "expired_jobs AS" in statement.sql
    assert "expired_source_locks AS" in statement.sql
    assert "lease_expires_at <= %(now)s" in statement.sql
    assert "locked_until <= %(now)s" in statement.sql
    assert statement.params["limit"] == 10


def test_balance_rollover_tick_sql_reseeds_expired_periods_with_skip_locked() -> None:
    now = datetime(2026, 6, 1, 0, 5, tzinfo=timezone.utc)

    statement = balance_rollover_tick_sql(now=now, limit=50)

    # Only ended periods are claimed; the live month (period_end_at in the future) is skipped.
    assert "balance.period_end_at <= %(now)s" in statement.sql
    # Concurrent replica ticks are safe: each row is rolled by exactly one tick.
    assert "FOR UPDATE OF balance SKIP LOCKED" in statement.sql
    # The new window snaps to the calendar month containing now (carry-nothing).
    assert "period_start_at = date_trunc('month', %(now)s::timestamptz)" in statement.sql
    assert "period_end_at = date_trunc('month', %(now)s::timestamptz) + interval '1 month'" in statement.sql
    # remaining_units is re-seeded from the active EntitlementPolicy included allowance.
    assert "remaining_units_jsonb = due.included_units_jsonb" in statement.sql
    assert "policy.status = 'active'" in statement.sql
    # used/reserved reset for the fresh period.
    assert "used_units_jsonb = '{}'::jsonb" in statement.sql
    assert "reserved_units_jsonb = '{}'::jsonb" in statement.sql
    assert statement.params["limit"] == 50


def test_runner_balance_rollover_once_executes_tick_and_reports_rolled_workspaces() -> None:
    connection = RecordingConnection(
        rows=[
            {"workspace_id": "ws_a", "period_start_at": "2026-06-01", "period_end_at": "2026-07-01"},
            {"workspace_id": "ws_b", "period_start_at": "2026-06-01", "period_end_at": "2026-07-01"},
        ]
    )
    runner = HostedCommandRunner(AppConfig(), connection=connection)

    result = runner.balance_rollover_once(limit=10)

    assert result.tick == "balance_rollover_once"
    assert result.affected_rows == 2
    assert result.params["rolled_workspace_ids"] == ["ws_a", "ws_b"]
    assert "FROM workspace_balances" in connection.calls[0][0]
    assert connection.calls[0][1]["limit"] == 10


def test_runner_balance_rollover_once_no_ops_when_nothing_due() -> None:
    connection = RecordingConnection(rows=[])
    runner = HostedCommandRunner(AppConfig(), connection=connection)

    result = runner.balance_rollover_once(limit=10)

    assert result.affected_rows == 0
    assert result.params["rolled_workspace_ids"] == []


def test_runner_tick_methods_return_affected_rows() -> None:
    connection = RecordingConnection(rows=[{"id": "row_1"}, {"id": "row_2"}])
    runner = HostedCommandRunner(AppConfig(), connection=connection)

    source_result = runner.source_refresh_tick(lease_owner="worker-1", limit=2)
    maintenance_result = runner.maintenance_tick(limit=2)

    assert source_result.tick == "source_refresh_tick"
    assert source_result.affected_rows == 2
    assert maintenance_result.tick == "maintenance_tick"
    assert maintenance_result.affected_rows == 2
    assert len(connection.calls) == 3


def test_worker_once_claims_and_executes_index_video_jobs(monkeypatch) -> None:
    created_at = datetime(2026, 5, 26, 4, 0, tzinfo=timezone.utc)
    connection = RecordingConnection(
        rows=[
            {
                "id": "job_1",
                "workspace_id": "ws_cli",
                "source_id": "src_1",
                "job_type": "index_video",
                "status": "queued",
                "priority": 100,
                "idempotency_key": "ws_cli:src_1:index_video:h1",
                "lease_owner": "worker-1",
                "created_at": created_at,
                "metadata_json": {},
            }
        ]
    )
    executions: list[dict[str, Any]] = []

    class FakeExecutor:
        def __init__(self, **kwargs: Any) -> None:
            executions.append({"init": kwargs})

        def execute(self, job: Job, *, lease_owner: str, lease_seconds: int = 900):
            assert job.id == "job_1"
            assert lease_owner == "worker-1"
            assert lease_seconds == 900

            class Result:
                def __init__(self) -> None:
                    self.job_id = job.id
                    self.workspace_id = job.workspace_id
                    self.source_id = job.source_id
                    self.youtube_video_id = "OEDoJyhQhXs"
                    self.status = "succeeded"
                    self.hosted_video_id = "vid_1"
                    self.transcript_version_id = "tx_1"
                    self.chunks_written = 1
                    self.embeddings_written = 1
                    self.denied_operation = None
                    self.error_code = None
                    self.error_message = None

            return Result()

    monkeypatch.setattr("yutome.hosted.runtime.HostedIndexingExecutor", FakeExecutor)
    runner = HostedCommandRunner(AppConfig(), connection=connection)

    result = runner.worker_once(lease_owner="worker-1", workspace_id="ws_cli")

    assert result.affected_rows == 1
    assert result.params["executions"][0]["status"] == "succeeded"
    assert executions[0]["init"]["connection"] is connection
    assert "job_type = ANY(%(job_types)s::text[])" in connection.calls[0][0]
    assert connection.calls[0][1]["job_types"] == ["index_video", "discover_source"]


def test_worker_once_dispatches_discover_source_jobs(monkeypatch) -> None:
    created_at = datetime(2026, 5, 26, 4, 0, tzinfo=timezone.utc)
    connection = RecordingConnection(
        rows=[
            {
                "id": "job_discover",
                "workspace_id": "ws_cli",
                "source_id": "src_channel",
                "job_type": "discover_source",
                "status": "queued",
                "priority": 100,
                "idempotency_key": "ws_cli:src_channel:discover_source:h1",
                "lease_owner": "worker-1",
                "created_at": created_at,
                "metadata_json": {"source_refresh_policy_id": "srp_1"},
            }
        ]
    )
    discoveries: list[dict[str, Any]] = []

    class FakeDiscoveryExecutor:
        def __init__(self, **kwargs: Any) -> None:
            discoveries.append({"init": kwargs})

        def execute(self, job: Job, *, lease_owner: str, lease_seconds: int = 900):
            assert job.job_type == "discover_source"
            assert lease_owner == "worker-1"
            assert lease_seconds == 900

            class Result:
                def __init__(self) -> None:
                    self.job_id = job.id
                    self.workspace_id = job.workspace_id
                    self.source_id = job.source_id or ""
                    self.status = "succeeded"
                    self.discovered_videos = 2
                    self.enqueued_jobs = 2
                    self.video_ids = ("OEDoJyhQhXs", "abcdefghijk")
                    self.error_code = None
                    self.error_message = None

            return Result()

    monkeypatch.setattr("yutome.hosted.runtime.HostedSourceDiscoveryExecutor", FakeDiscoveryExecutor)
    runner = HostedCommandRunner(AppConfig(), connection=connection)

    result = runner.worker_once(lease_owner="worker-1", workspace_id="ws_cli")

    assert result.affected_rows == 1
    assert result.params["executions"][0]["status"] == "succeeded"
    assert result.params["executions"][0]["enqueued_jobs"] == 2
    assert discoveries[0]["init"]["connection"] is connection


def test_runner_can_seed_real_index_video_job_without_mock_plan() -> None:
    connection = RecordingConnection(rows=[{"id": "job_seeded"}])
    runner = HostedCommandRunner(AppConfig(), connection=connection)

    result = runner.enqueue_index_video(workspace_id="ws_cli", source_url="https://www.youtube.com/watch?v=OEDoJyhQhXs")

    assert result.job_id == "job_seeded"
    assert result.youtube_video_id == "OEDoJyhQhXs"
    assert [call[0].split()[2] for call in connection.calls[:3]] == ["workspaces", "sources", "jobs"]
    assert connection.calls[2][1]["metadata_json"]


def test_runner_can_seed_source_refresh_policy_without_mock_plan() -> None:
    connection = RecordingConnection(rows=[{"id": "srp_1"}])
    runner = HostedCommandRunner(AppConfig(), connection=connection)

    result = runner.source_add(workspace_id="ws_cli", source_url="leoandlongevity", cadence_seconds=1800, max_new_videos_per_run=3)

    assert result.source_type == "handle"
    assert result.refresh_policy_id is not None
    assert "INSERT INTO source_refresh_policies" in connection.calls[2][0]
    assert connection.calls[2][1]["cadence_seconds"] == 1800
    assert connection.calls[2][1]["max_new_videos_per_run"] == 3


def test_runner_billing_status_executes_debug_sql_and_returns_snapshot() -> None:
    created_at = datetime(2026, 5, 26, 4, 0, tzinfo=timezone.utc)
    connection = RecordingConnection(
        rows=[
            {
                "reservation_id": "res_1",
                "workspace_id": "ws_cli",
                "job_id": "job_1",
                "job_status": "retry_wait",
                "job_error_code": None,
                "job_error_message": None,
                "operation_id": "op_1",
                "job_operation": "voyage.embed_documents",
                "operation_status": "reserved",
                "video_id": "vid_1",
                "subject": "voyage",
                "operation": "embed_documents",
                "operation_key": "voyage.embed_documents",
                "allocation_id": "alloc_voyage",
                "credential_mode": "hosted",
                "reservation_status": "reserved",
                "decision_json": json.dumps({"allowed": True, "reason": "allowed"}),
                "estimated_units_json": json.dumps({"total_tokens": 100}),
                "idempotency_key": "idem_1",
                "created_at": created_at,
                "metadata_json": json.dumps({}),
                "usage_events_json": json.dumps(
                    [
                        {
                            "id": "evt_1",
                            "event_type": "provider_attempt_succeeded",
                            "status": "succeeded",
                            "actual_units": {"total_tokens": 91},
                            "error_code": None,
                            "provider_request_id": "req_1",
                            "created_at": created_at.isoformat(),
                            "metadata": {},
                        }
                    ]
                ),
                "meter_exports_json": json.dumps(
                    [
                        {
                            "id": "stripe:ws_cli:evt_1:credits",
                            "usage_event_id": "evt_1",
                            "replay_status": "failed",
                            "stripe_customer_id": "cus_cli",
                            "meter_unit": "credits",
                            "value": "0.1",
                            "stripe_meter_event_identifier": None,
                            "source_event_dedupe_key": "stripe:ws_cli:evt_1:credits",
                            "attempt_count": 2,
                            "last_error": {"code": "stripe_meter_event_failed"},
                            "exported_at": None,
                            "updated_at": created_at.isoformat(),
                        }
                    ]
                ),
            }
        ]
    )
    runner = HostedCommandRunner(AppConfig(), connection=connection)

    result = runner.billing_status(workspace_id="ws_cli", limit=3, operation="voyage.embed_documents")

    assert connection.calls[0][1] == {
        "workspace_id": "ws_cli",
        "operation": "voyage.embed_documents",
        "limit": 3,
    }
    assert "FROM usage_reservations AS reservation" in connection.calls[0][0]
    assert result["rows"][0]["entitlement_decision"]["allowed"] is True
    assert result["rows"][0]["usage_events"][0]["id"] == "evt_1"
    assert result["rows"][0]["meter_exports"][0]["replay_status"] == "failed"


def test_runner_stripe_meter_export_once_skips_without_claiming_when_key_missing(monkeypatch) -> None:
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    connection = RecordingConnection(rows=[])
    runner = HostedCommandRunner(AppConfig(), connection=connection)

    result = runner.stripe_meter_export_once(lease_owner="worker-1", limit=1)

    assert result.attempted is False
    assert result.affected_rows == 0
    assert result.skipped == 1
    assert result.secret_key_configured is False
    assert result.rows[0]["status"] == "skipped"
    assert connection.calls == []


def test_runner_stripe_meter_export_once_posts_and_marks_success(monkeypatch) -> None:
    created_at = datetime(2026, 5, 26, 4, 0, tzinfo=timezone.utc)
    posted: list[dict[str, Any]] = []
    connection = RecordingConnection(
        rows=[
            {
                "id": "stripe:ws_cli:evt_1:credits",
                "workspace_id": "ws_cli",
                "usage_event_id": "evt_1",
                "reservation_id": "res_1",
                "stripe_customer_id": "cus_cli",
                "meter_unit": "credits",
                "event_name": "yutome.credits",
                "value_text": "0.1",
                "source_event_dedupe_key": "stripe:ws_cli:evt_1:credits",
                "status": "processing",
                "stripe_meter_event_identifier": None,
                "event_timestamp": created_at,
                "attempt_count": 1,
                "metadata_json": json.dumps({"operation_key": "voyage.embed_documents"}),
            }
        ]
    )

    def fake_post(payload: dict[str, Any], *, secret_key: str, api_base: str, idempotency_key: str) -> dict[str, Any]:
        posted.append(
            {"payload": payload, "secret_key": secret_key, "api_base": api_base, "idempotency_key": idempotency_key}
        )
        return {"object": "billing.meter_event", "identifier": payload["identifier"]}

    monkeypatch.setattr("yutome.hosted.runtime._post_stripe_meter_event", fake_post)
    runner = HostedCommandRunner(AppConfig(), connection=connection)

    result = runner.stripe_meter_export_once(
        lease_owner="worker-1",
        limit=1,
        secret_key="sk_test_123",
        stripe_api_base="https://api.stripe.test",
    )

    assert result.succeeded == 1
    assert result.failed == 0
    assert posted[0]["secret_key"] == "sk_test_123"
    assert posted[0]["api_base"] == "https://api.stripe.test"
    assert posted[0]["idempotency_key"] == "stripe:ws_cli:evt_1:credits"
    assert posted[0]["payload"]["event_name"] == "yutome.credits"
    assert posted[0]["payload"]["payload"]["stripe_customer_id"] == "cus_cli"
    assert posted[0]["payload"]["payload"]["value"] == "0.1"
    # body identifier + persisted "what we sent" are the compact hash (Stripe's 100-char cap);
    # the HTTP Idempotency-Key header (asserted above) keeps the readable dedupe key.
    assert posted[0]["payload"]["identifier"].startswith("me_")
    assert len(posted[0]["payload"]["identifier"]) <= 100
    assert connection.calls[1][1]["status"] == "succeeded"
    assert connection.calls[1][1]["stripe_meter_event_identifier"].startswith("me_")


def test_runner_reconcile_balance_reads_inputs_and_upserts_snapshot() -> None:
    period_start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    connection = RecordingConnection(
        rows=[
            {
                "row_kind": "usage",
                "id": "evt_1",
                "workspace_id": "ws_cli",
                "actual_units_json": json.dumps({"total_tokens": "300"}),
            },
        ]
    )
    runner = HostedCommandRunner(AppConfig(), connection=connection)

    result = runner.reconcile_balance(
        workspace_id="ws_cli",
        entitlement_policy_id="policy_cli",
        period_start_at=period_start,
        period_end_at=period_end,
        starting_units={"total_tokens": 1_000},
    )

    assert result["remaining_units"] == {"total_tokens": 700}
    assert "FROM usage_events" in connection.calls[0][0]
    assert "credit_ledger_entries" not in connection.calls[0][0]
    assert "INSERT INTO workspace_balances" in connection.calls[1][0]


def test_runner_migrate_applies_usage_idempotency_constraints() -> None:
    connection = RecordingConnection()
    runner = HostedCommandRunner(AppConfig(), connection=connection)

    applied = runner.migrate(phase="phase1")
    sql = "\n".join(call[0] for call in connection.calls)

    assert applied == len(connection.calls)
    assert "CREATE TABLE IF NOT EXISTS usage_reservations" in sql
    assert "idx_usage_reservations_workspace_idempotency_key" in sql
    assert "idx_usage_events_provider_request_idempotency" in sql


def test_runner_exposes_postgres_usage_gate_and_ledger() -> None:
    class UsageAdapterConnection(RecordingConnection):
        def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            params = dict(params or {})
            self.calls.append((statement, params))
            if "FROM workspace_balances" in statement and "FOR UPDATE" in statement:
                return [
                    {
                        "workspace_id": "ws_alice",
                        "entitlement_policy_id": "policy",
                        "remaining_units_jsonb": {"total_tokens": 500},
                        "reserved_units_jsonb": {},
                        "unlimited_units": [],
                    }
                ]
            return []

    connection = UsageAdapterConnection()
    runner = HostedCommandRunner(AppConfig(), connection=connection)
    idempotency_key = "ws_alice:vid_123:gemini.cleanup_transcript:h_fake"

    reservation = runner.usage_gate().reserve(
        workspace_id="ws_alice",
        subject="gemini",
        operation="cleanup_transcript",
        estimated_units={"total_tokens": 100},
        allocation=ProviderAllocation(
            id="alloc_gemini",
            workspace_id="ws_alice",
            provider="gemini",
            operation="cleanup_transcript",
        ),
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={"gemini.cleanup_transcript"},
        ),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"total_tokens": 500}),
        idempotency_key=idempotency_key,
    )
    event = UsageEvent(
        reservation_id=reservation.id,
        workspace_id="ws_alice",
        subject="gemini",
        operation="cleanup_transcript",
        event_type="provider_attempt_succeeded",
        status="succeeded",
        provider_request_id="req_123",
        actual_units={"total_tokens": 91},
        metadata={"idempotency_key": idempotency_key},
    )

    persisted_event = runner.usage_ledger().append(event)

    assert reservation.id.startswith("res_")
    assert any("INSERT INTO usage_reservations" in sql for sql, _params in connection.calls)
    assert persisted_event.id.startswith("evt_")
    event_inserts = [params for sql, params in connection.calls if "INSERT INTO usage_events" in sql]
    assert event_inserts
    assert event_inserts[-1]["provider_request_id"] == "req_123"


def test_build_hosted_api_app_attaches_runtime_postgres_components(monkeypatch) -> None:
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    connection = RecordingConnection()
    runner = HostedCommandRunner(AppConfig(), connection=connection)

    api_app = build_hosted_api_app(runner)

    assert api_app.state.hosted_connection is connection
    assert api_app.state.hosted_search_store.connection is connection
    assert api_app.state.hosted_adapter.search_store is api_app.state.hosted_search_store
    assert api_app.state.hosted_adapter.gate.connection is connection
    assert api_app.state.hosted_adapter.ledger.connection is connection
    assert api_app.state.hosted_api_auth_required is True
    assert api_app.state.hosted_api_auth_configured is False


def test_build_hosted_api_app_enables_token_auth_from_env(monkeypatch) -> None:
    monkeypatch.setenv(TOKEN_ENV_VAR, "hosted-secret")
    connection = RecordingConnection()
    runner = HostedCommandRunner(AppConfig(), connection=connection)

    api_app = build_hosted_api_app(runner)

    assert api_app.state.hosted_api_auth_required is True
    assert api_app.state.hosted_api_auth_configured is True
