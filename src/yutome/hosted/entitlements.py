from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

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

    def _allocation(self, *, workspace_id: str, subject: UsageSubject, operation: str) -> ProviderAllocation | ServiceAllocation | None:
        if subject == "search_store":
            rows = _rows_from_result(
                self.connection.execute(
                    _SERVICE_ALLOCATION_SQL,
                    {
                        "workspace_id": workspace_id,
                        "service": "search_store",
                        "operations": [operation, "*"],
                        "operation": operation,
                    },
                )
            )
            allocations = [_service_allocation_from_row(row) for row in rows]
        else:
            rows = _rows_from_result(
                self.connection.execute(
                    _PROVIDER_ALLOCATION_SQL,
                    {
                        "workspace_id": workspace_id,
                        "provider": subject,
                        "operations": [operation, "*"],
                        "operation": operation,
                    },
                )
            )
            allocations = [_provider_allocation_from_row(row) for row in rows]
        return resolve_allocation(allocations, workspace_id=workspace_id, subject=subject, operation=operation).allocation

    def _active_policy(self, *, workspace_id: str) -> EntitlementPolicy | None:
        row = _one(
            self.connection.execute(
                _ACTIVE_POLICY_SQL,
                {"workspace_id": workspace_id},
            )
        )
        if row is None:
            return None
        return EntitlementPolicy(
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            allowed_operations=set(_text_array(row.get("allowed_operations"))),
            hard_limits_by_operation=_json_mapping(row.get("hard_limits_jsonb")),
            soft_limits_by_operation=_json_mapping(row.get("soft_limits_jsonb")),
        )

    def _active_balance(self, *, workspace_id: str, entitlement_policy_id: str | None) -> WorkspaceBalance | None:
        if entitlement_policy_id is None:
            return None
        row = _one(
            self.connection.execute(
                _ACTIVE_BALANCE_SQL,
                {"workspace_id": workspace_id, "entitlement_policy_id": entitlement_policy_id},
            )
        )
        if row is None:
            return None
        return WorkspaceBalance(
            workspace_id=str(row["workspace_id"]),
            remaining_units=_json_mapping(row.get("remaining_units_jsonb")),
            unlimited_units=set(_text_array(row.get("unlimited_units"))),
        )


_PROVIDER_ALLOCATION_SQL = """
SELECT
    id,
    workspace_id,
    provider,
    operation,
    credential_mode,
    status,
    model_or_plan,
    external_allocation_id,
    metadata_json
FROM provider_allocations
WHERE workspace_id = %(workspace_id)s
  AND provider = %(provider)s
  AND operation = ANY(%(operations)s::text[])
ORDER BY CASE WHEN operation = %(operation)s THEN 0 ELSE 1 END, id;
""".strip()


_SERVICE_ALLOCATION_SQL = """
SELECT
    id,
    workspace_id,
    service,
    operation,
    credential_mode,
    status,
    backend,
    index_profile_ref,
    metadata_json
FROM service_allocations
WHERE workspace_id = %(workspace_id)s
  AND service = %(service)s
  AND operation = ANY(%(operations)s::text[])
ORDER BY CASE WHEN operation = %(operation)s THEN 0 ELSE 1 END, id;
""".strip()


_ACTIVE_POLICY_SQL = """
SELECT
    id,
    workspace_id,
    allowed_operations,
    hard_limits_jsonb,
    soft_limits_jsonb
FROM entitlement_policies
WHERE workspace_id = %(workspace_id)s
  AND status = 'active'
ORDER BY updated_at DESC, created_at DESC, id
LIMIT 1;
""".strip()


_ACTIVE_BALANCE_SQL = """
SELECT
    workspace_id,
    entitlement_policy_id,
    remaining_units_jsonb,
    unlimited_units
FROM workspace_balances
WHERE workspace_id = %(workspace_id)s
  AND entitlement_policy_id = %(entitlement_policy_id)s
  AND period_start_at <= now()
  AND period_end_at > now()
LIMIT 1;
""".strip()


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


__all__ = ["PostgresUsageContextProvider"]
