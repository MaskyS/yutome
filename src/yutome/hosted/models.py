from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


UsageSubject = Literal["gemini", "voyage", "webshare", "search_store"]
CredentialMode = Literal["hosted", "byo_hosted", "disabled", "service_internal"]
AllocationStatus = Literal["active", "limited", "disabled", "invalid"]
ReservationStatus = Literal["reserved", "denied", "released", "reconciled"]
EventStatus = Literal["started", "succeeded", "failed", "denied", "released"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProviderAllocation(BaseModel):
    id: str
    workspace_id: str
    provider: Literal["gemini", "voyage", "webshare"]
    operation: str
    mode: CredentialMode = "hosted"
    status: AllocationStatus = "active"
    model_or_plan: str | None = None
    external_allocation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ServiceAllocation(BaseModel):
    id: str
    workspace_id: str
    service: Literal["search_store"]
    operation: str
    mode: CredentialMode = "service_internal"
    status: AllocationStatus = "active"
    backend: str
    index_profile_ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EntitlementPolicy(BaseModel):
    id: str
    workspace_id: str
    allow_all_operations: bool = False
    allowed_operations: set[str] = Field(default_factory=set)
    max_units_by_operation: dict[str, dict[str, float]] = Field(default_factory=dict)

    def operation_allowed(self, operation_key: str) -> bool:
        return self.allow_all_operations or operation_key in self.allowed_operations


class WorkspaceBalance(BaseModel):
    workspace_id: str
    remaining_units: dict[str, float] = Field(default_factory=dict)
    unlimited_units: set[str] = Field(default_factory=set)

    def has_units(self, estimate: dict[str, float]) -> tuple[bool, str | None]:
        for unit, quantity in estimate.items():
            if unit in self.unlimited_units:
                continue
            remaining = self.remaining_units.get(unit)
            if remaining is None or quantity > remaining:
                return False, unit
        return True, None


class UsageDecision(BaseModel):
    allowed: bool
    reason: str = "allowed"
    message: str | None = None


class UsageReservation(BaseModel):
    id: str = Field(default_factory=lambda: f"res_{uuid4().hex}")
    workspace_id: str
    subject: UsageSubject
    operation: str
    allocation_id: str | None = None
    allocation_kind: CredentialMode
    estimated_units: dict[str, float] = Field(default_factory=dict)
    idempotency_key: str
    status: ReservationStatus
    decision: UsageDecision
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def operation_key(self) -> str:
        return f"{self.subject}.{self.operation}"


class UsageEvent(BaseModel):
    id: str = Field(default_factory=lambda: f"evt_{uuid4().hex}")
    reservation_id: str | None = None
    workspace_id: str
    subject: UsageSubject
    operation: str
    event_type: str
    status: EventStatus
    actual_units: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    provider_request_id: str | None = None
    error_code: str | None = None
    raw_usage: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def operation_key(self) -> str:
        return f"{self.subject}.{self.operation}"


class UsageNormalization(BaseModel):
    subject: UsageSubject
    operation: str
    actual_units: dict[str, float | int | str | bool | None] = Field(default_factory=dict)
    provider_request_id: str | None = None
    raw_usage: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
