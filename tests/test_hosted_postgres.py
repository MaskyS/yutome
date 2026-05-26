from __future__ import annotations

from collections.abc import Iterator
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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
from yutome.hosted.postgres import (
    apply_phase1_schema,
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
        _wait_for_postgres(dsn)
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
