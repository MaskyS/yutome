from __future__ import annotations

from yutome.hosted.models import (
    EntitlementPolicy,
    ProviderAllocation,
    ServiceAllocation,
    UsageDecision,
    UsageReservation,
    WorkspaceBalance,
)


Allocation = ProviderAllocation | ServiceAllocation


class UsageGate:
    """Small preflight gate for hosted provider and search-store operations."""

    def reserve(
        self,
        *,
        workspace_id: str,
        subject: str,
        operation: str,
        estimated_units: dict[str, float],
        allocation: Allocation | None,
        policy: EntitlementPolicy,
        balance: WorkspaceBalance,
        idempotency_key: str,
    ) -> UsageReservation:
        operation_key = f"{subject}.{operation}"
        decision = self._decide(
            workspace_id=workspace_id,
            operation_key=operation_key,
            estimated_units=estimated_units,
            allocation=allocation,
            policy=policy,
            balance=balance,
        )
        return UsageReservation(
            workspace_id=workspace_id,
            subject=subject,  # type: ignore[arg-type]
            operation=operation,
            allocation_id=allocation.id if allocation else None,
            allocation_kind=allocation.mode if allocation else "disabled",
            estimated_units=estimated_units,
            idempotency_key=idempotency_key,
            status="reserved" if decision.allowed else "denied",
            decision=decision,
        )

    def _decide(
        self,
        *,
        workspace_id: str,
        operation_key: str,
        estimated_units: dict[str, float],
        allocation: Allocation | None,
        policy: EntitlementPolicy,
        balance: WorkspaceBalance,
    ) -> UsageDecision:
        if allocation is None:
            return UsageDecision(allowed=False, reason="allocation_missing", message="No allocation is configured.")
        if allocation.workspace_id != workspace_id:
            return UsageDecision(allowed=False, reason="workspace_mismatch", message="Allocation belongs to another workspace.")
        if allocation.mode == "disabled" or allocation.status in {"disabled", "invalid"}:
            return UsageDecision(allowed=False, reason="allocation_disabled", message="Allocation is disabled or invalid.")
        if not policy.operation_allowed(operation_key):
            return UsageDecision(allowed=False, reason="operation_not_allowed", message="Operation is not enabled by policy.")

        maxima = policy.max_units_by_operation.get(operation_key, {})
        for unit, maximum in maxima.items():
            quantity = estimated_units.get(unit)
            if quantity is not None and quantity > maximum:
                return UsageDecision(
                    allowed=False,
                    reason="usage_limit_exceeded",
                    message=f"Estimated {unit} exceeds the operation limit.",
                )

        has_balance, unit = balance.has_units(estimated_units)
        if not has_balance:
            return UsageDecision(
                allowed=False,
                reason="insufficient_balance",
                message=f"Workspace does not have enough {unit}.",
            )

        return UsageDecision(allowed=True)
