from __future__ import annotations

import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from yutome.hosted.indexing import complete_job_operation_success_sql
from yutome.hosted.jobs import claim_jobs_sql, update_job_operation_status_sql
from yutome.hosted.postgres import (
    apply_hosted_schema,
    apply_phase1_schema,
    hosted_schema_statements,
    phase1_schema_statements,
    phase4_schema_statements,
)
from yutome.hosted.search_store import extension_check_sql


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> object:
        self.statements.append(statement)
        return None


def test_phase1_schema_statements_include_core_usage_tables() -> None:
    statements = phase1_schema_statements()
    joined = "\n".join(statements)

    assert "CREATE TABLE IF NOT EXISTS workspaces" in joined
    assert "CREATE TABLE IF NOT EXISTS provider_allocations" in joined
    assert "CREATE TABLE IF NOT EXISTS service_allocations" in joined
    assert "CREATE TABLE IF NOT EXISTS usage_reservations" in joined
    assert "CREATE TABLE IF NOT EXISTS usage_events" in joined
    assert all(statement.endswith(";") for statement in statements)


def test_phase4_schema_statements_include_hosted_runtime_tables_and_extensions() -> None:
    statements = phase4_schema_statements()
    joined = "\n".join(statements)

    assert "CREATE EXTENSION IF NOT EXISTS vector" in joined
    assert "CREATE EXTENSION IF NOT EXISTS vchord" in joined
    assert "CREATE EXTENSION IF NOT EXISTS pg_tokenizer" in joined
    assert "CREATE EXTENSION IF NOT EXISTS vchord_bm25" in joined
    assert "create_tokenizer('yutome_llmlingua2'" in joined
    assert "create_tokenizer('pg_tokenizer'" in joined
    assert "Tokenizer already exists:%" in joined
    assert "CREATE TABLE IF NOT EXISTS sources" in joined
    assert "CREATE TABLE IF NOT EXISTS source_refresh_policies" in joined
    assert "CREATE TABLE IF NOT EXISTS jobs" in joined
    assert "CREATE TABLE IF NOT EXISTS job_operations" in joined
    assert "output_json jsonb NOT NULL DEFAULT '{}'::jsonb" in joined
    assert "ADD COLUMN IF NOT EXISTS output_json" in joined
    assert "CREATE TABLE IF NOT EXISTS search_index_profiles" in joined
    assert "embedding_model text NOT NULL DEFAULT 'voyage-4-lite'" in joined
    assert "embedding_dimension integer NOT NULL DEFAULT 1024" in joined
    assert "chk_search_index_profiles_embedding_dimension_supported" in joined
    assert "CHECK (embedding_dimension = 1024)" in joined
    assert "CREATE TABLE IF NOT EXISTS chunks" in joined
    assert "bm25_document bm25vector NOT NULL" in joined
    assert "tokenize(chunks.text, sip.tokenizer)::bm25vector" in joined
    assert "idx_chunks_bm25_document" in joined
    assert "ON chunks USING bm25 (bm25_document bm25_ops)" in joined
    assert "CREATE TABLE IF NOT EXISTS chunk_embeddings" in joined
    assert "embedding vector(1024) NOT NULL" in joined
    assert "chk_chunk_embeddings_embedding_dimension" in joined
    assert "CHECK (vector_dims(embedding) = 1024)" in joined
    assert "ON chunk_embeddings USING vchordrq (embedding vector_l2_ops)" in joined
    assert "active_transcript_version_id text" in joined
    assert "is_active boolean" not in joined
    assert "idx_active_transcript_per_video" not in joined
    assert "idx_videos_active_transcript" in joined
    assert "UNIQUE(workspace_id, idempotency_key)" in joined
    assert "idx_jobs_claimable" in joined
    assert "idx_source_refresh_due" in joined
    assert all(statement.endswith(";") for statement in statements)


def test_hosted_schema_combines_usage_and_runtime_tables_in_order() -> None:
    statements = hosted_schema_statements()
    joined = "\n".join(statements)

    assert joined.index("CREATE TABLE IF NOT EXISTS usage_reservations") < joined.index(
        "CREATE TABLE IF NOT EXISTS jobs"
    )
    assert joined.index("CREATE TABLE IF NOT EXISTS jobs") < joined.index(
        "CREATE TABLE IF NOT EXISTS chunk_embeddings"
    )
    assert joined.index("CREATE TABLE IF NOT EXISTS usage_events") < joined.index(
        "CREATE TABLE IF NOT EXISTS billing_exports"
    )
    assert "CREATE TABLE IF NOT EXISTS price_books" in joined
    assert "CREATE TABLE IF NOT EXISTS entitlement_policies" in joined
    assert "CREATE TABLE IF NOT EXISTS workspace_balances" in joined
    assert "CREATE TABLE IF NOT EXISTS billing_customers" in joined
    assert "CREATE TABLE IF NOT EXISTS polar_webhook_snapshots" in joined


def test_apply_phase1_schema_runs_statements_in_order() -> None:
    connection = RecordingConnection()

    applied = apply_phase1_schema(connection, statements=["CREATE TABLE one;", "CREATE TABLE two;"])

    assert applied == 2
    assert connection.statements == ["CREATE TABLE one;", "CREATE TABLE two;"]


def test_apply_hosted_schema_runs_all_statements() -> None:
    connection = RecordingConnection()

    applied = apply_hosted_schema(connection, statements=["CREATE EXTENSION vector;", "CREATE TABLE jobs;"])

    assert applied == 2
    assert connection.statements == ["CREATE EXTENSION vector;", "CREATE TABLE jobs;"]


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


def test_live_postgres_accepts_nullable_and_array_hosted_job_parameters() -> None:
    dsn = os.getenv("YUTOME_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set YUTOME_TEST_POSTGRES_DSN to validate psycopg/Postgres parameter inference")

    import psycopg

    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    connection = None
    last_error: BaseException | None = None
    for _ in range(20):
        try:
            connection = psycopg.connect(dsn, autocommit=False)
            break
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(0.25)
    if connection is None:
        raise AssertionError("could not connect to live Postgres test DSN") from last_error

    with connection:
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

            for statement in (
                claim_jobs_sql(
                    lease_owner="worker_1",
                    now=now,
                    lease_seconds=900,
                    limit=1,
                    job_types=["index_video"],
                    executor_kind=None,
                    executor_ref=None,
                ),
                update_job_operation_status_sql(
                    operation_id="op_1",
                    workspace_id="ws_1",
                    status="failed_retryable",
                    now=now,
                    usage_reservation_id=None,
                    job_id=None,
                    lease_owner=None,
                ),
                complete_job_operation_success_sql(
                    operation_id="op_1",
                    workspace_id="ws_1",
                    output={"ok": True},
                    now=now + timedelta(seconds=1),
                    usage_reservation_id=None,
                    job_id=None,
                    lease_owner=None,
                ),
                extension_check_sql(["vector", "vchord"]),
            ):
                cursor.execute(statement.sql, statement.params)

        connection.rollback()
