"""Local dev server for the hosted FastAPI used by the web dashboard.

Serves the account read endpoints (/account/summary, /account/library,
/account/assistants) and /account/bootstrap against a local Postgres, with the
dev tokens/secret that match web/.dev.vars. Schema is applied idempotently on
startup using the same hosted migration bootstrap as the real hosted API.

Run (see web/scripts/local-dev.sh for the full stack):

    docker run -d --rm --name yutome-dev-pg -e POSTGRES_PASSWORD=postgres \\
        -p 55432:5432 tensorchord/vchord-suite:pg17-latest
    YUTOME_E2E_PG_DSN=postgresql://postgres:postgres@127.0.0.1:55432/postgres \\
        uv run --with uvicorn uvicorn dev_hosted_api:app --app-dir web/scripts --port 8000
"""

from __future__ import annotations

import os
import time

import psycopg
from psycopg.rows import dict_row

from yutome.config import AppConfig
from yutome.hosted.http_api import build_postgres_app
from yutome.hosted.runtime import HostedCommandRunner

DSN = os.environ.get("YUTOME_E2E_PG_DSN", "postgresql://postgres:postgres@127.0.0.1:55432/postgres")


def _apply_hosted_schema(connection: object) -> int:
    """Apply the same full hosted schema/migration path production uses."""
    return HostedCommandRunner(AppConfig(), connection=connection).migrate(phase="hosted")


def _wait_and_apply_schema(retries: int = 60) -> None:
    last: Exception | None = None
    for _ in range(retries):
        try:
            with psycopg.connect(DSN, autocommit=True) as conn:
                _apply_hosted_schema(conn)
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
