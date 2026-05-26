from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from yutome.cli import app
from yutome.config import AppConfig, HostedConfig, write_default_config
from yutome.hosted.http_api import TOKEN_ENV_VAR
from yutome.hosted.runtime import (
    HostedCommandRunner,
    HostedDbCheck,
    HostedIndexingSmokeResult,
    HostedTickResult,
    build_hosted_api_app,
    maintenance_tick_sql,
    mock_hosted_public_indexing_bootstrap_statements,
    mock_hosted_public_indexing_plan,
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
                    "allocation_kind": "hosted",
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
                    "billing_exports": [
                        {
                            "id": "bill_evt_denied",
                            "usage_event_id": "evt_denied",
                            "provider": "polar",
                            "replay_status": "skipped",
                            "external_customer_id": workspace_id,
                            "customer_id": None,
                            "external_meter_key": "ai_usage",
                            "external_event_id": None,
                            "source_event_dedupe_key": "polar:evt_denied:gemini.transcribe_media",
                            "attempt_count": 0,
                            "last_error": {},
                            "exported_at": None,
                            "updated_at": "2026-05-26T04:00:00Z",
                        }
                    ],
                }
            ],
        }

    def mock_indexing_smoke(
        self,
        *,
        workspace_id: str,
        migrate: bool = False,
        migration_phase: str = "hosted",
        query: str | None = None,
        limit: int = 3,
        source_url: str = "https://www.youtube.com/watch?v=OEDoJyhQhXs",
    ) -> HostedIndexingSmokeResult:
        self.calls.append(
            (
                "mock_indexing_smoke",
                {
                    "workspace_id": workspace_id,
                    "migrate": migrate,
                    "migration_phase": migration_phase,
                    "query": query,
                    "limit": limit,
                    "source_url": source_url,
                },
            )
        )
        return HostedIndexingSmokeResult(
            ok=True,
            migrated=migrate,
            migration_phase=migration_phase if migrate else None,  # type: ignore[arg-type]
            applied_migrations=7 if migrate else 0,
            workspace_id=workspace_id,
            source_id="src_mock",
            job_id="job_mock",
            youtube_video_id="OEDoJyhQhXs",
            hosted_video_id="vid_mock",
            transcript_version_id="tx_mock",
            query=query or "hosted indexing",
            operations_executed=12,
            operation_names=["videos.upsert", "search_store.replace_active_transcript"],
            rows=[{"chunk_id": "chunk_1", "score": 1.0}],
            usage={"operation": "hybrid_query"},
        )

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


def _config_path(tmp_path: Path) -> Path:
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    return config_path


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
    assert "FOR UPDATE SKIP LOCKED" in statement.sql
    assert "next_run_at <= %(now)s" in statement.sql
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


