from __future__ import annotations

from collections.abc import Iterator
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from decimal import Decimal

import pytest

from yutome.hosted.billing import (
    BillingCustomer,
    BillingExportEvent,
    CreditLedgerEntry,
    EntitlementPolicyRecord,
    PolarWebhookSnapshot,
    PriceBook,
    WorkspaceBalanceSnapshot,
    upsert_billing_customer_sql,
    upsert_billing_export_sql,
    upsert_credit_ledger_entry_sql,
    upsert_entitlement_policy_sql,
    upsert_polar_webhook_snapshot_sql,
    upsert_price_book_sql,
    upsert_workspace_balance_sql,
)
from yutome.hosted.indexing import complete_job_operation_success_sql, enqueue_index_video_job_sql
from yutome.hosted.jobs import (
    active_job_lease_sql,
    claim_jobs_sql,
    job_repository_constraint_statements,
    release_job_lease_sql,
    renew_job_lease_sql,
    retry_job_sql,
    update_job_operation_status_sql,
)
from yutome.hosted.models import UsageDecision, UsageEvent, UsageReservation
from yutome.hosted.postgres import (
    apply_phase1_schema,
)
from yutome.hosted.repositories import (
    insert_usage_event_sql,
    update_usage_reservation_status_sql,
    usage_repository_constraint_statements,
    upsert_usage_reservation_sql,
)
from yutome.hosted.search_store import extension_check_sql


@pytest.fixture(scope="session")
def live_postgres_dsn() -> Iterator[str]:
    configured = os.getenv("YUTOME_TEST_POSTGRES_DSN")
    if configured:
        _wait_for_postgres(configured)
        yield configured
        return
    if not shutil.which("docker"):
        pytest.skip("set YUTOME_TEST_POSTGRES_DSN or install Docker/OrbStack for live Postgres validation")
    if subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        pytest.skip("Docker/OrbStack is not running")

    container_name = f"yutome-pg-test-{os.getpid()}"
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            container_name,
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-e",
            "POSTGRES_DB=yutome_test",
            "-p",
            "127.0.0.1::5432",
            "postgres:16-alpine",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    try:
        port = (
            subprocess.check_output(["docker", "port", container_name, "5432/tcp"], text=True)
            .strip()
            .rsplit(":", 1)[1]
        )
        dsn = f"postgresql://postgres:postgres@127.0.0.1:{port}/yutome_test"
        try:
            _wait_for_postgres(dsn)
        except AssertionError as exc:
            pytest.skip(f"auto-started Postgres test container did not become ready: {exc}")
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_for_postgres(dsn: str) -> None:
    import psycopg

    last_error: BaseException | None = None
    for _ in range(40):
        try:
            with psycopg.connect(dsn) as connection:
                connection.execute("SELECT 1;")
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(0.25)
    raise AssertionError("could not connect to live Postgres test DSN") from last_error


def test_hosted_sql_does_not_leave_postgres_params_untyped_in_ambiguous_contexts() -> None:
    hosted_dir = Path(__file__).parents[1] / "src" / "yutome" / "hosted"
    risky_patterns = {
        r"%\([^)]+\)s\s+IS\s+NULL": "cast nullable placeholders used in IS NULL, for example %(id)s::text IS NULL",
        r"COALESCE\s*\(\s*%\([^)]+\)s\s*,": "cast placeholders passed to COALESCE, for example COALESCE(%(id)s::text, id)",
        r"\b(?:ANY|ALL)\s*\(\s*%\([^)]+\)s\s*\)": "cast array placeholders, for example ANY(%(ids)s::text[])",
        r"\bIN\s+(?:\(\s*)?%\([^)]+\)s": "use = ANY(%(ids)s::<type>[]) instead of binding a list directly to IN",
    }
    findings: list[str] = []

    for path in sorted(hosted_dir.glob("*.py")):
        text = path.read_text()
        for pattern, guidance in risky_patterns.items():
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{path.relative_to(hosted_dir.parents[2])}:{line}: {match.group(0)!r}; {guidance}")

    assert findings == []


