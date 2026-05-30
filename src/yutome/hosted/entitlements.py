from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import Text, any_, bindparam, case, func, select
from sqlalchemy.dialects.postgresql import ARRAY

from yutome.hosted.allocations import resolve_allocation
from yutome.hosted.mcp_query import HostedMcpAuthContext, HostedMcpUsageContext
from yutome.hosted.models import (
    EntitlementPolicy,
    ProviderAllocation,
    ServiceAllocation,
    UnitQuantity,
    UsageSubject,
    WorkspaceBalance,
)
from yutome.hosted.repositories import SqlStatement
from yutome.hosted.schema import (
    entitlement_policies,
    provider_allocations,
    service_allocations,
    workspace_balances,
    workspaces,
)
from yutome.hosted.sqlalchemy_core import compile_postgres_statement


# Stripe subscription statuses that keep a workspace entitled to ingest + tool calls. The seat
# treats `trialing` exactly like `active`; everything else (past_due/canceled/none) combined
# with an elapsed trial window is trial-expiry read-only.
_ENTITLED_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing"})


class SqlConnection(Protocol):
    def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> Any:
        ...


@dataclass
class PostgresUsageContextProvider:
    """Load hosted UsageGate inputs from Postgres, failing closed when state is missing.

    A missing policy, balance, or allocation resolves to a deny rather than a permissive
    default, so an unconfigured or partially provisioned workspace cannot spend.
    """

    connection: SqlConnection
    search_store_backend: str = "postgres_vectorchord"

    def __call__(
        self,
        auth: HostedMcpAuthContext,
        operation: str,
        estimated_units: Mapping[str, UnitQuantity],
    ) -> HostedMcpUsageContext:
        return self.for_subject(auth=auth, subject="search_store", operation=operation, estimated_units=estimated_units)

    def voyage(
        self,
        auth: HostedMcpAuthContext,
        operation: str,
        estimated_units: Mapping[str, UnitQuantity],
    ) -> HostedMcpUsageContext:
        return self.for_subject(auth=auth, subject="voyage", operation=operation, estimated_units=estimated_units)

    def for_subject(
        self,
        *,
        auth: HostedMcpAuthContext,
        subject: UsageSubject,
        operation: str,
        estimated_units: Mapping[str, UnitQuantity],
    ) -> HostedMcpUsageContext:
        # Trial-expiry read-only: when the seat's trial has ended with no active/trialing
        # subscription, ingest and agent-facing MCP/API tool calls hard-deny (a deny policy
        # makes every operation_not_allowed). Dashboard BFF reads (auth.dashboard_read) are
        # exempt so the existing corpus stays readable. Fail closed: an unreadable/missing
        # workspace row is treated as expired rather than entitled.
        if not auth.dashboard_read and self._trial_expired(workspace_id=auth.workspace_id):
            return HostedMcpUsageContext(
                allocation=None,
                policy=_deny_policy(workspace_id=auth.workspace_id),
                balance=WorkspaceBalance(workspace_id=auth.workspace_id),
            )
        policy = self._active_policy(workspace_id=auth.workspace_id)
        allocation = self._allocation(workspace_id=auth.workspace_id, subject=subject, operation=operation)
        balance = self._active_balance(
            workspace_id=auth.workspace_id,
            entitlement_policy_id=policy.id if policy is not None else None,
        )
        return HostedMcpUsageContext(
            allocation=allocation,
            policy=policy or _deny_policy(workspace_id=auth.workspace_id),
            balance=balance or WorkspaceBalance(workspace_id=auth.workspace_id),
        )

    def _trial_expired(self, *, workspace_id: str) -> bool:
        """True when the workspace's trial has ended and it has no active/trialing subscription.

        Reads workspaces.subscription_status / trial_ends_at. A still-running trial
        (`trialing`) and an `active` subscription are entitled regardless of trial_ends_at. A
        missing row reads as expired (fail closed).
        """

        statement = workspace_subscription_state_sql(workspace_id=workspace_id)
        row = _one(self.connection.execute(statement.sql, statement.params))
        if row is None:
            return True
        subscription_status = str(row.get("subscription_status") or "none").strip().lower()
        if subscription_status in _ENTITLED_SUBSCRIPTION_STATUSES:
            return False
        trial_ends_at = row.get("trial_ends_at")
        if trial_ends_at is None:
            # No subscription and no trial window recorded: treat as expired (fail closed).
            return True
        return _coerce_datetime(trial_ends_at) <= _now_utc()

    def _allocation(self, *, workspace_id: str, subject: UsageSubject, operation: str) -> ProviderAllocation | ServiceAllocation | None:
        if subject == "search_store":
            statement = service_allocation_sql(workspace_id=workspace_id, service="search_store", operation=operation)
            rows = _rows_from_result(self.connection.execute(statement.sql, statement.params))
            allocations = [_service_allocation_from_row(row) for row in rows]
        else:
            statement = provider_allocation_sql(workspace_id=workspace_id, provider=subject, operation=operation)
            rows = _rows_from_result(self.connection.execute(statement.sql, statement.params))
            allocations = [_provider_allocation_from_row(row) for row in rows]
        return resolve_allocation(allocations, workspace_id=workspace_id, subject=subject, operation=operation).allocation

    def _active_policy(self, *, workspace_id: str) -> EntitlementPolicy | None:
        statement = active_policy_sql(workspace_id=workspace_id)
        row = _one(self.connection.execute(statement.sql, statement.params))
        if row is None:
            return None
        return EntitlementPolicy(
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            allowed_operations=set(_text_array(row.get("allowed_operations"))),
            hard_limits_by_operation=_json_mapping(row.get("hard_limits_jsonb")),
            soft_limits_by_operation=_json_mapping(row.get("soft_limits_jsonb")),
            requests_per_minute=_optional_int(row.get("requests_per_minute")),
        )

    def _active_balance(self, *, workspace_id: str, entitlement_policy_id: str | None) -> WorkspaceBalance | None:
        if entitlement_policy_id is None:
            return None
        statement = active_balance_sql(workspace_id=workspace_id, entitlement_policy_id=entitlement_policy_id)
        row = _one(self.connection.execute(statement.sql, statement.params))
        if row is None:
            return None
        return WorkspaceBalance(
            workspace_id=str(row["workspace_id"]),
            remaining_units=_json_mapping(row.get("remaining_units_jsonb")),
            unlimited_units=set(_text_array(row.get("unlimited_units"))),
        )


