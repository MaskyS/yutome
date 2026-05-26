"""Local dev server for the hosted FastAPI used by the web dashboard.

Serves the account read endpoints (/account/summary, /account/library,
/account/assistants) and /account/bootstrap against a local Postgres, with the
dev tokens/secret that match web/.dev.vars. Schema is applied idempotently on
startup (VectorChord-only tables are skipped — the dashboard reads don't need
them, so a plain `postgres` image works).

Run (see web/scripts/local-dev.sh for the full stack):

    docker run -d --rm --name yutome-dev-pg -e POSTGRES_PASSWORD=postgres \\
        -p 55432:5432 postgres:16
    YUTOME_E2E_PG_DSN=postgresql://postgres:postgres@127.0.0.1:55432/postgres \\
        uv run --with uvicorn uvicorn dev_hosted_api:app --app-dir web/scripts --port 8000
"""

from __future__ import annotations

import os
import time

import psycopg
from psycopg.rows import dict_row

from yutome.hosted.billing import billing_schema_statements
from yutome.hosted.http_api import build_postgres_app
from yutome.hosted.postgres import phase1_schema_statements, phase4_schema_statements

DSN = os.environ.get("YUTOME_E2E_PG_DSN", "postgresql://postgres:postgres@127.0.0.1:55432/postgres")

# Tables whose DDL needs the VectorChord/pg_tokenizer extensions; the dashboard
# read endpoints never touch them, so skip them to run on a plain Postgres.
_VECTORCHORD = (
    "EXTENSION", "create_tokenizer", "bm25", "vchord", "vector(", "tokenize(",
    "chunk", "search_index_profiles", "transcript_versions", "fts_document", "$yutome$",
)


def _wait_and_apply_schema(retries: int = 60) -> None:
    last: Exception | None = None
    for _ in range(retries):
        try:
            with psycopg.connect(DSN, autocommit=True) as conn:
                statements = (
                    phase1_schema_statements()
                    + billing_schema_statements()
                    + [s for s in phase4_schema_statements() if not any(t in s for t in _VECTORCHORD)]
                )
                for statement in statements:
                    try:
                        conn.execute(statement)
                    except psycopg.Error as exc:  # tolerate re-apply on a persistent DB
                        if "already exists" not in str(exc).lower():
                            raise
            return
        except psycopg.OperationalError as exc:  # Postgres not ready yet
            last = exc
            time.sleep(1)
    raise RuntimeError(f"Postgres at {DSN} never became ready") from last


class PerCallConnection:
    """A fresh connection per execute so parallel React Router loaders are safe."""

    def execute(self, sql: str, params: dict | None = None):
        with psycopg.connect(DSN, row_factory=dict_row, autocommit=True) as conn:
            cur = conn.execute(sql, params)
            try:
                return cur.fetchall()
            except psycopg.ProgrammingError:
                return []


_wait_and_apply_schema()

app = build_postgres_app(
    connection=PerCallConnection(),
    expected_api_token="dev-mcp-token",
    expected_account_api_token="dev-dashboard-token",
    account_session_secret="dev-account-session-secret",
    account_session_audience="yutome:hosted-oauth",
)
