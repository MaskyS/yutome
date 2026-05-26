from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, TypeAlias
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


UsageSubject = Literal["gemini", "voyage", "webshare", "search_store"]
CredentialMode = Literal["hosted", "byo_hosted", "disabled", "service_internal"]
AllocationStatus = Literal["active", "limited", "disabled", "invalid"]
ReservationStatus = Literal["reserved", "denied", "released", "reconciled"]
EventStatus = Literal["started", "succeeded", "failed", "denied", "released"]
UsageDenialEffect = Literal["hard", "soft"]
UnitQuantity: TypeAlias = int | Decimal
UnitMap: TypeAlias = dict[str, UnitQuantity]
UsageUnitValue: TypeAlias = UnitQuantity | str | bool | None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_unit_quantity(value: Any) -> UnitQuantity:
    if isinstance(value, bool):
        raise TypeError("Boolean values are not usage quantities.")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return _canonical_quantity(value)
    if isinstance(value, float):
        return _canonical_quantity(Decimal(str(value)))
    if isinstance(value, str):
        try:
            return _canonical_quantity(Decimal(value))
        except InvalidOperation as exc:
            raise ValueError(f"Invalid usage quantity: {value!r}") from exc
    raise TypeError(f"Invalid usage quantity type: {type(value).__name__}.")


def normalize_unit_map(value: Any, *, allow_negative: bool = False) -> UnitMap:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError("Usage units must be a mapping.")
    normalized: UnitMap = {}
    for unit, quantity in value.items():
        exact = normalize_unit_quantity(quantity)
        if not allow_negative and unit_quantity_decimal(exact) < 0:
            raise ValueError(f"Usage quantity for {unit} must be non-negative.")
        normalized[str(unit)] = exact
    return normalized


def normalize_usage_units(value: Any) -> dict[str, UsageUnitValue]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError("Usage units must be a mapping.")
    normalized: dict[str, UsageUnitValue] = {}
    for unit, quantity in value.items():
        if quantity is None or isinstance(quantity, bool):
            normalized[str(unit)] = quantity
        elif isinstance(quantity, (int, float, Decimal)):
            normalized[str(unit)] = normalize_unit_quantity(quantity)
        elif isinstance(quantity, str):
            try:
                normalized[str(unit)] = normalize_unit_quantity(quantity)
            except ValueError:
                normalized[str(unit)] = quantity
        else:
            raise TypeError(f"Invalid usage unit value for {unit}: {type(quantity).__name__}.")
    return normalized


def jsonable_exact(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable_exact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable_exact(item) for item in value]
    if isinstance(value, set):
        return sorted(jsonable_exact(item) for item in value)
    return value


def unit_quantity_decimal(value: UnitQuantity) -> Decimal:
    return Decimal(value) if isinstance(value, int) else value


def add_unit_maps(*maps: dict[str, UnitQuantity]) -> UnitMap:
    totals: dict[str, Decimal] = {}
    for unit_map in maps:
        for unit, quantity in unit_map.items():
            totals[unit] = totals.get(unit, Decimal("0")) + unit_quantity_decimal(quantity)
    return {unit: _canonical_quantity(quantity) for unit, quantity in totals.items() if quantity != 0}


def subtract_unit_maps(left: dict[str, UnitQuantity], right: dict[str, UnitQuantity]) -> UnitMap:
    negative_right = {unit: -unit_quantity_decimal(quantity) for unit, quantity in right.items()}
    return add_unit_maps(left, negative_right)


def _canonical_quantity(value: Decimal) -> UnitQuantity:
    if not value.is_finite():
        raise ValueError("Usage quantities must be finite.")
    if value == value.to_integral_value():
        return int(value)
    return value


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
    max_units_by_operation: dict[str, UnitMap] = Field(default_factory=dict)
    soft_units_by_operation: dict[str, UnitMap] = Field(default_factory=dict)

    @field_validator("max_units_by_operation", "soft_units_by_operation", mode="before")
    @classmethod
    def _normalize_limit_units(cls, value: Any) -> dict[str, UnitMap]:
        if value is None:
            return {}
        return {str(operation): normalize_unit_map(units) for operation, units in dict(value).items()}

    def operation_allowed(self, operation_key: str) -> bool:
        subject, _, _operation = operation_key.partition(".")
        return (
            self.allow_all_operations
            or "*" in self.allowed_operations
            or operation_key in self.allowed_operations
            or f"{subject}.*" in self.allowed_operations
        )


class WorkspaceBalance(BaseModel):
    workspace_id: str
    remaining_units: UnitMap = Field(default_factory=dict)
    unlimited_units: set[str] = Field(default_factory=set)

    @field_validator("remaining_units", mode="before")
    @classmethod
    def _normalize_remaining_units(cls, value: Any) -> UnitMap:
        return normalize_unit_map(value)

    def has_units(self, estimate: dict[str, UnitQuantity]) -> tuple[bool, str | None]:
        exact_estimate = normalize_unit_map(estimate)
        for unit, quantity in exact_estimate.items():
            if unit in self.unlimited_units:
                continue
            remaining = self.remaining_units.get(unit)
            if remaining is None or unit_quantity_decimal(quantity) > unit_quantity_decimal(remaining):
                return False, unit
        return True, None


class UsageDecision(BaseModel):
    allowed: bool
    reason: str = "allowed"
    message: str | None = None
    denial_effect: UsageDenialEffect = "hard"


class UsageReservation(BaseModel):
    id: str = Field(default_factory=lambda: f"res_{uuid4().hex}")
    workspace_id: str
    subject: UsageSubject
    operation: str
    allocation_id: str | None = None
    allocation_kind: CredentialMode
    estimated_units: UnitMap = Field(default_factory=dict)
    idempotency_key: str
    status: ReservationStatus
    decision: UsageDecision
    created_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("estimated_units", mode="before")
    @classmethod
    def _normalize_estimated_units(cls, value: Any) -> UnitMap:
        return normalize_unit_map(value)

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
    actual_units: dict[str, UsageUnitValue] = Field(default_factory=dict)
    provider_request_id: str | None = None
    error_code: str | None = None
    raw_usage: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("actual_units", mode="before")
    @classmethod
    def _normalize_actual_units(cls, value: Any) -> dict[str, UsageUnitValue]:
        return normalize_usage_units(value)

    @property
    def operation_key(self) -> str:
        return f"{self.subject}.{self.operation}"


class UsageNormalization(BaseModel):
    subject: UsageSubject
    operation: str
    actual_units: dict[str, UsageUnitValue] = Field(default_factory=dict)
    provider_request_id: str | None = None
    raw_usage: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("actual_units", mode="before")
    @classmethod
    def _normalize_actual_units(cls, value: Any) -> dict[str, UsageUnitValue]:
        return normalize_usage_units(value)
