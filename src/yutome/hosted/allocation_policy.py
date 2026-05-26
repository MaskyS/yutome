from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from yutome.hosted.allocations import Allocation, resolve_allocation
from yutome.hosted.gate import UsageGate
from yutome.hosted.ids import idempotency_key, input_hash
from yutome.hosted.models import (
    EntitlementPolicy,
    ProviderAllocation,
    ServiceAllocation,
    UsageReservation,
    UsageSubject,
    WorkspaceBalance,
)


EstimateMethod = Literal[
    "gemini.media_duration_formula",
    "gemini.count_tokens",
    "voyage.count_tokens",
    "voyage.local_token_counts",
    "webshare.request_estimate",
    "search_store.query_estimate",
]


class OperationEstimate(BaseModel):
    subject: UsageSubject
    operation: str
    estimated_units: dict[str, float] = Field(default_factory=dict)
    method: EstimateMethod
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def operation_key(self) -> str:
        return f"{self.subject}.{self.operation}"


class WebshareSubuserAllocation(BaseModel):
    id: str
    workspace_id: str
    subuser_id: str
    label: str | None = None
    proxy_limit_gb: float | None = None
    bandwidth_remaining_bytes: float | None = None
    max_thread_count: int | None = None
    status: Literal["active", "limited", "disabled", "invalid"] = "active"

    def to_provider_allocation(self, *, operation: str = "proxy_fetch") -> ProviderAllocation:
        return ProviderAllocation(
            id=self.id,
            workspace_id=self.workspace_id,
            provider="webshare",
            operation=operation,
            mode="hosted",
            status=self.status,
            external_allocation_id=self.subuser_id,
            metadata={
                "webshare_subuser_id": self.subuser_id,
                "label": self.label,
                "proxy_limit_gb": self.proxy_limit_gb,
                "bandwidth_remaining_bytes": self.bandwidth_remaining_bytes,
                "max_thread_count": self.max_thread_count,
            },
        )


@dataclass(frozen=True)
class AllocationPolicyDecision:
    reservation: UsageReservation
    estimate: OperationEstimate
    allocation: Allocation | None
    denial_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.reservation.decision.allowed


def decide_allocation_for_estimate(
    *,
    workspace_id: str,
    estimate: OperationEstimate,
    allocations: list[Allocation],
    policy: EntitlementPolicy,
    balance: WorkspaceBalance,
    subject_id: str | None = None,
    idempotency_extras: list[str] | None = None,
    gate: UsageGate | None = None,
) -> AllocationPolicyDecision:
    resolution = resolve_allocation(
        allocations,
        workspace_id=workspace_id,
        subject=estimate.subject,
        operation=estimate.operation,
    )
    key = idempotency_key(
        workspace_id=workspace_id,
        subject_id=subject_id,
        operation=estimate.operation_key,
        input_hash_value=input_hash(
            {
                "estimate": estimate.model_dump(mode="json"),
                "allocation_id": resolution.allocation.id if resolution.allocation else None,
            }
        ),
        extras=idempotency_extras,
    )
    reservation = (gate or UsageGate()).reserve(
        workspace_id=workspace_id,
        subject=estimate.subject,
        operation=estimate.operation,
        estimated_units=estimate.estimated_units,
        allocation=resolution.allocation,
        policy=policy,
        balance=balance,
        idempotency_key=key,
    )
    return AllocationPolicyDecision(
        reservation=reservation,
        estimate=estimate,
        allocation=resolution.allocation,
        denial_message=None if reservation.decision.allowed else user_facing_denial_message(reservation),
        metadata={"allocation_resolution": resolution.reason, "estimate_method": estimate.method},
    )


def estimate_gemini_media_transcription(
    *,
    duration_seconds: float,
    total_tokens_estimate: float | None = None,
    media_resolution: str | None = None,
) -> OperationEstimate:
    units = {"media_seconds": max(0.0, float(duration_seconds))}
    if total_tokens_estimate is not None:
        units["total_tokens"] = max(0.0, float(total_tokens_estimate))
    return OperationEstimate(
        subject="gemini",
        operation="transcribe_media",
        estimated_units=units,
        method="gemini.media_duration_formula",
        metadata={"media_resolution": media_resolution},
    )


def estimate_gemini_tokens(*, operation: str, total_tokens_estimate: float) -> OperationEstimate:
    return OperationEstimate(
        subject="gemini",
        operation=operation,
        estimated_units={"total_tokens": max(0.0, float(total_tokens_estimate))},
        method="gemini.count_tokens",
    )


def estimate_voyage_embeddings(*, operation: str, total_tokens_estimate: float, vectors: int) -> OperationEstimate:
    return OperationEstimate(
        subject="voyage",
        operation=operation,
        estimated_units={
            "total_tokens": max(0.0, float(total_tokens_estimate)),
            "vectors": max(0.0, float(vectors)),
        },
        method="voyage.count_tokens",
    )


def estimate_webshare_proxy_fetch(
    *,
    request_count: int = 1,
    bytes_estimate: float | None = None,
) -> OperationEstimate:
    units = {"request_count": max(0.0, float(request_count))}
    if bytes_estimate is not None:
        units["bytes"] = max(0.0, float(bytes_estimate))
    return OperationEstimate(
        subject="webshare",
        operation="proxy_fetch",
        estimated_units=units,
        method="webshare.request_estimate",
    )


def estimate_search_store_query(
    *,
    operation: Literal["lexical_query", "semantic_query", "hybrid_query"],
    candidate_limit: int,
    query_vector_dimensions: int | None = None,
) -> OperationEstimate:
    units = {"queries": 1.0, "candidate_limit": max(0.0, float(candidate_limit))}
    if query_vector_dimensions is not None:
        units["query_vector_dimensions"] = max(0.0, float(query_vector_dimensions))
    return OperationEstimate(
        subject="search_store",
        operation=operation,
        estimated_units=units,
        method="search_store.query_estimate",
    )


def user_facing_denial_message(reservation: UsageReservation) -> str:
    reason = reservation.decision.reason
    if reason == "allocation_missing":
        return "This hosted operation is not configured for the workspace."
    if reason == "allocation_disabled":
        return "This hosted operation is disabled for the workspace."
    if reason == "operation_not_allowed":
        return "This workspace plan does not allow this hosted operation."
    if reason == "usage_limit_exceeded":
        return "This hosted operation is above the per-operation limit."
    if reason == "insufficient_balance":
        return "This workspace does not have enough remaining usage balance."
    if reason == "workspace_mismatch":
        return "This allocation belongs to a different workspace."
    return reservation.decision.message or "This hosted operation is not allowed."


def default_search_store_allocation(
    *,
    workspace_id: str,
    operation: str = "*",
    backend: str = "postgres_vectorchord",
    index_profile_ref: str | None = None,
) -> ServiceAllocation:
    return ServiceAllocation(
        id=f"svc_{workspace_id}_search_store",
        workspace_id=workspace_id,
        service="search_store",
        operation=operation,
        mode="service_internal",
        status="active",
        backend=backend,
        index_profile_ref=index_profile_ref,
    )


__all__ = [
    "AllocationPolicyDecision",
    "OperationEstimate",
    "WebshareSubuserAllocation",
    "decide_allocation_for_estimate",
    "default_search_store_allocation",
    "estimate_gemini_media_transcription",
    "estimate_gemini_tokens",
    "estimate_search_store_query",
    "estimate_voyage_embeddings",
    "estimate_webshare_proxy_fetch",
    "user_facing_denial_message",
]