def test_runner_tick_methods_return_affected_rows() -> None:
    connection = RecordingConnection(rows=[{"id": "row_1"}, {"id": "row_2"}])
    runner = HostedCommandRunner(AppConfig(), connection=connection)

    source_result = runner.source_refresh_tick(lease_owner="worker-1", limit=2)
    maintenance_result = runner.maintenance_tick(limit=2)

    assert source_result.tick == "source_refresh_tick"
    assert source_result.affected_rows == 2
    assert maintenance_result.tick == "maintenance_tick"
    assert maintenance_result.affected_rows == 2
    assert len(connection.calls) == 2


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
                "allocation_kind": "hosted",
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
                "billing_exports_json": json.dumps(
                    [
                        {
                            "id": "bill_1",
                            "usage_event_id": "evt_1",
                            "provider": "polar",
                            "replay_status": "failed",
                            "external_customer_id": "ws_cli",
                            "customer_id": None,
                            "external_meter_key": "ai_usage",
                            "external_event_id": None,
                            "source_event_dedupe_key": "polar:evt_1:voyage.embed_documents",
                            "attempt_count": 2,
                            "last_error": {"code": "polar_unavailable"},
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
    assert result["rows"][0]["billing_exports"][0]["replay_status"] == "failed"


def test_mock_indexing_smoke_bootstraps_dependencies_and_executes_plan_before_query() -> None:
    connection = RecordingConnection(rows=[{"chunk_id": "chunk_1", "score": 1.0}])
    runner = HostedCommandRunner(AppConfig(), connection=connection)

    result = runner.mock_indexing_smoke(workspace_id="ws_cli", query="hosted indexing", limit=2)
    statements = [call[0] for call in connection.calls]

    assert result.ok is True
    assert result.migrated is False
    assert result.operations_executed == len(result.operation_names)
    assert result.rows == [{"chunk_id": "chunk_1", "score": 1.0}]
    assert result.usage["operation"] == "hybrid_query"
    assert statements[0].startswith("INSERT INTO workspaces")
    assert statements[1].startswith("INSERT INTO sources")
    assert statements[2].startswith("INSERT INTO jobs")
    assert "INSERT INTO videos" in statements[3]
    assert "INSERT INTO search_index_profiles" in statements[4]
    assert "FULL OUTER JOIN semantic USING (chunk_id)" in statements[-1]
    assert connection.calls[-1][1]["workspace_id"] == "ws_cli"


def test_mock_indexing_bootstrap_statements_cover_hosted_foreign_keys() -> None:
    plan = mock_hosted_public_indexing_plan(workspace_id="ws_cli")

    statements = mock_hosted_public_indexing_bootstrap_statements(plan.source, plan.job)
    sql = "\n".join(statement.sql for statement in statements)

    assert [statement.sql.split(" ", 2)[2].split()[0] for statement in statements] == [
        "workspaces",
        "sources",
        "jobs",
    ]
    assert "ON CONFLICT (id) DO UPDATE" in sql
    assert "ON CONFLICT (workspace_id, idempotency_key) DO UPDATE" in sql
    assert statements[1].params["id"] == plan.source.id
    assert statements[2].params["id"] == plan.job.id


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
    connection = RecordingConnection()
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
    assert "INSERT INTO usage_reservations" in connection.calls[0][0]
    assert persisted_event.id.startswith("evt_")
    assert "INSERT INTO usage_events" in connection.calls[-1][0]
    assert connection.calls[-1][1]["provider_request_id"] == "req_123"


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


def test_hosted_api_cli_command_runs_fake_app_server(monkeypatch, tmp_path: Path) -> None:
    config_path = _config_path(tmp_path)
    fake_app = object()
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_build_app(command_config_path: Path) -> object:
        calls.append(("build_app", {"config_path": command_config_path}))
        return fake_app

    def fake_run_app(api_app: object, *, host: str, port: int, log_level: str = "info") -> None:
        calls.append(
            (
                "run_app",
                {
                    "api_app": api_app,
                    "host": host,
                    "port": port,
                    "log_level": log_level,
                },
            )
        )

    monkeypatch.setattr("yutome.cli._hosted_api_app", fake_build_app)
    monkeypatch.setattr("yutome.cli._run_hosted_api_app", fake_run_app)

    result = CliRunner().invoke(
        app,
        [
            "hosted",
            "api",
            "--config",
            str(config_path),
            "--host",
            "0.0.0.0",
            "--port",
            "4321",
            "--log-level",
            "warning",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        ("build_app", {"config_path": config_path}),
        (
            "run_app",
            {
                "api_app": fake_app,
                "host": "0.0.0.0",
                "port": 4321,
                "log_level": "warning",
            },
        ),
    ]


def test_hosted_cli_commands_use_fake_runner_and_emit_json(monkeypatch, tmp_path: Path) -> None:
    config_path = _config_path(tmp_path)
    fake = FakeHostedRunner()
    monkeypatch.setattr("yutome.cli._hosted_runner", lambda _config_path: fake)
    runner = CliRunner()

    migrate = runner.invoke(app, ["hosted", "migrate", "--config", str(config_path), "--phase", "phase4", "--json"])
    db_check = runner.invoke(app, ["hosted", "db-check", "--config", str(config_path), "--json"])
    search = runner.invoke(
        app,
        ["hosted", "search-smoke", "vitamin d", "--config", str(config_path), "--limit", "4", "--json"],
    )
    billing = runner.invoke(
        app,
        [
            "hosted",
            "billing-status",
            "--config",
            str(config_path),
            "--workspace-id",
            "ws_cli",
            "--operation",
            "gemini.transcribe_media",
            "--limit",
            "5",
            "--json",
        ],
    )
    indexing = runner.invoke(
        app,
        [
            "hosted",
            "mock-indexing-smoke",
            "--config",
            str(config_path),
            "--workspace-id",
            "ws_cli",
            "--migrate",
            "--phase",
            "hosted",
            "--query",
            "hosted indexing",
            "--limit",
            "2",
            "--json",
        ],
    )
    worker = runner.invoke(
        app,
        [
            "hosted",
            "worker",
            "--config",
            str(config_path),
            "--once",
            "--lease-owner",
            "worker-1",
            "--workspace-id",
            "ws_cli",
            "--json",
        ],
    )
    source_tick = runner.invoke(
        app,
        ["hosted", "source-refresh-tick", "--config", str(config_path), "--lease-owner", "source-1", "--json"],
    )
    maintenance_tick = runner.invoke(app, ["hosted", "maintenance-tick", "--config", str(config_path), "--json"])

    assert migrate.exit_code == 0, migrate.output
    assert json.loads(migrate.output) == {"ok": True, "phase": "phase4", "applied": 7}
    assert db_check.exit_code == 0, db_check.output
    assert json.loads(db_check.output)["database_reachable"] is True
    assert search.exit_code == 0, search.output
    assert json.loads(search.output)["rows"][0]["chunk_id"] == "chunk_1"
    assert billing.exit_code == 0, billing.output
    billing_payload = json.loads(billing.output)
    assert billing_payload["rows"][0]["entitlement_decision"]["reason"] == "usage_limit_exceeded"
    assert billing_payload["rows"][0]["billing_exports"][0]["source_event_dedupe_key"] == "polar:evt_denied:gemini.transcribe_media"
    assert indexing.exit_code == 0, indexing.output
    indexing_payload = json.loads(indexing.output)
    assert indexing_payload["migrated"] is True
    assert indexing_payload["operations_executed"] == 12
    assert indexing_payload["rows"][0]["chunk_id"] == "chunk_1"
    assert worker.exit_code == 0, worker.output
    assert json.loads(worker.output)["tick"] == "worker_once"
    assert source_tick.exit_code == 0, source_tick.output
    assert json.loads(source_tick.output)["affected_rows"] == 2
    assert maintenance_tick.exit_code == 0, maintenance_tick.output
    assert json.loads(maintenance_tick.output)["affected_rows"] == 3
    assert ("search_smoke", {"workspace_id": "ws_default", "query": "vitamin d", "limit": 4}) in fake.calls
    assert (
        "billing_status",
        {"workspace_id": "ws_cli", "limit": 5, "operation": "gemini.transcribe_media"},
    ) in fake.calls
    assert ("worker_once", {"lease_owner": "worker-1", "limit": 1, "lease_seconds": 900, "workspace_id": "ws_cli"}) in fake.calls


def test_hosted_billing_status_human_output_explains_denied_work(monkeypatch, tmp_path: Path) -> None:
    config_path = _config_path(tmp_path)
    fake = FakeHostedRunner()
    monkeypatch.setattr("yutome.cli._hosted_runner", lambda _config_path: fake)

    result = CliRunner().invoke(app, ["hosted", "billing-status", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "Hosted billing/usage status: workspace=ws_default" in result.output
    assert "reservation=denied decision=denied:usage_limit_exceeded" in result.output
    assert "Fallback paused by policy." in result.output
    assert "usage_events:" in result.output
    assert "billing_exports:" in result.output
    assert "dedupe=polar:evt_denied:gemini.transcribe_media" in result.output


def test_hosted_db_check_cli_reports_missing_url_without_live_db(monkeypatch, tmp_path: Path) -> None:
    config_path = _config_path(tmp_path)
    monkeypatch.delenv("YUTOME_POSTGRES_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    result = CliRunner().invoke(app, ["hosted", "db-check", "--config", str(config_path), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["url_configured"] is False
    assert payload["error"] == "postgres_url_missing"