def test_phase1_schema_applies_to_fresh_postgres_schema(live_postgres_dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    schema = f"yutome_phase1_{os.getpid()}"
    with psycopg.connect(live_postgres_dsn, autocommit=True, row_factory=dict_row) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}";')
        try:
            connection.execute(f'SET search_path TO "{schema}";')
            applied = apply_phase1_schema(connection)

            rows = connection.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %(schema)s
                ORDER BY table_name;
                """,
                {"schema": schema},
            ).fetchall()
            tables = {row["table_name"] for row in rows}
            provider_columns = {
                row["column_name"]
                for row in connection.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = %(schema)s
                      AND table_name = 'provider_allocations';
                    """,
                    {"schema": schema},
                ).fetchall()
            }
            reservation_columns = {
                row["column_name"]
                for row in connection.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = %(schema)s
                      AND table_name = 'usage_reservations';
                    """,
                    {"schema": schema},
                ).fetchall()
            }

            assert applied > 0
            assert {
                "users",
                "workspaces",
                "provider_allocations",
                "service_allocations",
                "usage_reservations",
                "usage_events",
            } <= tables
            assert "credential_mode" in provider_columns
            assert "mode" not in provider_columns
            assert "credential_mode" in reservation_columns
        finally:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE;')


def test_account_jobs_query_returns_enriched_source_and_video_context(live_postgres_dsn: str) -> None:
    # The dashboard Activity feed needs each job row joined to its source's
    # display name/type and (for index_video) the video title. Exercise the
    # generated SQL against real Postgres so a wrong column/join/JSONB key fails
    # here rather than only in production. Minimal stand-in tables (matching the
    # columns the query reads) keep this free of the VectorChord extensions the
    # full indexing schema requires.
    import psycopg
    from psycopg.rows import dict_row

    from yutome.hosted.http_api import _account_jobs_sql

    schema = f"yutome_jobs_{os.getpid()}"
    workspace_id = "ws_jobs_test"
    with psycopg.connect(live_postgres_dsn, autocommit=True, row_factory=dict_row) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}";')
        try:
            connection.execute(f'SET search_path TO "{schema}";')
            connection.execute(
                """
                CREATE TABLE jobs (
                    id text PRIMARY KEY,
                    workspace_id text NOT NULL,
                    source_id text,
                    job_type text NOT NULL,
                    status text NOT NULL,
                    priority integer,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    started_at timestamptz,
                    finished_at timestamptz,
                    cancelled_at timestamptz,
                    error_code text,
                    error_message text,
                    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb
                );
                """
            )
            connection.execute(
                "CREATE TABLE sources (id text PRIMARY KEY, display_name text, source_type text, source_url text);"
            )
            connection.execute(
                "CREATE TABLE videos (workspace_id text NOT NULL, youtube_video_id text NOT NULL, title text NOT NULL DEFAULT '');"
            )
            connection.execute(
                "INSERT INTO sources (id, display_name, source_type, source_url) "
                "VALUES ('src_chan', '@chan', 'channel', 'https://www.youtube.com/@chan');"
            )
            connection.execute(
                "INSERT INTO videos (workspace_id, youtube_video_id, title) "
                "VALUES (%(ws)s, 'abcdEFGHijk', 'Indexed Title');",
                {"ws": workspace_id},
            )
            connection.execute(
                """
                INSERT INTO jobs (id, workspace_id, source_id, job_type, status, metadata_json)
                VALUES ('job_index', %(ws)s, 'src_chan', 'index_video', 'succeeded', %(meta)s::jsonb);
                """,
                {"ws": workspace_id, "meta": '{"youtube_video_id": "abcdEFGHijk"}'},
            )

            statement = _account_jobs_sql(workspace_id=workspace_id, limit=25)
            rows = connection.execute(statement.sql, statement.params).fetchall()

            assert len(rows) == 1
            row = rows[0]
            assert row["source_display_name"] == "@chan"
            assert row["source_type"] == "channel"
            assert row["source_url"] == "https://www.youtube.com/@chan"
            assert row["video_title"] == "Indexed Title"
        finally:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE;')


def test_live_postgres_executes_core_built_usage_repository_upserts(live_postgres_dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    with psycopg.connect(live_postgres_dsn, autocommit=False, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TEMP TABLE usage_reservations (
                    id text PRIMARY KEY,
                    workspace_id text NOT NULL,
                    subject text NOT NULL,
                    operation text NOT NULL,
                    allocation_id text,
                    credential_mode text NOT NULL,
                    estimated_units_json jsonb NOT NULL DEFAULT '{}'::jsonb,
                    idempotency_key text NOT NULL,
                    status text NOT NULL,
                    decision_json jsonb NOT NULL DEFAULT '{}'::jsonb,
                    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
                    created_at timestamptz NOT NULL DEFAULT now()
                ) ON COMMIT DROP;
                """
            )
            cursor.execute(
                """
                CREATE TEMP TABLE usage_events (
                    id text PRIMARY KEY,
                    reservation_id text,
                    workspace_id text NOT NULL,
                    subject text NOT NULL,
                    operation text NOT NULL,
                    event_type text NOT NULL,
                    status text NOT NULL,
                    actual_units_json jsonb NOT NULL DEFAULT '{}'::jsonb,
                    provider_request_id text,
                    error_code text,
                    raw_usage_json jsonb NOT NULL DEFAULT '{}'::jsonb,
                    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
                    created_at timestamptz NOT NULL DEFAULT now()
                ) ON COMMIT DROP;
                """
            )
            for statement in usage_repository_constraint_statements():
                cursor.execute(statement)

            reservation = UsageReservation(
                id="res_1",
                workspace_id="ws_1",
                subject="voyage",
                operation="embed_documents",
                allocation_id="alloc_voyage",
                credential_mode="hosted",
                estimated_units={"total_tokens": 2000, "vectors": 2},
                idempotency_key="idem_res_1",
                status="reserved",
                decision=UsageDecision(allowed=True),
                created_at=now,
                metadata={"job_id": "job_1"},
            )
            reservation_statement = upsert_usage_reservation_sql(reservation)
            first_reservation = cursor.execute(reservation_statement.sql, reservation_statement.params).fetchone()
            duplicate_reservation = cursor.execute(
                reservation_statement.sql,
                {
                    **reservation_statement.params,
                    "id": "res_other",
                    "estimated_units_json": '{"total_tokens":9999}',
                },
            ).fetchone()

            assert first_reservation["id"] == "res_1"
            assert first_reservation["estimated_units_json"] == {"total_tokens": 2000, "vectors": 2}
            assert duplicate_reservation["id"] == "res_1"
            assert duplicate_reservation["estimated_units_json"] == {"total_tokens": 2000, "vectors": 2}

            released_reservation_statement = update_usage_reservation_status_sql(
                reservation_id="res_1",
                workspace_id="ws_1",
                status="released",
            )
            released_reservation = cursor.execute(
                released_reservation_statement.sql,
                released_reservation_statement.params,
            ).fetchone()
            assert released_reservation["status"] == "released"

            event = UsageEvent(
                id="evt_1",
                reservation_id="res_1",
                workspace_id="ws_1",
                subject="voyage",
                operation="embed_documents",
                event_type="provider_attempt_succeeded",
                status="succeeded",
                actual_units={"total_tokens": 1900, "vectors": 2},
                provider_request_id="provider_req_1",
                raw_usage={"usage": {"total_tokens": 1900}},
                created_at=now,
            )
            event_statement = insert_usage_event_sql(event, idempotency="provider_request")
            first_event = cursor.execute(event_statement.sql, event_statement.params).fetchone()
            duplicate_event = cursor.execute(
                event_statement.sql,
                {**event_statement.params, "id": "evt_other", "actual_units_json": '{"total_tokens":1}'},
            ).fetchone()

            assert first_event["id"] == "evt_1"
            assert first_event["actual_units_json"] == {"total_tokens": 1900, "vectors": 2}
            assert duplicate_event["id"] == "evt_1"
            assert duplicate_event["actual_units_json"] == {"total_tokens": 1900, "vectors": 2}

        connection.rollback()


