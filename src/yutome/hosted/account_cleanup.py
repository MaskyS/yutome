"""Guarded hosted workspace cleanup.

This module owns the authoritative children-first delete order for workspace
purges. Any new workspace-scoped table added to ``schema.py`` must be added here
before the parent ``workspaces`` row can be deleted safely.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import Table, bindparam, delete, select

from yutome.hosted.repositories import SqlStatement
from yutome.hosted.schema import (
    account_grants,
    account_sessions,
    api_keys,
    chunk_embeddings,
    chunks,
    entitlement_policies,
    job_operations,
    jobs,
    provider_allocations,
    search_index_profiles,
    service_allocations,
    source_refresh_policies,
    sources,
    stripe_customers,
    stripe_meter_exports,
    stripe_webhook_events,
    transcript_versions,
    usage_events,
    usage_reservations,
    videos,
    workspace_balances,
    workspace_members,
    workspaces,
    youtube_grants,
)
from yutome.hosted.sqlalchemy_core import compile_postgres_statement


SAFE_SYNTHETIC_SUBSCRIPTION_STATUSES = frozenset({"trialing", "none", "canceled", "incomplete_expired"})

WORKSPACE_CHILD_TABLES_DELETE_ORDER: tuple[Table, ...] = (
    chunk_embeddings,
    chunks,
    transcript_versions,
    videos,
    job_operations,
    jobs,
    source_refresh_policies,
    stripe_meter_exports,
    usage_events,
    usage_reservations,
    sources,
    search_index_profiles,
    account_grants,
    account_sessions,
    api_keys,
    youtube_grants,
    stripe_customers,
    stripe_webhook_events,
    workspace_balances,
    entitlement_policies,
    provider_allocations,
    service_allocations,
    workspace_members,
)


class SqlConnection(Protocol):
    def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> Any:
        ...


class AccountCleanupError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class AccountCleanupResult:
    workspace_id: str
    deleted: bool
    deleted_tables: tuple[str, ...]


def synthetic_workspace_guard(workspace_row: Mapping[str, Any] | None, *, allow_paid: bool = False) -> None:
    """Fail closed unless the workspace row is structurally safe to purge."""

    if not workspace_row:
        raise AccountCleanupError(
            "workspace_not_found",
            "Hosted workspace was not found.",
            status_code=404,
        )

    # Personal smoke workspaces use the deterministic ws_ prefix; reject other account shapes.
    workspace_id = str(workspace_row.get("id") or "")
    if not workspace_id.startswith("ws_"):
        raise AccountCleanupError(
            "workspace_id_invalid",
            "Hosted workspace cleanup only accepts ws_-prefixed workspace ids.",
            status_code=400,
        )

    # Paid, past-due, or unpaid subscription states are real accounts unless explicitly overridden.
    subscription_status = str(workspace_row.get("subscription_status") or "none").strip().lower()
    if not allow_paid and subscription_status not in SAFE_SYNTHETIC_SUBSCRIPTION_STATUSES:
        raise AccountCleanupError(
            "workspace_not_synthetic",
            "Hosted workspace cleanup refuses workspaces with active or billable subscription state.",
            status_code=409,
        )


def workspace_cleanup_statements(workspace_id: str) -> list[SqlStatement]:
    statements = [_delete_by_workspace_sql(table, workspace_id=workspace_id) for table in WORKSPACE_CHILD_TABLES_DELETE_ORDER]
    statements.append(_delete_workspace_sql(workspace_id=workspace_id))
    return statements


def delete_synthetic_workspace(
    connection: SqlConnection,
    *,
    workspace_id: str,
    allow_paid: bool = False,
) -> AccountCleanupResult:
    workspace_row = _load_workspace_row(connection, workspace_id=workspace_id)
    synthetic_workspace_guard(workspace_row, allow_paid=allow_paid)
    stripe_customer_row = _load_stripe_customer_for_guard(connection, workspace_id=workspace_id)
    _stripe_customer_guard(stripe_customer_row, allow_paid=allow_paid)

    statements = workspace_cleanup_statements(workspace_id)
    _execute_cleanup_statements(connection, statements)
    return AccountCleanupResult(
        workspace_id=workspace_id,
        deleted=True,
        deleted_tables=tuple(table.name for table in (*WORKSPACE_CHILD_TABLES_DELETE_ORDER, workspaces)),
    )


def _load_workspace_row(connection: SqlConnection, *, workspace_id: str) -> dict[str, Any] | None:
    statement = (
        select(
            workspaces.c.id,
            workspaces.c.name,
            workspaces.c.status,
            workspaces.c.subscription_status,
            workspaces.c.owner_user_id,
        )
        .where(workspaces.c.id == bindparam("workspace_id", value=workspace_id))
        .limit(bindparam("limit", value=1))
    )
    sql, params = compile_postgres_statement(statement)
    rows = _rows_from_result(connection.execute(sql + ";", params))
    return rows[0] if rows else None


def _load_stripe_customer_for_guard(connection: SqlConnection, *, workspace_id: str) -> dict[str, Any] | None:
    statement = (
        select(
            stripe_customers.c.workspace_id,
            stripe_customers.c.stripe_subscription_id,
            stripe_customers.c.subscription_status,
        )
        .where(stripe_customers.c.workspace_id == bindparam("workspace_id", value=workspace_id))
        .limit(bindparam("limit", value=1))
    )
    sql, params = compile_postgres_statement(statement)
    rows = _rows_from_result(connection.execute(sql + ";", params))
    return rows[0] if rows else None


def _stripe_customer_guard(stripe_customer_row: Mapping[str, Any] | None, *, allow_paid: bool = False) -> None:
    if not stripe_customer_row or allow_paid:
        return

    # A mirrored Stripe subscription id means this workspace has subscribed before.
    stripe_subscription_id = str(stripe_customer_row.get("stripe_subscription_id") or "").strip()
    subscription_status = str(stripe_customer_row.get("subscription_status") or "none").strip().lower()
    if stripe_subscription_id or subscription_status == "active":
        raise AccountCleanupError(
            "workspace_not_synthetic",
            "Hosted workspace cleanup refuses workspaces with Stripe subscription history.",
            status_code=409,
        )


def _delete_by_workspace_sql(table: Table, *, workspace_id: str) -> SqlStatement:
    statement = delete(table).where(table.c.workspace_id == bindparam("workspace_id", value=workspace_id))
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def _delete_workspace_sql(*, workspace_id: str) -> SqlStatement:
    statement = delete(workspaces).where(workspaces.c.id == bindparam("workspace_id", value=workspace_id))
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def _execute_cleanup_statements(connection: SqlConnection, statements: list[SqlStatement]) -> None:
    transaction = getattr(connection, "transaction", None)
    if callable(transaction):
        with transaction():
            for statement in statements:
                connection.execute(statement.sql, statement.params)
        return
    for statement in statements:
        connection.execute(statement.sql, statement.params)


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


__all__ = [
    "AccountCleanupError",
    "AccountCleanupResult",
    "WORKSPACE_CHILD_TABLES_DELETE_ORDER",
    "delete_synthetic_workspace",
    "synthetic_workspace_guard",
    "workspace_cleanup_statements",
]