def provider_allocation_sql(*, workspace_id: str, provider: str, operation: str) -> SqlStatement:
    operations = [operation, "*"]
    workspace_param = bindparam("workspace_id", value=workspace_id)
    provider_param = bindparam("provider", value=provider)
    operation_param = bindparam("operation", value=operation)
    statement = (
        select(
            provider_allocations.c.id,
            provider_allocations.c.workspace_id,
            provider_allocations.c.provider,
            provider_allocations.c.operation,
            provider_allocations.c.credential_mode,
            provider_allocations.c.status,
            provider_allocations.c.model_or_plan,
            provider_allocations.c.external_allocation_id,
            provider_allocations.c.metadata_json,
        )
        .where(
            provider_allocations.c.workspace_id == workspace_param,
            provider_allocations.c.provider == provider_param,
            provider_allocations.c.operation == any_(bindparam("operations", value=operations, type_=ARRAY(Text))),
        )
        .order_by(
            case((provider_allocations.c.operation == operation_param, 0), else_=1),
            provider_allocations.c.id,
        )
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def service_allocation_sql(*, workspace_id: str, service: str, operation: str) -> SqlStatement:
    operations = [operation, "*"]
    workspace_param = bindparam("workspace_id", value=workspace_id)
    service_param = bindparam("service", value=service)
    operation_param = bindparam("operation", value=operation)
    statement = (
        select(
            service_allocations.c.id,
            service_allocations.c.workspace_id,
            service_allocations.c.service,
            service_allocations.c.operation,
            service_allocations.c.credential_mode,
            service_allocations.c.status,
            service_allocations.c.backend,
            service_allocations.c.index_profile_ref,
            service_allocations.c.metadata_json,
        )
        .where(
            service_allocations.c.workspace_id == workspace_param,
            service_allocations.c.service == service_param,
            service_allocations.c.operation == any_(bindparam("operations", value=operations, type_=ARRAY(Text))),
        )
        .order_by(
            case((service_allocations.c.operation == operation_param, 0), else_=1),
            service_allocations.c.id,
        )
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def workspace_subscription_state_sql(*, workspace_id: str) -> SqlStatement:
    workspace_param = bindparam("workspace_id", value=workspace_id)
    statement = select(
        workspaces.c.subscription_status,
        workspaces.c.trial_ends_at,
    ).where(workspaces.c.id == workspace_param)
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def active_policy_sql(*, workspace_id: str) -> SqlStatement:
    workspace_param = bindparam("workspace_id", value=workspace_id)
    statement = (
        select(
            entitlement_policies.c.id,
            entitlement_policies.c.workspace_id,
            entitlement_policies.c.allowed_operations,
            entitlement_policies.c.hard_limits_jsonb,
            entitlement_policies.c.soft_limits_jsonb,
        )
        .where(
            entitlement_policies.c.workspace_id == workspace_param,
            entitlement_policies.c.status == bindparam("status", value="active"),
        )
        .order_by(
            entitlement_policies.c.updated_at.desc(),
            entitlement_policies.c.created_at.desc(),
            entitlement_policies.c.id,
        )
        .limit(bindparam("limit", value=1))
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def active_balance_sql(*, workspace_id: str, entitlement_policy_id: str) -> SqlStatement:
    workspace_param = bindparam("workspace_id", value=workspace_id)
    statement = (
        select(
            workspace_balances.c.workspace_id,
            workspace_balances.c.entitlement_policy_id,
            workspace_balances.c.remaining_units_jsonb,
            workspace_balances.c.unlimited_units,
        )
        .where(
            workspace_balances.c.workspace_id == workspace_param,
            workspace_balances.c.entitlement_policy_id == bindparam(
                "entitlement_policy_id",
                value=entitlement_policy_id,
            ),
            workspace_balances.c.period_start_at <= func.now(),
            workspace_balances.c.period_end_at > func.now(),
        )
        .limit(bindparam("limit", value=1))
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def _deny_policy(*, workspace_id: str) -> EntitlementPolicy:
    return EntitlementPolicy(id=f"policy_{workspace_id}_missing", workspace_id=workspace_id)


def _provider_allocation_from_row(row: Mapping[str, Any]) -> ProviderAllocation:
    return ProviderAllocation(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        provider=str(row["provider"]),  # type: ignore[arg-type]
        operation=str(row["operation"]),
        mode=str(row.get("credential_mode") or "hosted"),  # type: ignore[arg-type]
        status=str(row.get("status") or "active"),  # type: ignore[arg-type]
        model_or_plan=_optional_str(row.get("model_or_plan")),
        external_allocation_id=_optional_str(row.get("external_allocation_id")),
        metadata=_json_mapping(row.get("metadata_json")),
    )


def _service_allocation_from_row(row: Mapping[str, Any]) -> ServiceAllocation:
    return ServiceAllocation(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        service=str(row["service"]),  # type: ignore[arg-type]
        operation=str(row["operation"]),
        mode=str(row.get("credential_mode") or "service_internal"),  # type: ignore[arg-type]
        status=str(row.get("status") or "active"),  # type: ignore[arg-type]
        backend=str(row.get("backend") or "postgres_vectorchord"),
        index_profile_ref=_optional_str(row.get("index_profile_ref")),
        metadata=_json_mapping(row.get("metadata_json")),
    )


def _one(result: Any) -> dict[str, Any] | None:
    rows = _rows_from_result(result)
    return rows[0] if rows else None


def _rows_from_result(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if hasattr(result, "mappings"):
        return [dict(row) for row in result.mappings()]
    if hasattr(result, "fetchall"):
        rows = result.fetchall()
    elif isinstance(result, Iterable) and not isinstance(result, (str, bytes, Mapping)):
        rows = list(result)
    else:
        return []
    return [dict(row) for row in rows]


def _json_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _text_array(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return [item.strip().strip('"') for item in stripped[1:-1].split(",") if item.strip()]
        return [item for item in stripped.replace(",", " ").split() if item]
    return [str(value)]


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text_value = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text_value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    raise TypeError(f"Cannot interpret trial_ends_at value of type {type(value).__name__}.")


__all__ = [
    "PostgresUsageContextProvider",
    "active_balance_sql",
    "active_policy_sql",
    "provider_allocation_sql",
    "service_allocation_sql",
    "workspace_subscription_state_sql",
]