def test_live_postgres_executes_core_built_billing_upserts(live_postgres_dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    with psycopg.connect(live_postgres_dsn, autocommit=False, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE TEMP TABLE workspaces (id text PRIMARY KEY, name text NOT NULL) ON COMMIT DROP;")
            cursor.execute("CREATE TEMP TABLE usage_reservations (id text PRIMARY KEY) ON COMMIT DROP;")
            cursor.execute("CREATE TEMP TABLE usage_events (id text PRIMARY KEY) ON COMMIT DROP;")
            cursor.execute(
                """
                CREATE TEMP TABLE price_books (
                    id text PRIMARY KEY,
                    version text NOT NULL UNIQUE,
                    effective_at timestamptz,
                    currency text NOT NULL DEFAULT 'usd',
                    products_jsonb jsonb NOT NULL DEFAULT '[]'::jsonb,
                    unit_mapping_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                    status text NOT NULL DEFAULT 'draft',
                    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now()
                ) ON COMMIT DROP;
                """
            )
            cursor.execute(
                """
                CREATE TEMP TABLE entitlement_policies (
                    id text PRIMARY KEY,
                    workspace_id text NOT NULL,
                    plan_key text NOT NULL,
                    price_book_id text NOT NULL,
                    allowed_operations text[] NOT NULL DEFAULT ARRAY[]::text[],
                    included_units_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                    hard_limits_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                    soft_limits_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                    grace_policy_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                    status text NOT NULL DEFAULT 'active',
                    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    UNIQUE(workspace_id, plan_key, price_book_id)
                ) ON COMMIT DROP;
                """
            )
            cursor.execute(
                """
                CREATE TEMP TABLE workspace_balances (
                    workspace_id text PRIMARY KEY,
                    entitlement_policy_id text NOT NULL,
                    period_start_at timestamptz NOT NULL,
                    period_end_at timestamptz NOT NULL,
                    used_units_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                    reserved_units_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                    remaining_units_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                    unlimited_units text[] NOT NULL DEFAULT ARRAY[]::text[],
                    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
                    updated_at timestamptz NOT NULL DEFAULT now()
                ) ON COMMIT DROP;
                """
            )
            cursor.execute(
                """
                CREATE TEMP TABLE billing_customers (
                    id text PRIMARY KEY,
                    workspace_id text NOT NULL,
                    provider text NOT NULL,
                    external_customer_id text NOT NULL,
                    external_subscription_id text,
                    subscription_status_snapshot_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                    last_webhook_at timestamptz,
                    status text NOT NULL DEFAULT 'active',
                    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    UNIQUE(workspace_id, provider),
                    UNIQUE(provider, external_customer_id)
                ) ON COMMIT DROP;
                """
            )
            cursor.execute(
                """
                CREATE TEMP TABLE credit_ledger_entries (
                    id text PRIMARY KEY,
                    workspace_id text NOT NULL,
                    idempotency_key text NOT NULL,
                    provider text NOT NULL,
                    external_order_id text,
                    external_customer_id text,
                    direction text NOT NULL,
                    unit text NOT NULL,
                    quantity_text text NOT NULL,
                    reason text NOT NULL,
                    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
                    occurred_at timestamptz NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    UNIQUE(workspace_id, idempotency_key)
                ) ON COMMIT DROP;
                """
            )
            cursor.execute(
                """
                CREATE TEMP TABLE billing_exports (
                    id text PRIMARY KEY,
                    workspace_id text NOT NULL,
                    usage_event_id text NOT NULL,
                    reservation_id text,
                    billing_customer_id text,
                    price_book_id text,
                    provider text NOT NULL,
                    external_customer_id text,
                    customer_id text,
                    external_meter_key text,
                    external_event_id text,
                    event_name text NOT NULL,
                    export_units_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                    source_event_dedupe_key text NOT NULL,
                    status text NOT NULL DEFAULT 'pending',
                    authorization_effect text NOT NULL DEFAULT 'none',
                    attempt_count integer NOT NULL DEFAULT 0,
                    last_error_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
                    event_timestamp timestamptz NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    exported_at timestamptz,
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    UNIQUE(provider, source_event_dedupe_key),
                    UNIQUE(provider, external_event_id)
                ) ON COMMIT DROP;
                """
            )
            cursor.execute(
                """
                CREATE TEMP TABLE polar_webhook_snapshots (
                    id text PRIMARY KEY,
                    webhook_event_id text UNIQUE,
                    payload_hash text NOT NULL,
                    event_type text NOT NULL,
                    workspace_id text,
                    external_customer_id text,
                    external_subscription_id text,
                    customer_state_snapshot_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                    payload_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                    replay_status text NOT NULL DEFAULT 'pending',
                    received_at timestamptz NOT NULL,
                    processed_at timestamptz,
                    last_error_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
                    created_at timestamptz NOT NULL DEFAULT now()
                ) ON COMMIT DROP;
                """
            )

            cursor.execute("INSERT INTO workspaces (id, name) VALUES ('ws_1', 'Workspace');")
            cursor.execute("INSERT INTO usage_reservations (id) VALUES ('res_1');")
            cursor.execute("INSERT INTO usage_events (id) VALUES ('evt_1');")

            price_book_statement = upsert_price_book_sql(PriceBook(id="pb_1", version="starter-v1", status="active"))
            price_book = cursor.execute(price_book_statement.sql, price_book_statement.params).fetchone()
            assert price_book["version"] == "starter-v1"

            policy = EntitlementPolicyRecord(
                id="pol_1",
                workspace_id="ws_1",
                plan_key="starter",
                price_book_id="pb_1",
                allowed_operations=("voyage.embed_documents",),
                hard_limits={"voyage.embed_documents": {"vectors": 10}},
            )
            policy_statement = upsert_entitlement_policy_sql(policy)
            policy_row = cursor.execute(policy_statement.sql, policy_statement.params).fetchone()
            assert policy_row["hard_limits_jsonb"]["voyage.embed_documents"]["vectors"] == 10

            balance_statement = upsert_workspace_balance_sql(
                WorkspaceBalanceSnapshot(
                    workspace_id="ws_1",
                    entitlement_policy_id="pol_1",
                    period_start_at=now,
                    period_end_at=now + timedelta(days=30),
                    remaining_units={"vectors": 10},
                    updated_at=now,
                )
            )
            assert cursor.execute(balance_statement.sql, balance_statement.params).fetchone()["remaining_units_jsonb"] == {
                "vectors": 10
            }

            customer_statement = upsert_billing_customer_sql(
                BillingCustomer(id="bc_1", workspace_id="ws_1", external_customer_id="cus_1", last_webhook_at=now)
            )
            assert cursor.execute(customer_statement.sql, customer_statement.params).fetchone()["external_customer_id"] == "cus_1"

            credit_statement = upsert_credit_ledger_entry_sql(
                CreditLedgerEntry(
                    id="cred_1",
                    workspace_id="ws_1",
                    idempotency_key="order_1:vectors:0",
                    unit="vectors",
                    quantity=Decimal("10"),
                    external_order_id="order_1",
                    reason="order_grant",
                    occurred_at=now,
                )
            )
            assert cursor.execute(credit_statement.sql, credit_statement.params).fetchone()["quantity_text"] == "10"

            export_statement = upsert_billing_export_sql(
                BillingExportEvent(
                    idempotency_key="bill_1",
                    usage_event_id="evt_1",
                    reservation_id="res_1",
                    workspace_id="ws_1",
                    operation_key="voyage.embed_documents",
                    event_name="yutome.voyage.embed_documents",
                    actual_units={"vectors": 2},
                    timestamp=now,
                    external_customer_id="cus_1",
                )
            )
            assert cursor.execute(export_statement.sql, export_statement.params).fetchone()["export_units_jsonb"] == {"vectors": 2}

            snapshot_statement = upsert_polar_webhook_snapshot_sql(
                PolarWebhookSnapshot(
                    id="snap_1",
                    webhook_event_id="evt_polar_1",
                    payload_hash="sha256:abc",
                    event_type="order.paid",
                    workspace_id="ws_1",
                    payload={"id": "evt_polar_1"},
                    received_at=now,
                )
            )
            assert cursor.execute(snapshot_statement.sql, snapshot_statement.params).fetchone()["payload_hash"] == "sha256:abc"

        connection.rollback()


def test_live_postgres_executes_hosted_job_sql_with_lease_and_type_guards(live_postgres_dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    with psycopg.connect(live_postgres_dsn, autocommit=False, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TEMP TABLE jobs (
                    id text PRIMARY KEY,
                    workspace_id text NOT NULL,
                    source_id text,
                    job_type text NOT NULL,
                    status text NOT NULL,
                    priority integer NOT NULL DEFAULT 100,
                    idempotency_key text NOT NULL DEFAULT 'idem',
                    run_after timestamptz,
                    executor_kind text,
                    executor_ref text,
                    lease_owner text,
                    leased_at timestamptz,
                    lease_expires_at timestamptz,
                    retry_after timestamptz,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    started_at timestamptz,
                    finished_at timestamptz,
                    cancelled_at timestamptz,
                    error_code text,
                    error_message text,
                    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb
                ) ON COMMIT DROP;
                """
            )
            cursor.execute(
                """
                CREATE TEMP TABLE job_operations (
                    id text PRIMARY KEY,
                    workspace_id text NOT NULL,
                    job_id text NOT NULL,
                    operation text NOT NULL DEFAULT 'gemini.cleanup_transcript',
                    source_id text,
                    video_id text,
                    input_hash text NOT NULL DEFAULT 'hash',
                    idempotency_key text NOT NULL DEFAULT 'op-idem',
                    status text NOT NULL DEFAULT 'planned',
                    attempt_count integer NOT NULL DEFAULT 0,
                    usage_reservation_id text,
                    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
                    output_json jsonb NOT NULL DEFAULT '{}'::jsonb,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now()
                ) ON COMMIT DROP;
                """
            )
            for statement in job_repository_constraint_statements():
                cursor.execute(statement)
            cursor.execute(
                """
                INSERT INTO jobs (
                    id, workspace_id, job_type, status, priority, idempotency_key,
                    lease_owner, lease_expires_at, created_at
                )
                VALUES ('job_1', 'ws_1', 'index_video', 'queued', 10, 'idem_1', NULL, NULL, %(now)s);
                """,
                {"now": now},
            )
            cursor.execute(
                """
                INSERT INTO job_operations (id, workspace_id, job_id, operation, idempotency_key)
                VALUES ('op_1', 'ws_1', 'job_1', 'gemini.cleanup_transcript', 'op_idem_1');
                """
            )

            claim = claim_jobs_sql(
                lease_owner="worker_1",
                now=now,
                lease_seconds=900,
                limit=1,
                workspace_id="ws_1",
                job_types=["index_video"],
                executor_kind=None,
                executor_ref=None,
            )
            claimed = cursor.execute(claim.sql, claim.params).fetchall()
            assert [row["id"] for row in claimed] == ["job_1"]
            assert claimed[0]["lease_owner"] == "worker_1"
            assert claimed[0]["executor_kind"] is None

            active = active_job_lease_sql(job_id="job_1", lease_owner="worker_1", now=now + timedelta(seconds=1))
            assert cursor.execute(active.sql, active.params).fetchone()["id"] == "job_1"

            renewed = renew_job_lease_sql(
                job_id="job_1",
                lease_owner="worker_1",
                now=now + timedelta(seconds=1),
                lease_seconds=300,
            )
            assert cursor.execute(renewed.sql, renewed.params).fetchone()["lease_expires_at"] == now + timedelta(
                seconds=301
            )

            started = update_job_operation_status_sql(
                operation_id="op_1",
                workspace_id="ws_1",
                status="started",
                now=now + timedelta(seconds=2),
                usage_reservation_id=None,
                job_id="job_1",
                lease_owner="worker_1",
            )
            started_row = cursor.execute(started.sql, started.params).fetchone()
            assert started_row["status"] == "started"
            assert started_row["attempt_count"] == 1

            completed = complete_job_operation_success_sql(
                operation_id="op_1",
                workspace_id="ws_1",
                output={"ok": True},
                now=now + timedelta(seconds=3),
                usage_reservation_id=None,
                job_id="job_1",
                lease_owner="worker_1",
            )
            completed_row = cursor.execute(completed.sql, completed.params).fetchone()
            assert completed_row["status"] == "succeeded"
            assert completed_row["output_json"] == {"ok": True}

            cursor.execute(
                """
                INSERT INTO jobs (
                    id, workspace_id, job_type, status, priority, idempotency_key,
                    lease_owner, lease_expires_at, created_at
                )
                VALUES ('job_expired', 'ws_1', 'index_video', 'queued', 20, 'idem_expired',
                        'worker_1', %(expired_at)s, %(now)s);
                """,
                {"expired_at": now - timedelta(seconds=1), "now": now},
            )
            cursor.execute(
                """
                INSERT INTO job_operations (id, workspace_id, job_id, operation, idempotency_key)
                VALUES ('op_expired', 'ws_1', 'job_expired', 'gemini.cleanup_transcript', 'op_idem_expired');
                """,
            )
            expired_guard = update_job_operation_status_sql(
                operation_id="op_expired",
                workspace_id="ws_1",
                status="started",
                now=now,
                job_id="job_expired",
                lease_owner="worker_1",
            )
            assert cursor.execute(expired_guard.sql, expired_guard.params).fetchall() == []

            cursor.execute("UPDATE jobs SET status = 'succeeded' WHERE id = 'job_1';")
            enqueue = enqueue_index_video_job_sql(
                workspace_id="ws_1",
                source_id="src_1",
                video_id="OEDoJyhQhXs",
                priority=5,
                now=now + timedelta(seconds=5),
                metadata={"seeded_by": "test"},
            )
            cursor.execute(
                """
                INSERT INTO jobs (
                    id, workspace_id, source_id, job_type, status, priority,
                    idempotency_key, created_at, metadata_json
                )
                VALUES ('job_terminal', 'ws_1', 'src_1', 'index_video', 'succeeded', 50,
                        %(idempotency_key)s, %(now)s, '{"previous":true}'::jsonb);
                """,
                {"idempotency_key": enqueue.params["idempotency_key"], "now": now},
            )
            enqueued_terminal = cursor.execute(enqueue.sql, enqueue.params).fetchone()
            assert enqueued_terminal["status"] == "succeeded"
            assert enqueued_terminal["priority"] == 5
            assert enqueued_terminal["metadata_json"]["previous"] is True
            assert enqueued_terminal["metadata_json"]["youtube_video_id"] == "OEDoJyhQhXs"

            retry = retry_job_sql(
                job_id="job_1",
                lease_owner="worker_1",
                now=now + timedelta(seconds=4),
                retry_after=now + timedelta(minutes=5),
                error_code="ignored",
                error_message="terminal jobs stay terminal",
            )
            assert cursor.execute(retry.sql, retry.params).fetchall() == []

            release = release_job_lease_sql(job_id="job_1", lease_owner="worker_1")
            cursor.execute(release.sql, release.params)
            released = cursor.execute("SELECT lease_owner, leased_at, lease_expires_at FROM jobs WHERE id = 'job_1';").fetchone()
            assert released == {"lease_owner": None, "leased_at": None, "lease_expires_at": None}

            extension_check = extension_check_sql(["vector", "vchord"])
            assert cursor.execute(extension_check.sql, extension_check.params).fetchall() == []

            with pytest.raises(psycopg.errors.UniqueViolation):
                cursor.execute(
                    """
                    INSERT INTO jobs (id, workspace_id, job_type, status, priority, idempotency_key, created_at)
                    VALUES ('job_duplicate', 'ws_1', 'index_video', 'queued', 30, 'idem_1', %(now)s);
                    """,
                    {"now": now},
                )

        connection.rollback()
