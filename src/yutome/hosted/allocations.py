from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from yutome.hosted.models import (
    ProviderAllocation,
    ServiceAllocation,
    UsageSubject,
)


Allocation = ProviderAllocation | ServiceAllocation
AllocationResolutionReason = Literal["matched_exact", "matched_wildcard", "missing"]


@dataclass(frozen=True)
class AllocationResolution:
    workspace_id: str
    subject: UsageSubject
    operation: str
    allocation: Allocation | None
    reason: AllocationResolutionReason

    @property
    def operation_key(self) -> str:
        return operation_key(self.subject, self.operation)


def operation_key(subject: UsageSubject, operation: str) -> str:
    return f"{subject}.{operation}"


def resolve_allocation(
    allocations: Iterable[Allocation],
    *,
    workspace_id: str,
    subject: UsageSubject,
    operation: str,
) -> AllocationResolution:
    """Resolve the allocation a hosted operation should present to UsageGate."""
    candidates = [
        allocation
        for allocation in allocations
        if _allocation_subject(allocation) == subject
        and allocation.workspace_id == workspace_id
        and allocation.operation in {operation, "*"}
    ]
    if not candidates:
        return AllocationResolution(
            workspace_id=workspace_id,
            subject=subject,
            operation=operation,
            allocation=None,
            reason="missing",
        )

    allocation = sorted(candidates, key=lambda candidate: _allocation_rank(candidate, operation))[0]
    return AllocationResolution(
        workspace_id=workspace_id,
        subject=subject,
        operation=operation,
        allocation=allocation,
        reason="matched_exact" if allocation.operation == operation else "matched_wildcard",
    )


def allocation_operation_matches(allocation: Allocation, operation: str) -> bool:
    return allocation.operation in {operation, "*"}


def _allocation_subject(allocation: Allocation) -> UsageSubject:
    if isinstance(allocation, ProviderAllocation):
        return allocation.provider
    return allocation.service


def _allocation_rank(allocation: Allocation, operation: str) -> tuple[int, int, str]:
    operation_rank = 0 if allocation.operation == operation else 1
    status_rank = 0 if allocation.status in {"active", "limited"} and allocation.credential_mode != "disabled" else 1
    return operation_rank, status_rank, allocation.id


__all__ = [
    "Allocation",
    "AllocationResolution",
    "allocation_operation_matches",
    "operation_key",
    "resolve_allocation",
]
