from __future__ import annotations

import hashlib
import hmac
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from psycopg.types.json import Jsonb
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from yutome.hosted.errors import redact_sensitive_failure_text
from yutome.hosted.ids import input_hash
from yutome.hosted.models import (
    EntitlementPolicy,
    UnitMap,
    UnitQuantity,
    UsageEvent,
    WorkspaceBalance,
    add_unit_maps,
    jsonable_exact,
    normalize_unit_map,
    normalize_usage_units,
    unit_quantity_decimal,
)
from yutome.hosted.schema import (
    entitlement_policies,
    price_books,
    stripe_customers,
    stripe_meter_exports,
    stripe_webhook_events,
    workspace_balances,
)
from yutome.hosted.sqlalchemy_core import compile_postgres_statement


# Stripe is the only billing provider. The billing mirror never authorizes a call.
BillingProvider = Literal["stripe"]
BillingReplayStatus = Literal["pending", "processing", "succeeded", "failed", "skipped"]
BillingRecordStatus = Literal["draft", "active", "paused", "archived"]
SubscriptionStatus = Literal["active", "past_due", "canceled", "none"]

# Single composite billable meter. Internal cost/quota units are collapsed into one
# `credits` value reported to one Stripe Billing Meter. event_name must match the
# event_name configured on the Stripe Billing Meter; the metered Price binds to that
# meter via recurring.meter.
CREDITS_METER_EVENT_NAME = "yutome.credits"
CREDITS_METER_ID_ENV_VAR = "STRIPE_METER_CREDITS_ID"
CREDITS_UNIT = "credits"

# Metered overage is OFF for launch (hard-cap model): the flat seat is the only charge and the
# included allowance is a pure cap enforced by UsageGate, so no meter events are sent. Flip
# STRIPE_OVERAGE_ENABLED on — together with request rate limiting and a per-period overage
# ceiling (yt-indexer-434 / yt-indexer-6a0) — to bill usage beyond the included allowance. The
# overage machinery below stays built and tested, just dormant.
STRIPE_OVERAGE_ENABLED_ENV_VAR = "STRIPE_OVERAGE_ENABLED"


def overage_metering_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(STRIPE_OVERAGE_ENABLED_ENV_VAR) or "").strip().lower() in {"1", "true", "yes", "on"}


# Internal usage units that contribute to one composite `credits` value. The weight is
# credits charged per unit of usage. Units absent from this table (candidate_limit,
# query_vector_dimensions, request_count, bytes, latency_ms, …) are cost-visibility /
# quota units and are NEVER reported to Stripe — mirroring the workspace-balance
# "untracked_units" discipline.
#
# Calibration: 1 credit ~= 1 indexed video-hour ~= ~$0.10 retail. A ~20-min video on the
# common index path (existing transcript -> Gemini cleanup -> Voyage embed) costs about
# 5_700 total_tokens + ~8 vectors ~= 0.34 credits, so ~3 videos/hour ~= ~1.02 credits per
# video-hour. media_seconds covers the Gemini transcribe fallback path (no caption track);
# queries price read traffic. See STARTER_INCLUDED_UNITS in account.py for the seat
# allowance derived from these weights.
STRIPE_CREDIT_UNIT_WEIGHTS: dict[str, Decimal] = {
    "media_seconds": Decimal("0.0003"),
    "total_tokens": Decimal("0.00005"),
    "vectors": Decimal("0.007"),
    "queries": Decimal("0.001"),
}


@dataclass(frozen=True)
class BillingSqlStatement:
    sql: str
    params: dict[str, Any]


class StripeWebhookVerificationError(ValueError):
    pass


class ProductLimit(BaseModel):
    product_code: str
    operation_key: str
    unit: str
    included_quantity: UnitQuantity | None = None
    hard_limit: UnitQuantity | None = None
    meter_event_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PriceBookProduct(BaseModel):
    code: str
    name: str
    stripe_price_id: str | None = None
    limits: tuple[ProductLimit, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    def limits_for_operation(self, operation_key: str) -> tuple[ProductLimit, ...]:
        return tuple(limit for limit in self.limits if limit.operation_key == operation_key)


class PriceBook(BaseModel):
    id: str
    version: str
    products: tuple[PriceBookProduct, ...] = ()
    currency: str = "usd"
    unit_mapping: dict[str, Any] = Field(default_factory=dict)
    status: BillingRecordStatus = "draft"
    effective_at: datetime | None = None
    created_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def product(self, code: str) -> PriceBookProduct | None:
        return next((product for product in self.products if product.code == code), None)


class EntitlementPolicyRecord(BaseModel):
    id: str
    workspace_id: str
    plan_key: str
    price_book_id: str
    allowed_operations: tuple[str, ...] = ()
    included_units: dict[str, UnitMap] = Field(default_factory=dict)
    hard_limits: dict[str, UnitMap] = Field(default_factory=dict)
    soft_limits: dict[str, UnitMap] = Field(default_factory=dict)
    grace_policy: dict[str, Any] = Field(default_factory=dict)
    status: BillingRecordStatus = "active"
    created_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("included_units", "hard_limits", "soft_limits", mode="before")
    @classmethod
    def _normalize_nested_units(cls, value: Any) -> dict[str, UnitMap]:
        if value is None:
            return {}
        return {str(operation): normalize_unit_map(units) for operation, units in dict(value).items()}

    def to_runtime_policy(self) -> EntitlementPolicy:
        return EntitlementPolicy(
            id=self.id,
            workspace_id=self.workspace_id,
            allow_all_operations=False,
            allowed_operations=set(self.allowed_operations),
            hard_limits_by_operation=dict(self.hard_limits),
            soft_limits_by_operation=dict(self.soft_limits),
        )


class WorkspaceBalanceSnapshot(BaseModel):
    workspace_id: str
    entitlement_policy_id: str
    period_start_at: datetime
    period_end_at: datetime
    used_units: UnitMap = Field(default_factory=dict)
    reserved_units: UnitMap = Field(default_factory=dict)
    remaining_units: UnitMap = Field(default_factory=dict)
    unlimited_units: tuple[str, ...] = ()
    updated_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("used_units", "reserved_units", "remaining_units", mode="before")
    @classmethod
    def _normalize_units(cls, value: Any) -> UnitMap:
        return normalize_unit_map(value)

    def to_runtime_balance(self) -> WorkspaceBalance:
        return WorkspaceBalance(
            workspace_id=self.workspace_id,
            remaining_units=dict(self.remaining_units),
            unlimited_units=set(self.unlimited_units),
        )


class StripeCustomer(BaseModel):
    """One Stripe Customer per workspace. A free workspace has no row until it
    subscribes via /billing/checkout (lazy creation)."""

    id: str
    workspace_id: str
    provider: BillingProvider = "stripe"
    stripe_customer_id: str
    stripe_subscription_id: str | None = None
    subscription_status: SubscriptionStatus = "none"
    subscription_status_snapshot: dict[str, Any] = Field(default_factory=dict)
    last_webhook_at: datetime | None = None
    status: BillingRecordStatus = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class StripeMeterExportEvent(BaseModel):
    """A settled usage event mirrored out to the Stripe composite credits meter,
    idempotent by source_event_dedupe_key (== row id == Stripe meter_event identifier)."""

    idempotency_key: str
    usage_event_id: str
    reservation_id: str | None = None
    workspace_id: str
    stripe_customer_id: str | None = None
    operation_key: str
    meter_unit: str = CREDITS_UNIT
    event_name: str = CREDITS_METER_EVENT_NAME
    value: UnitQuantity = 0
    replay_status: BillingReplayStatus = "pending"
    timestamp: datetime
    stripe_meter_event_identifier: str | None = None
    attempt_count: int = 0
    last_error_code: str | None = None
    last_error_message: str | None = None
    exported_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("value", mode="before")
    @classmethod
    def _normalize_value(cls, value: Any) -> UnitQuantity:
        from yutome.hosted.models import normalize_unit_quantity

        return normalize_unit_quantity(value)

    @property
    def source_event_dedupe_key(self) -> str:
        return f"stripe:{self.workspace_id}:{self.usage_event_id}:{self.meter_unit}"

    @property
    def value_decimal(self) -> Decimal:
        return unit_quantity_decimal(self.value)

    @property
    def stripe_identifier(self) -> str:
        """The value sent as Stripe meter_event.identifier (Stripe caps it at 100 chars).

        source_event_dedupe_key embeds the 68-char `evt_<sha256>` usage event id, so the
        readable key overruns the limit (~111 chars). Hash it to a stable, collision-
        resistant id that is still 1:1 with the dedupe key, so Stripe's own >=24h
        identifier dedupe holds across retries. The readable form stays in the DB key and
        in metadata for debugging."""
        return "me_" + hashlib.sha256(self.source_event_dedupe_key.encode("utf-8")).hexdigest()


class StripeWebhookEvent(BaseModel):
    """A persisted Stripe webhook event. id is the Stripe event id (evt_...), giving
    exactly-once processing via on_conflict_do_nothing on the PK."""

    id: str
    type: str
    workspace_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    status: BillingReplayStatus = "pending"
    received_at: datetime
    processed_at: datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None


class StripeWebhookProcessingResult(BaseModel):
    event: StripeWebhookEvent
    stripe_customer: StripeCustomer | None = None
    ignored: bool = False


class StripeMeterExportWorkerResult(BaseModel):
    tick: str = "stripe_meter_export_once"
    attempted: bool
    affected_rows: int
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    secret_key_configured: bool = False
    rows: list[dict[str, Any]] = Field(default_factory=list)


class BillingDebugUsageEvent(BaseModel):
    id: str
    event_type: str
    status: str
    actual_units: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    provider_request_id: str | None = None
    created_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BillingDebugExport(BaseModel):
    id: str
    usage_event_id: str
    provider: BillingProvider = "stripe"
    replay_status: str
    stripe_customer_id: str | None = None
    meter_unit: str | None = None
    value: str | None = None
    stripe_meter_event_identifier: str | None = None
    source_event_dedupe_key: str
    attempt_count: int = 0
    last_error: dict[str, Any] = Field(default_factory=dict)
    exported_at: datetime | None = None
    updated_at: datetime | None = None


class BillingDebugReservation(BaseModel):
    reservation_id: str
    workspace_id: str
    job_id: str | None = None
    job_status: str | None = None
    job_error_code: str | None = None
    job_error_message: str | None = None
    operation_id: str | None = None
    job_operation: str | None = None
    operation_status: str | None = None
    video_id: str | None = None
    subject: str
    operation: str
    operation_key: str
    allocation_id: str | None = None
    credential_mode: str
    reservation_status: str
    entitlement_decision: dict[str, Any] = Field(default_factory=dict)
    estimated_units: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    created_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    usage_events: tuple[BillingDebugUsageEvent, ...] = ()
    meter_exports: tuple[BillingDebugExport, ...] = ()


class BillingDebugSnapshot(BaseModel):
    workspace_id: str
    limit: int
    operation: str | None = None
    rows: tuple[BillingDebugReservation, ...] = ()


POSTGRES_BILLING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS price_books (
    id text PRIMARY KEY,
    version text NOT NULL UNIQUE,
    effective_at timestamptz,
    currency text NOT NULL DEFAULT 'usd',
    products_jsonb jsonb NOT NULL DEFAULT '[]'::jsonb,
    unit_mapping_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'draft',
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS entitlement_policies (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    plan_key text NOT NULL,
    price_book_id text NOT NULL REFERENCES price_books(id),
    allowed_operations text[] NOT NULL DEFAULT ARRAY[]::text[],
    included_units_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    hard_limits_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    soft_limits_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    grace_policy_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'active',
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, plan_key, price_book_id)
);

CREATE TABLE IF NOT EXISTS workspace_balances (
    workspace_id text PRIMARY KEY REFERENCES workspaces(id),
    entitlement_policy_id text NOT NULL REFERENCES entitlement_policies(id),
    period_start_at timestamptz NOT NULL,
    period_end_at timestamptz NOT NULL,
    used_units_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    reserved_units_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    remaining_units_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    unlimited_units text[] NOT NULL DEFAULT ARRAY[]::text[],
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS stripe_customers (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id) UNIQUE,
    stripe_customer_id text NOT NULL UNIQUE,
    stripe_subscription_id text,
    subscription_status text NOT NULL DEFAULT 'none',
    subscription_status_snapshot_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_webhook_at timestamptz,
    status text NOT NULL DEFAULT 'active',
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS stripe_meter_exports (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    usage_event_id text NOT NULL REFERENCES usage_events(id),
    reservation_id text REFERENCES usage_reservations(id),
    stripe_customer_id text,
    meter_unit text NOT NULL,
    event_name text NOT NULL,
    value_text text NOT NULL,
    source_event_dedupe_key text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    stripe_meter_event_identifier text,
    attempt_count integer NOT NULL DEFAULT 0,
    last_error_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    event_timestamp timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    exported_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(source_event_dedupe_key)
);

CREATE TABLE IF NOT EXISTS stripe_webhook_events (
    id text PRIMARY KEY,
    type text NOT NULL,
    workspace_id text REFERENCES workspaces(id),
    payload_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'pending',
    received_at timestamptz NOT NULL,
    processed_at timestamptz,
    last_error_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_stripe_meter_exports_replay
    ON stripe_meter_exports(status, updated_at)
    WHERE status IN ('pending', 'failed');
CREATE INDEX IF NOT EXISTS idx_stripe_webhook_events_replay
    ON stripe_webhook_events(status, received_at)
    WHERE status IN ('pending', 'failed');
""".strip()


def credits_from_billable_units(units: Mapping[str, Any]) -> Decimal:
    """Collapse internal billable units into one composite `credits` value.

    Only units in STRIPE_CREDIT_UNIT_WEIGHTS contribute; all other units stay internal.
    Returns an exact Decimal so fractional media_seconds/tokens are not truncated.
    """

    total = Decimal(0)
    for unit, quantity in _billable_units(units).items():
        weight = STRIPE_CREDIT_UNIT_WEIGHTS.get(unit)
        if weight is None:
            continue
        total += unit_quantity_decimal(quantity) * weight
    return total


def included_allowance_credits(remaining_units: Mapping[str, Any]) -> Decimal:
    """Credits-equivalent of the billable portion of a workspace balance.

    The seat ships a monthly included allowance tracked as billable units in
    WorkspaceBalance.remaining_units (total_tokens/vectors/queries/media_seconds). Collapsing
    that remaining allowance through the same composite weights yields how many credits of
    included usage are still free. Non-billable quota units (candidate_limit, request_count,
    …) do not contribute. A unit that is overdrawn (negative remaining) contributes 0 rather
    than a negative credit, so an overdrawn balance reads as "no allowance left" not "owed".
    """

    total = Decimal(0)
    for unit, quantity in normalize_usage_units(remaining_units).items():
        weight = STRIPE_CREDIT_UNIT_WEIGHTS.get(unit)
        if weight is None or isinstance(quantity, (bool, str)) or quantity is None:
            continue
        exact = unit_quantity_decimal(quantity)
        if exact <= 0:
            continue
        total += exact * weight
    return total


def overage_credits_for_event(
    event_credits: Decimal,
    *,
    included_remaining_credits: Decimal,
) -> Decimal:
    """Portion of an event's composite credits that falls BEYOND the included allowance.

    ``included_remaining_credits`` is the credits-equivalent of the seat allowance still
    available BEFORE this event consumed any of it. Only the excess is metered to Stripe:

      - allowance fully covers the event  -> 0 overage (nothing metered, never over-bills)
      - allowance partially covers it     -> meter exactly the uncovered remainder
      - allowance already exhausted (<=0)  -> meter the whole event (never under-bills)

    Clamped to >= 0 so a negative/overdrawn allowance figure still meters the full event.
    """

    available = included_remaining_credits if included_remaining_credits > 0 else Decimal(0)
    overage = event_credits - available
    return overage if overage > 0 else Decimal(0)


def stripe_meter_export_idempotency_key(event: UsageEvent, *, meter_unit: str = CREDITS_UNIT) -> str:
    return f"stripe:{event.workspace_id}:{event.id}:{meter_unit}"


def stripe_meter_exports_from_usage_event(
    event: UsageEvent,
    *,
    stripe_customer_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    credits_override: Decimal | None = None,
) -> tuple[StripeMeterExportEvent, ...]:
    """Build the composite credits meter export for a usage event.

    One usage event maps to one credits meter export. The row is `pending` only when the
    event settled (succeeded/released); otherwise `skipped`. A zero/empty credits value
    is skipped (nothing to bill).

    ``credits_override`` meters only the OVERAGE portion (credits beyond the seat's monthly
    included allowance). When supplied and <= 0 the whole event sits within the allowance and
    nothing is enqueued. When omitted the full event credits are metered (e.g. a workspace
    with no remaining included allowance, or a caller that has already netted out allowance).
    """

    credits = credits_from_billable_units(event.actual_units)
    if credits <= 0:
        return ()
    metered_credits = credits if credits_override is None else credits_override
    if metered_credits <= 0:
        return ()
    settled = event.status in {"succeeded", "released"}
    replay_status: BillingReplayStatus = "pending" if settled else "skipped"
    export_metadata = {
        "usage_event_id": event.id,
        "reservation_id": event.reservation_id,
        "workspace_id": event.workspace_id,
        "subject": event.subject,
        "operation": event.operation,
        "operation_key": event.operation_key,
        "event_type": event.event_type,
        "status": event.status,
        "provider_request_id": event.provider_request_id,
        "billable_units": {
            unit: str(unit_quantity_decimal(quantity))
            for unit, quantity in _billable_units(event.actual_units).items()
            if unit in STRIPE_CREDIT_UNIT_WEIGHTS
        },
        # event_credits = the event's full composite credits; metered_credits = the overage
        # portion actually reported to Stripe (== event_credits when no allowance is netted).
        "event_credits": str(credits),
        "metered_credits": str(metered_credits),
    }
    export_metadata.update(_redacted_metadata(metadata or {}))
    export = StripeMeterExportEvent(
        idempotency_key=stripe_meter_export_idempotency_key(event),
        usage_event_id=event.id,
        reservation_id=event.reservation_id,
        workspace_id=event.workspace_id,
        stripe_customer_id=stripe_customer_id,
        operation_key=event.operation_key,
        meter_unit=CREDITS_UNIT,
        event_name=CREDITS_METER_EVENT_NAME,
        value=metered_credits,
        replay_status=replay_status,
        timestamp=event.created_at,
        metadata={key: value for key, value in export_metadata.items() if value is not None},
    )
    return (export,)


def stripe_meter_event_payload(export: StripeMeterExportEvent) -> dict[str, Any]:
    """Build the Stripe v1 /v1/billing/meter_events body for a meter export.

    Stripe form-encodes payload[stripe_customer_id] and payload[value]; identifier dedupes
    over a rolling >=24h window; timestamp must be within past 35 days / <=5min future.
    """

    return {
        "event_name": export.event_name,
        "payload": {
            "stripe_customer_id": export.stripe_customer_id,
            "value": str(export.value_decimal),
        },
        "identifier": export.stripe_identifier,
        "timestamp": int(export.timestamp.timestamp()),
    }


def derive_workspace_balance_snapshot(
    *,
    workspace_id: str,
    entitlement_policy_id: str,
    period_start_at: datetime,
    period_end_at: datetime,
    starting_units: dict[str, UnitQuantity] | None = None,
    used_units: dict[str, UnitQuantity] | None = None,
    reserved_units: dict[str, UnitQuantity] | None = None,
    unlimited_units: tuple[str, ...] = (),
    updated_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> WorkspaceBalanceSnapshot:
    available = normalize_unit_map(starting_units or {})
    used = normalize_unit_map(used_units or {})
    reserved = normalize_unit_map(reserved_units or {})
    # The balance tracks only PROVISIONED units — those the plan includes or marks
    # unlimited. Providers report far more (latency_ms, candidate_tokens, vector
    # dimensions, request byte sizes, …); that usage stays in the usage ledger, but
    # must not enter the balance, or each un-provisioned unit becomes a phantom quota
    # that immediately goes negative.
    tracked_units = set(available) | set(unlimited_units)
    untracked_units = sorted((set(used) | set(reserved)) - tracked_units)
    used = {unit: quantity for unit, quantity in used.items() if unit in tracked_units}
    reserved = {unit: quantity for unit, quantity in reserved.items() if unit in tracked_units}
    net_remaining = {
        unit: quantity
        for unit, quantity in add_unit_maps(available, _negated_units(used), _negated_units(reserved)).items()
        if unit not in unlimited_units
    }
    remaining, overdrawn = _clamped_remaining_units(net_remaining)
    snapshot_metadata = dict(metadata or {})
    snapshot_metadata["derived_from"] = {"starting_units": available}
    snapshot_metadata["balance_status"] = "overdrawn" if overdrawn else "available"
    snapshot_metadata["remaining_units_clamped"] = bool(overdrawn)
    snapshot_metadata["net_remaining_units"] = net_remaining
    snapshot_metadata["overdrawn_units"] = overdrawn
    # Units reported by providers but not provisioned by the plan: kept out of the
    # balance, recorded here (and in the usage ledger) for cost visibility.
    snapshot_metadata["untracked_units"] = untracked_units
    return WorkspaceBalanceSnapshot(
        workspace_id=workspace_id,
        entitlement_policy_id=entitlement_policy_id,
        period_start_at=period_start_at,
        period_end_at=period_end_at,
        used_units=used,
        reserved_units=reserved,
        remaining_units=remaining,
        unlimited_units=unlimited_units,
        updated_at=updated_at,
        metadata=snapshot_metadata,
    )


def derive_workspace_balance_snapshot_from_rows(
    *,
    workspace_id: str,
    entitlement_policy_id: str,
    period_start_at: datetime,
    period_end_at: datetime,
    usage_rows: tuple[Mapping[str, Any], ...] = (),
    reserved_rows: tuple[Mapping[str, Any], ...] = (),
    starting_units: dict[str, UnitQuantity] | None = None,
    unlimited_units: tuple[str, ...] = (),
    updated_at: datetime | None = None,
) -> WorkspaceBalanceSnapshot:
    used_units = add_unit_maps(*(_units_from_json_row(row, "actual_units_json") for row in usage_rows)) if usage_rows else {}
    reserved_units = (
        add_unit_maps(*(_units_from_json_row(row, "estimated_units_json") for row in reserved_rows)) if reserved_rows else {}
    )
    return derive_workspace_balance_snapshot(
        workspace_id=workspace_id,
        entitlement_policy_id=entitlement_policy_id,
        period_start_at=period_start_at,
        period_end_at=period_end_at,
        starting_units=starting_units,
        used_units=used_units,
        reserved_units=reserved_units,
        unlimited_units=unlimited_units,
        updated_at=updated_at,
        metadata={
            "reconciliation": "usage_reservations",
            "usage_event_ids": [row.get("id") for row in usage_rows if row.get("id") is not None],
            "reserved_reservation_ids": [row.get("id") for row in reserved_rows if row.get("id") is not None],
        },
    )


def balance_reconciliation_input_sql(
    *,
    workspace_id: str,
    period_start_at: datetime,
    period_end_at: datetime,
) -> BillingSqlStatement:
    return BillingSqlStatement(
        sql="""
SELECT 'usage' AS row_kind,
       id,
       workspace_id,
       actual_units_json,
       NULL::jsonb AS estimated_units_json,
       created_at AS row_timestamp
FROM usage_events
WHERE workspace_id = %(workspace_id)s
  AND status IN ('succeeded', 'released')
  AND created_at >= %(period_start_at)s
  AND created_at < %(period_end_at)s
UNION ALL
SELECT 'reservation' AS row_kind,
       id,
       workspace_id,
       NULL::jsonb AS actual_units_json,
       estimated_units_json,
       created_at AS row_timestamp
FROM usage_reservations
WHERE workspace_id = %(workspace_id)s
  AND status = 'reserved'
  AND created_at >= %(period_start_at)s
  AND created_at < %(period_end_at)s
ORDER BY row_timestamp ASC, id ASC;
""".strip(),
        params={
            "workspace_id": workspace_id,
            "period_start_at": period_start_at,
            "period_end_at": period_end_at,
        },
    )


def mark_stripe_meter_export_replay(
    export: StripeMeterExportEvent,
    *,
    replay_status: BillingReplayStatus,
    stripe_meter_event_identifier: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> StripeMeterExportEvent:
    return export.model_copy(
        update={
            "replay_status": replay_status,
            "stripe_meter_event_identifier": stripe_meter_event_identifier or export.stripe_meter_event_identifier,
            "last_error_code": error_code,
            "last_error_message": error_message,
            "attempt_count": export.attempt_count + 1,
        }
    )


# --- Stripe webhook ---------------------------------------------------------

def stripe_webhook_event_from_payload(
    payload: dict[str, Any],
    *,
    received_at: datetime | None = None,
) -> StripeWebhookEvent:
    event_id = _optional_string(payload.get("id"))
    if event_id is None:
        raise StripeWebhookVerificationError("webhook_event_id_missing")
    event_type = str(payload.get("type") or "")
    return StripeWebhookEvent(
        id=event_id,
        type=event_type,
        workspace_id=_workspace_id_from_event(payload),
        payload=dict(payload),
        received_at=received_at or _event_received_at(payload),
    )


def verify_stripe_webhook_signature(
    *,
    raw_body: bytes,
    header: str | None,
    secret: str,
    now: int | None = None,
    tolerance_seconds: int = 300,
) -> None:
    """Verify a Stripe webhook signature.

    Stripe signs `'<ts>.' + raw_body` with HMAC-SHA256 keyed by the raw whsec_ secret
    string (NOT base64-decoded). The Stripe-Signature header is `t=<ts>,v1=<sig>,...`.
    Raises StripeWebhookVerificationError on any failure.
    """

    if not header or not header.strip():
        raise StripeWebhookVerificationError("webhook_signature_missing")
    timestamp: str | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    if timestamp is None or not signatures:
        raise StripeWebhookVerificationError("webhook_signature_missing")
    try:
        timestamp_int = int(timestamp)
    except ValueError as exc:
        raise StripeWebhookVerificationError("webhook_timestamp_invalid") from exc
    clock = int(time.time()) if now is None else int(now)
    if tolerance_seconds >= 0 and abs(clock - timestamp_int) > tolerance_seconds:
        raise StripeWebhookVerificationError("webhook_timestamp_outside_tolerance")
    signed_payload = timestamp.encode("utf-8") + b"." + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise StripeWebhookVerificationError("webhook_signature_invalid")


def process_stripe_webhook_event(
    payload: dict[str, Any],
    *,
    received_at: datetime | None = None,
) -> StripeWebhookProcessingResult:
    event = stripe_webhook_event_from_payload(payload, received_at=received_at)
    stripe_customer = _stripe_customer_from_event(event)
    ignored = stripe_customer is None
    return StripeWebhookProcessingResult(event=event, stripe_customer=stripe_customer, ignored=ignored)


def update_workspace_subscription_status_sql(
    *,
    workspace_id: str,
    subscription_status: str,
) -> BillingSqlStatement:
    """Mirror a subscription lifecycle change onto the workspace row.

    The entitlement layer reads workspaces.subscription_status to decide trial-expiry
    read-only. The StripeCustomer model already normalizes Stripe's `trialing` to the entitled
    `active`, so mirroring its status keeps the workspace entitled while subscribed and lets it
    fall back to the trial window once `canceled`/`past_due`/`none`.
    """

    return BillingSqlStatement(
        sql="""
UPDATE workspaces
SET subscription_status = %(subscription_status)s
WHERE id = %(workspace_id)s;
""".strip(),
        params={"workspace_id": workspace_id, "subscription_status": subscription_status},
    )


def provision_starter_entitlement_statements(workspace_id: str) -> tuple[BillingSqlStatement, ...]:
    """Idempotently ensure a subscribed workspace has a usable EntitlementPolicy + seeded
    WorkspaceBalance for the current monthly period.

    Reuses the account-bootstrap starter builders so a workspace that subscribed before ever
    signing in (or whose bootstrap predated entitlement provisioning) still gets the same
    `starter` plan rows. All three upserts are idempotent: the price book and policy upsert by
    their natural keys, and the balance upsert preserves an existing `remaining_units_jsonb`
    (the reserve/settle ledger is the source of truth for the live period), so re-running on a
    `customer.subscription.updated` replay never resets a mid-period balance.
    """

    from yutome.hosted.account import (
        upsert_starter_entitlement_policy_sql,
        upsert_starter_price_book_sql,
        upsert_starter_workspace_balance_sql,
    )

    return tuple(
        _as_billing_statement(statement)
        for statement in (
            upsert_starter_price_book_sql(),
            upsert_starter_entitlement_policy_sql(workspace_id),
            upsert_starter_workspace_balance_sql(workspace_id),
        )
    )


def stripe_webhook_processing_statements(result: StripeWebhookProcessingResult) -> tuple[BillingSqlStatement, ...]:
    statements: list[BillingSqlStatement] = [upsert_stripe_webhook_event_sql(result.event)]
    if result.stripe_customer is not None:
        statements.append(upsert_stripe_customer_sql(result.stripe_customer))
        statements.append(
            update_workspace_subscription_status_sql(
                workspace_id=result.stripe_customer.workspace_id,
                subscription_status=result.stripe_customer.subscription_status,
            )
        )
        # The StripeCustomer model normalizes Stripe `trialing`/`complete` to `active`, so a
        # subscribe/renew event that lands the workspace in the entitled state seeds the starter
        # EntitlementPolicy + WorkspaceBalance for the current period. Without this, a workspace
        # that subscribed before signing in (no account bootstrap) would have no policy/balance
        # and every spend would fail closed.
        if result.stripe_customer.subscription_status == "active":
            statements.extend(provision_starter_entitlement_statements(result.stripe_customer.workspace_id))
    final_event = result.event.model_copy(
        update={
            "status": "skipped" if result.ignored else "succeeded",
            "processed_at": datetime.now(tz=result.event.received_at.tzinfo),
        }
    )
    statements.append(upsert_stripe_webhook_event_sql(final_event))
    return tuple(statements)


def _as_billing_statement(statement: Any) -> BillingSqlStatement:
    return BillingSqlStatement(sql=statement.sql, params=dict(statement.params))


def billing_schema_statements(sql: str = POSTGRES_BILLING_SCHEMA_SQL) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    for raw_line in sql.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        current.append(raw_line)
        if line.endswith(";"):
            statements.append("\n".join(current).strip())
            current = []
    if current:
        statements.append("\n".join(current).strip())
    return statements


def _billing_sql_statement(statement: Any) -> BillingSqlStatement:
    sql, params = compile_postgres_statement(statement)
    return BillingSqlStatement(sql=sql + ";", params=params)


def upsert_price_book_sql(price_book: PriceBook) -> BillingSqlStatement:
    params = price_book_params(price_book)
    statement = insert(price_books).values(
        id=params["id"],
        version=params["version"],
        effective_at=params["effective_at"],
        currency=params["currency"],
        products_jsonb=params["products_jsonb"],
        unit_mapping_jsonb=params["unit_mapping_jsonb"],
        status=params["status"],
        metadata_json=params["metadata_json"],
        created_at=params["created_at"] or func.now(),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[price_books.c.version],
        set_={
            "effective_at": statement.excluded.effective_at,
            "currency": statement.excluded.currency,
            "products_jsonb": statement.excluded.products_jsonb,
            "unit_mapping_jsonb": statement.excluded.unit_mapping_jsonb,
            "status": statement.excluded.status,
            "metadata_json": statement.excluded.metadata_json,
            "updated_at": func.now(),
        },
    ).returning(price_books)
    return _billing_sql_statement(statement)


def upsert_entitlement_policy_sql(policy: EntitlementPolicyRecord) -> BillingSqlStatement:
    params = entitlement_policy_params(policy)
    statement = insert(entitlement_policies).values(
        id=params["id"],
        workspace_id=params["workspace_id"],
        plan_key=params["plan_key"],
        price_book_id=params["price_book_id"],
        allowed_operations=params["allowed_operations"],
        included_units_jsonb=params["included_units_jsonb"],
        hard_limits_jsonb=params["hard_limits_jsonb"],
        soft_limits_jsonb=params["soft_limits_jsonb"],
        grace_policy_jsonb=params["grace_policy_jsonb"],
        status=params["status"],
        metadata_json=params["metadata_json"],
        created_at=params["created_at"] or func.now(),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[
            entitlement_policies.c.workspace_id,
            entitlement_policies.c.plan_key,
            entitlement_policies.c.price_book_id,
        ],
        set_={
            "allowed_operations": statement.excluded.allowed_operations,
            "included_units_jsonb": statement.excluded.included_units_jsonb,
            "hard_limits_jsonb": statement.excluded.hard_limits_jsonb,
            "soft_limits_jsonb": statement.excluded.soft_limits_jsonb,
            "grace_policy_jsonb": statement.excluded.grace_policy_jsonb,
            "status": statement.excluded.status,
            "metadata_json": statement.excluded.metadata_json,
            "updated_at": func.now(),
        },
    ).returning(entitlement_policies)
    return _billing_sql_statement(statement)


def upsert_workspace_balance_sql(balance: WorkspaceBalanceSnapshot) -> BillingSqlStatement:
    params = workspace_balance_params(balance)
    statement = insert(workspace_balances).values(
        workspace_id=params["workspace_id"],
        entitlement_policy_id=params["entitlement_policy_id"],
        period_start_at=params["period_start_at"],
        period_end_at=params["period_end_at"],
        used_units_jsonb=params["used_units_jsonb"],
        reserved_units_jsonb=params["reserved_units_jsonb"],
        remaining_units_jsonb=params["remaining_units_jsonb"],
        unlimited_units=params["unlimited_units"],
        metadata_json=params["metadata_json"],
        updated_at=params["updated_at"] or func.now(),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[workspace_balances.c.workspace_id],
        set_={
            "entitlement_policy_id": statement.excluded.entitlement_policy_id,
            "period_start_at": statement.excluded.period_start_at,
            "period_end_at": statement.excluded.period_end_at,
            "used_units_jsonb": statement.excluded.used_units_jsonb,
            "reserved_units_jsonb": statement.excluded.reserved_units_jsonb,
            "remaining_units_jsonb": statement.excluded.remaining_units_jsonb,
            "unlimited_units": statement.excluded.unlimited_units,
            "metadata_json": statement.excluded.metadata_json,
            "updated_at": statement.excluded.updated_at,
        },
    ).returning(workspace_balances)
    return _billing_sql_statement(statement)


def upsert_stripe_customer_sql(customer: StripeCustomer) -> BillingSqlStatement:
    params = stripe_customer_params(customer)
    statement = insert(stripe_customers).values(
        id=params["id"],
        workspace_id=params["workspace_id"],
        stripe_customer_id=params["stripe_customer_id"],
        stripe_subscription_id=params["stripe_subscription_id"],
        subscription_status=params["subscription_status"],
        subscription_status_snapshot_jsonb=params["subscription_status_snapshot_jsonb"],
        last_webhook_at=params["last_webhook_at"],
        status=params["status"],
        metadata_json=params["metadata_json"],
        created_at=params["created_at"] or func.now(),
        updated_at=params["updated_at"] or func.now(),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[stripe_customers.c.workspace_id],
        set_={
            "stripe_customer_id": statement.excluded.stripe_customer_id,
            "stripe_subscription_id": func.coalesce(
                statement.excluded.stripe_subscription_id, stripe_customers.c.stripe_subscription_id
            ),
            "subscription_status": statement.excluded.subscription_status,
            "subscription_status_snapshot_jsonb": statement.excluded.subscription_status_snapshot_jsonb,
            "last_webhook_at": statement.excluded.last_webhook_at,
            "status": statement.excluded.status,
            "metadata_json": statement.excluded.metadata_json,
            "updated_at": func.now(),
        },
    ).returning(stripe_customers)
    return _billing_sql_statement(statement)


def upsert_stripe_meter_export_sql(export: StripeMeterExportEvent) -> BillingSqlStatement:
    params = stripe_meter_export_params(export)
    statement = insert(stripe_meter_exports).values(
        id=params["id"],
        workspace_id=params["workspace_id"],
        usage_event_id=params["usage_event_id"],
        reservation_id=params["reservation_id"],
        stripe_customer_id=params["stripe_customer_id"],
        meter_unit=params["meter_unit"],
        event_name=params["event_name"],
        value_text=params["value_text"],
        source_event_dedupe_key=params["source_event_dedupe_key"],
        status=params["status"],
        stripe_meter_event_identifier=params["stripe_meter_event_identifier"],
        attempt_count=params["attempt_count"],
        last_error_jsonb=params["last_error_jsonb"],
        metadata_json=params["metadata_json"],
        event_timestamp=params["event_timestamp"],
        exported_at=params["exported_at"],
    )
    statement = statement.on_conflict_do_update(
        index_elements=[stripe_meter_exports.c.source_event_dedupe_key],
        set_={
            "stripe_customer_id": func.coalesce(
                statement.excluded.stripe_customer_id, stripe_meter_exports.c.stripe_customer_id
            ),
            "stripe_meter_event_identifier": func.coalesce(
                statement.excluded.stripe_meter_event_identifier,
                stripe_meter_exports.c.stripe_meter_event_identifier,
            ),
            "status": statement.excluded.status,
            "attempt_count": func.greatest(statement.excluded.attempt_count, stripe_meter_exports.c.attempt_count),
            "last_error_jsonb": statement.excluded.last_error_jsonb,
            "metadata_json": statement.excluded.metadata_json,
            "exported_at": func.coalesce(statement.excluded.exported_at, stripe_meter_exports.c.exported_at),
            "updated_at": func.now(),
        },
    ).returning(stripe_meter_exports)
    return _billing_sql_statement(statement)


def enqueue_stripe_meter_export_sql(export: StripeMeterExportEvent) -> BillingSqlStatement:
    """Insert a pending meter export at usage write time, leaving existing rows untouched."""

    params = stripe_meter_export_params(export)
    statement = insert(stripe_meter_exports).values(
        id=params["id"],
        workspace_id=params["workspace_id"],
        usage_event_id=params["usage_event_id"],
        reservation_id=params["reservation_id"],
        stripe_customer_id=params["stripe_customer_id"],
        meter_unit=params["meter_unit"],
        event_name=params["event_name"],
        value_text=params["value_text"],
        source_event_dedupe_key=params["source_event_dedupe_key"],
        status=params["status"],
        stripe_meter_event_identifier=params["stripe_meter_event_identifier"],
        attempt_count=params["attempt_count"],
        last_error_jsonb=params["last_error_jsonb"],
        metadata_json=params["metadata_json"],
        event_timestamp=params["event_timestamp"],
        exported_at=params["exported_at"],
    )
    statement = statement.on_conflict_do_nothing(
        index_elements=[stripe_meter_exports.c.source_event_dedupe_key]
    )
    return _billing_sql_statement(statement)


def load_stripe_customer_sql(*, workspace_id: str) -> BillingSqlStatement:
    return BillingSqlStatement(
        sql="""
SELECT id,
       workspace_id,
       stripe_customer_id,
       stripe_subscription_id,
       subscription_status,
       status
FROM stripe_customers
WHERE workspace_id = %(workspace_id)s;
""".strip(),
        params={"workspace_id": workspace_id},
    )


def claim_stripe_meter_exports_sql(
    *,
    lease_owner: str,
    now: datetime,
    limit: int = 100,
) -> BillingSqlStatement:
    if limit <= 0:
        raise ValueError("limit must be positive")
    return BillingSqlStatement(
        sql="""
WITH due AS (
    SELECT id
    FROM stripe_meter_exports
    WHERE status IN ('pending', 'failed')
    ORDER BY updated_at ASC, id ASC
    LIMIT %(limit)s
    FOR UPDATE SKIP LOCKED
)
UPDATE stripe_meter_exports AS meter_export
SET status = 'processing',
    attempt_count = meter_export.attempt_count + 1,
    metadata_json = jsonb_set(
        meter_export.metadata_json,
        '{last_claim}',
        jsonb_build_object('lease_owner', %(lease_owner)s::text, 'claimed_at', %(now)s::timestamptz::text),
        true
    ),
    updated_at = %(now)s::timestamptz
FROM due
WHERE meter_export.id = due.id
RETURNING meter_export.*;
""".strip(),
        params={"lease_owner": lease_owner, "now": now, "limit": limit},
    )


def finish_stripe_meter_export_sql(
    *,
    export_id: str,
    now: datetime,
    replay_status: BillingReplayStatus,
    stripe_meter_event_identifier: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> BillingSqlStatement:
    if replay_status == "processing":
        raise ValueError("finish status must be terminal or retryable")
    return BillingSqlStatement(
        sql="""
UPDATE stripe_meter_exports
SET status = %(status)s,
    stripe_meter_event_identifier = COALESCE(%(stripe_meter_event_identifier)s::text, stripe_meter_event_identifier),
    last_error_jsonb = %(last_error_jsonb)s,
    exported_at = CASE WHEN %(status)s = 'succeeded' THEN %(now)s ELSE exported_at END,
    updated_at = %(now)s
WHERE id = %(id)s
RETURNING *;
""".strip(),
        params={
            "id": export_id,
            "now": now,
            "status": replay_status,
            "stripe_meter_event_identifier": stripe_meter_event_identifier,
            "last_error_jsonb": Jsonb(_error_snapshot(error_code, error_message)),
        },
    )


def stripe_meter_export_event_from_row(row: Mapping[str, Any]) -> StripeMeterExportEvent:
    return StripeMeterExportEvent(
        idempotency_key=str(row["id"]),
        usage_event_id=str(row["usage_event_id"]),
        reservation_id=_optional_string(row.get("reservation_id")),
        workspace_id=str(row["workspace_id"]),
        stripe_customer_id=_optional_string(row.get("stripe_customer_id")),
        operation_key=_operation_key_from_export_row(row),
        meter_unit=str(row.get("meter_unit") or CREDITS_UNIT),
        event_name=str(row.get("event_name") or CREDITS_METER_EVENT_NAME),
        value=row.get("value_text") or "0",
        replay_status=row.get("status", "pending"),
        timestamp=row.get("event_timestamp"),
        stripe_meter_event_identifier=_optional_string(row.get("stripe_meter_event_identifier")),
        attempt_count=int(row.get("attempt_count") or 0),
        metadata=dict(row.get("metadata_json") or {}),
    )


def upsert_stripe_webhook_event_sql(event: StripeWebhookEvent) -> BillingSqlStatement:
    params = stripe_webhook_event_params(event)
    statement = insert(stripe_webhook_events).values(
        id=params["id"],
        type=params["type"],
        workspace_id=params["workspace_id"],
        payload_jsonb=params["payload_jsonb"],
        status=params["status"],
        received_at=params["received_at"],
        processed_at=params["processed_at"],
        last_error_jsonb=params["last_error_jsonb"],
    )
    # PK is the Stripe event id, so a replay of the same event is exactly-once: the
    # snapshot insert is a no-op, and only the finalize statement updates status.
    statement = statement.on_conflict_do_update(
        index_elements=[stripe_webhook_events.c.id],
        set_={
            "status": statement.excluded.status,
            "processed_at": func.coalesce(statement.excluded.processed_at, stripe_webhook_events.c.processed_at),
            "last_error_jsonb": statement.excluded.last_error_jsonb,
        },
    ).returning(stripe_webhook_events)
    return _billing_sql_statement(statement)


def billing_debug_snapshot_sql(
    *,
    workspace_id: str,
    limit: int = 20,
    operation: str | None = None,
) -> BillingSqlStatement:
    if limit <= 0:
        raise ValueError("limit must be positive")
    return BillingSqlStatement(
        sql="""
WITH recent_reservations AS (
    SELECT
        reservation.id AS reservation_id,
        reservation.workspace_id,
        job_operation.job_id,
        job.status AS job_status,
        job.error_code AS job_error_code,
        job.error_message AS job_error_message,
        job_operation.id AS operation_id,
        job_operation.operation AS job_operation,
        job_operation.status AS operation_status,
        job_operation.video_id,
        reservation.subject,
        reservation.operation,
        reservation.subject || '.' || reservation.operation AS operation_key,
        reservation.allocation_id,
        reservation.credential_mode,
        reservation.status AS reservation_status,
        reservation.decision_json,
        reservation.estimated_units_json,
        reservation.idempotency_key,
        reservation.created_at,
        reservation.metadata_json
    FROM usage_reservations AS reservation
    LEFT JOIN job_operations AS job_operation
        ON job_operation.workspace_id = reservation.workspace_id
       AND job_operation.usage_reservation_id = reservation.id
    LEFT JOIN jobs AS job
        ON job.workspace_id = reservation.workspace_id
       AND job.id = job_operation.job_id
    WHERE reservation.workspace_id = %(workspace_id)s
      AND (
          %(operation)s::text IS NULL
          OR reservation.subject || '.' || reservation.operation = %(operation)s::text
          OR reservation.operation = %(operation)s::text
          OR job_operation.operation = %(operation)s::text
      )
    ORDER BY reservation.created_at DESC, reservation.id DESC
    LIMIT %(limit)s
)
SELECT
    recent_reservations.*,
    COALESCE(
        (
            SELECT jsonb_agg(
                jsonb_build_object(
                    'id', usage_event.id,
                    'event_type', usage_event.event_type,
                    'status', usage_event.status,
                    'actual_units', usage_event.actual_units_json,
                    'error_code', usage_event.error_code,
                    'provider_request_id', usage_event.provider_request_id,
                    'created_at', usage_event.created_at,
                    'metadata', usage_event.metadata_json
                )
                ORDER BY usage_event.created_at DESC, usage_event.id DESC
            )
            FROM usage_events AS usage_event
            WHERE usage_event.workspace_id = recent_reservations.workspace_id
              AND usage_event.reservation_id = recent_reservations.reservation_id
        ),
        '[]'::jsonb
    ) AS usage_events_json,
    COALESCE(
        (
            SELECT jsonb_agg(
                jsonb_build_object(
                    'id', meter_export.id,
                    'usage_event_id', meter_export.usage_event_id,
                    'replay_status', meter_export.status,
                    'stripe_customer_id', meter_export.stripe_customer_id,
                    'meter_unit', meter_export.meter_unit,
                    'value', meter_export.value_text,
                    'stripe_meter_event_identifier', meter_export.stripe_meter_event_identifier,
                    'source_event_dedupe_key', meter_export.source_event_dedupe_key,
                    'attempt_count', meter_export.attempt_count,
                    'last_error', meter_export.last_error_jsonb,
                    'exported_at', meter_export.exported_at,
                    'updated_at', meter_export.updated_at
                )
                ORDER BY meter_export.updated_at DESC, meter_export.id DESC
            )
            FROM stripe_meter_exports AS meter_export
            WHERE meter_export.workspace_id = recent_reservations.workspace_id
              AND (
                  meter_export.reservation_id = recent_reservations.reservation_id
                  OR meter_export.usage_event_id IN (
                      SELECT usage_event.id
                      FROM usage_events AS usage_event
                      WHERE usage_event.workspace_id = recent_reservations.workspace_id
                        AND usage_event.reservation_id = recent_reservations.reservation_id
                  )
              )
        ),
        '[]'::jsonb
    ) AS meter_exports_json
FROM recent_reservations
ORDER BY created_at DESC, reservation_id DESC;
""".strip(),
        params={"workspace_id": workspace_id, "operation": operation, "limit": limit},
    )


def billing_debug_snapshot_from_rows(
    rows: list[dict[str, Any]],
    *,
    workspace_id: str,
    limit: int,
    operation: str | None = None,
) -> BillingDebugSnapshot:
    return BillingDebugSnapshot(
        workspace_id=workspace_id,
        limit=limit,
        operation=operation,
        rows=tuple(billing_debug_reservation_from_row(row) for row in rows),
    )


def billing_debug_reservation_from_row(row: dict[str, Any]) -> BillingDebugReservation:
    usage_events = tuple(
        BillingDebugUsageEvent.model_validate(event)
        for event in (row.get("usage_events_json") or [])
    )
    meter_exports = tuple(
        BillingDebugExport.model_validate(export)
        for export in (row.get("meter_exports_json") or [])
    )
    subject = str(row["subject"])
    operation = str(row["operation"])
    return BillingDebugReservation(
        reservation_id=str(row["reservation_id"]),
        workspace_id=str(row["workspace_id"]),
        job_id=_optional_string(row.get("job_id")),
        job_status=_optional_string(row.get("job_status")),
        job_error_code=_optional_string(row.get("job_error_code")),
        job_error_message=_optional_string(row.get("job_error_message")),
        operation_id=_optional_string(row.get("operation_id")),
        job_operation=_optional_string(row.get("job_operation")),
        operation_status=_optional_string(row.get("operation_status")),
        video_id=_optional_string(row.get("video_id")),
        subject=subject,
        operation=operation,
        operation_key=_optional_string(row.get("operation_key")) or f"{subject}.{operation}",
        allocation_id=_optional_string(row.get("allocation_id")),
        credential_mode=str(row["credential_mode"]),
        reservation_status=str(row["reservation_status"]),
        entitlement_decision=dict(row.get("decision_json") or {}),
        estimated_units=dict(row.get("estimated_units_json") or {}),
        idempotency_key=str(row["idempotency_key"]),
        created_at=row.get("created_at"),
        metadata=dict(row.get("metadata_json") or {}),
        usage_events=usage_events,
        meter_exports=meter_exports,
    )


def _billable_units(units: dict[str, Any] | Mapping[str, Any]) -> UnitMap:
    numeric: dict[str, UnitQuantity] = {}
    normalized = normalize_usage_units(units)
    for unit, quantity in normalized.items():
        if isinstance(quantity, bool) or quantity is None or isinstance(quantity, str):
            continue
        if unit_quantity_decimal(quantity) < 0:
            raise ValueError(
                f"Negative billing unit {unit} must be modeled through reconciliation, not a meter event."
            )
        numeric[unit] = quantity
    return numeric


def _redacted_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _redacted_metadata_value(item) for key, item in value.items()}


def _redacted_metadata_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_failure_text(value)
    if isinstance(value, Mapping):
        return _redacted_metadata(value)
    if isinstance(value, list):
        return [_redacted_metadata_value(item) for item in value]
    return value


def _negated_units(units: dict[str, UnitQuantity]) -> UnitMap:
    return {unit: -unit_quantity_decimal(quantity) for unit, quantity in units.items()}


def _clamped_remaining_units(units: dict[str, UnitQuantity]) -> tuple[UnitMap, UnitMap]:
    remaining: UnitMap = {}
    overdrawn: dict[str, UnitQuantity] = {}
    for unit, quantity in units.items():
        exact = unit_quantity_decimal(quantity)
        if exact < 0:
            overdrawn[unit] = -exact
        else:
            remaining[unit] = quantity
    return remaining, normalize_unit_map(overdrawn)


def price_book_params(price_book: PriceBook) -> dict[str, Any]:
    return {
        "id": price_book.id,
        "version": price_book.version,
        "effective_at": price_book.effective_at,
        "currency": price_book.currency,
        "products_jsonb": Jsonb(jsonable_exact([product.model_dump(mode="json") for product in price_book.products])),
        "unit_mapping_jsonb": Jsonb(jsonable_exact(price_book.unit_mapping)),
        "status": price_book.status,
        "metadata_json": Jsonb(jsonable_exact(price_book.metadata)),
        "created_at": price_book.created_at,
    }


def entitlement_policy_params(policy: EntitlementPolicyRecord) -> dict[str, Any]:
    return {
        "id": policy.id,
        "workspace_id": policy.workspace_id,
        "plan_key": policy.plan_key,
        "price_book_id": policy.price_book_id,
        "allowed_operations": list(policy.allowed_operations),
        "included_units_jsonb": Jsonb(jsonable_exact(policy.included_units)),
        "hard_limits_jsonb": Jsonb(jsonable_exact(policy.hard_limits)),
        "soft_limits_jsonb": Jsonb(jsonable_exact(policy.soft_limits)),
        "grace_policy_jsonb": Jsonb(jsonable_exact(policy.grace_policy)),
        "status": policy.status,
        "metadata_json": Jsonb(jsonable_exact(policy.metadata)),
        "created_at": policy.created_at,
    }


def workspace_balance_params(balance: WorkspaceBalanceSnapshot) -> dict[str, Any]:
    return {
        "workspace_id": balance.workspace_id,
        "entitlement_policy_id": balance.entitlement_policy_id,
        "period_start_at": balance.period_start_at,
        "period_end_at": balance.period_end_at,
        "used_units_jsonb": Jsonb(jsonable_exact(balance.used_units)),
        "reserved_units_jsonb": Jsonb(jsonable_exact(balance.reserved_units)),
        "remaining_units_jsonb": Jsonb(jsonable_exact(balance.remaining_units)),
        "unlimited_units": list(balance.unlimited_units),
        "metadata_json": Jsonb(jsonable_exact(balance.metadata)),
        "updated_at": balance.updated_at,
    }


def stripe_customer_params(customer: StripeCustomer) -> dict[str, Any]:
    return {
        "id": customer.id,
        "workspace_id": customer.workspace_id,
        "stripe_customer_id": customer.stripe_customer_id,
        "stripe_subscription_id": customer.stripe_subscription_id,
        "subscription_status": customer.subscription_status,
        "subscription_status_snapshot_jsonb": Jsonb(jsonable_exact(customer.subscription_status_snapshot)),
        "last_webhook_at": customer.last_webhook_at,
        "status": customer.status,
        "metadata_json": Jsonb(jsonable_exact(customer.metadata)),
        "created_at": customer.created_at,
        "updated_at": customer.updated_at,
    }


def stripe_meter_export_params(export: StripeMeterExportEvent) -> dict[str, Any]:
    return {
        "id": export.idempotency_key,
        "workspace_id": export.workspace_id,
        "usage_event_id": export.usage_event_id,
        "reservation_id": export.reservation_id,
        "stripe_customer_id": export.stripe_customer_id,
        "meter_unit": export.meter_unit,
        "event_name": export.event_name,
        "value_text": str(export.value_decimal),
        "source_event_dedupe_key": export.source_event_dedupe_key,
        "status": export.replay_status,
        "stripe_meter_event_identifier": export.stripe_meter_event_identifier,
        "attempt_count": export.attempt_count,
        "last_error_jsonb": Jsonb(_error_snapshot(export.last_error_code, export.last_error_message)),
        "metadata_json": Jsonb(jsonable_exact(export.metadata)),
        "event_timestamp": export.timestamp,
        "exported_at": export.exported_at,
    }


def stripe_webhook_event_params(event: StripeWebhookEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "type": event.type,
        "workspace_id": event.workspace_id,
        "payload_jsonb": Jsonb(jsonable_exact(event.payload)),
        "status": event.status,
        "received_at": event.received_at,
        "processed_at": event.processed_at,
        "last_error_jsonb": Jsonb(_error_snapshot(event.last_error_code, event.last_error_message)),
    }


def stripe_customer_id(*, stripe_customer_id: str) -> str:
    return input_hash({"provider": "stripe", "stripe_customer_id": stripe_customer_id}, prefix="sc")


def _error_snapshot(code: str | None, message: str | None) -> dict[str, str]:
    error: dict[str, str] = {}
    if code is not None:
        error["code"] = code
    if message is not None:
        error["message"] = message
    return error


def _first_mapping(data: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return None


def _units_from_json_row(row: Mapping[str, Any], key: str) -> UnitMap:
    return _billable_units(dict(row.get(key) or {}))


def _operation_key_from_export_row(row: Mapping[str, Any]) -> str:
    metadata = dict(row.get("metadata_json") or {})
    operation_key = metadata.get("operation_key")
    if operation_key:
        return str(operation_key)
    source_dedupe = str(row.get("source_event_dedupe_key") or "")
    parts = source_dedupe.split(":")
    if len(parts) >= 3:
        return parts[2]
    return "usage"


def _event_object(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, Mapping):
        obj = data.get("object")
        if isinstance(obj, Mapping):
            return dict(obj)
    return {}


def _event_received_at(payload: dict[str, Any]) -> datetime:
    from datetime import timezone

    created = payload.get("created")
    if isinstance(created, (int, float)):
        return datetime.fromtimestamp(int(created), tz=timezone.utc)
    return datetime.now(tz=timezone.utc)


def _workspace_id_from_event(payload: dict[str, Any]) -> str | None:
    obj = _event_object(payload)
    metadata = _first_mapping(obj, "metadata") or {}
    subscription_data = _first_mapping(obj, "subscription_data") or {}
    subscription_metadata = _first_mapping(subscription_data, "metadata") or {}
    return _optional_string(
        metadata.get("workspace_id")
        or subscription_metadata.get("workspace_id")
        or obj.get("client_reference_id")
    )


def _stripe_customer_from_event(event: StripeWebhookEvent) -> StripeCustomer | None:
    if event.type == "checkout.session.completed":
        return _customer_from_checkout_session(event)
    if event.type in {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }:
        return _customer_from_subscription(event)
    return None


def _customer_from_checkout_session(event: StripeWebhookEvent) -> StripeCustomer | None:
    obj = _event_object(event.payload)
    stripe_customer_external_id = _optional_string(obj.get("customer"))
    workspace_id = event.workspace_id
    if stripe_customer_external_id is None or workspace_id is None:
        return None
    subscription_id = _optional_string(obj.get("subscription"))
    status = _subscription_status(str(obj.get("status") or "active"))
    return StripeCustomer(
        id=stripe_customer_id(stripe_customer_id=stripe_customer_external_id),
        workspace_id=workspace_id,
        stripe_customer_id=stripe_customer_external_id,
        stripe_subscription_id=subscription_id,
        subscription_status=status,
        subscription_status_snapshot={"event_type": event.type, "checkout_session": obj},
        last_webhook_at=event.received_at,
        status=_record_status(status),
        metadata={"last_stripe_event_type": event.type, "webhook_event_id": event.id},
    )


def _customer_from_subscription(event: StripeWebhookEvent) -> StripeCustomer | None:
    obj = _event_object(event.payload)
    stripe_customer_external_id = _optional_string(obj.get("customer"))
    workspace_id = event.workspace_id
    if stripe_customer_external_id is None or workspace_id is None:
        return None
    subscription_id = _optional_string(obj.get("id"))
    raw_status = "canceled" if event.type == "customer.subscription.deleted" else str(obj.get("status") or "")
    status = _subscription_status(raw_status)
    return StripeCustomer(
        id=stripe_customer_id(stripe_customer_id=stripe_customer_external_id),
        workspace_id=workspace_id,
        stripe_customer_id=stripe_customer_external_id,
        stripe_subscription_id=subscription_id,
        subscription_status=status,
        subscription_status_snapshot={"event_type": event.type, "subscription": obj},
        last_webhook_at=event.received_at,
        status=_record_status(status),
        metadata={"last_stripe_event_type": event.type, "webhook_event_id": event.id},
    )


def _subscription_status(raw: str) -> SubscriptionStatus:
    normalized = raw.strip().lower()
    if normalized in {"active", "trialing", "complete"}:
        return "active"
    if normalized in {"past_due", "unpaid", "incomplete", "incomplete_expired"}:
        return "past_due"
    if normalized in {"canceled", "cancelled"}:
        return "canceled"
    return "none"


def _record_status(subscription_status: SubscriptionStatus) -> BillingRecordStatus:
    if subscription_status == "active":
        return "active"
    if subscription_status == "past_due":
        return "paused"
    if subscription_status == "canceled":
        return "archived"
    return "active"


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


__all__ = [
    "BillingDebugExport",
    "BillingDebugReservation",
    "BillingDebugSnapshot",
    "BillingDebugUsageEvent",
    "BillingProvider",
    "BillingRecordStatus",
    "BillingReplayStatus",
    "BillingSqlStatement",
    "CREDITS_METER_EVENT_NAME",
    "CREDITS_METER_ID_ENV_VAR",
    "CREDITS_UNIT",
    "EntitlementPolicyRecord",
    "POSTGRES_BILLING_SCHEMA_SQL",
    "PriceBook",
    "PriceBookProduct",
    "ProductLimit",
    "STRIPE_CREDIT_UNIT_WEIGHTS",
    "StripeCustomer",
    "StripeMeterExportEvent",
    "StripeMeterExportWorkerResult",
    "StripeWebhookEvent",
    "StripeWebhookProcessingResult",
    "StripeWebhookVerificationError",
    "SubscriptionStatus",
    "WorkspaceBalanceSnapshot",
    "balance_reconciliation_input_sql",
    "billing_debug_reservation_from_row",
    "billing_debug_snapshot_from_rows",
    "billing_debug_snapshot_sql",
    "billing_schema_statements",
    "claim_stripe_meter_exports_sql",
    "credits_from_billable_units",
    "derive_workspace_balance_snapshot",
    "derive_workspace_balance_snapshot_from_rows",
    "enqueue_stripe_meter_export_sql",
    "entitlement_policy_params",
    "finish_stripe_meter_export_sql",
    "included_allowance_credits",
    "load_stripe_customer_sql",
    "mark_stripe_meter_export_replay",
    "overage_credits_for_event",
    "overage_metering_enabled",
    "price_book_params",
    "process_stripe_webhook_event",
    "provision_starter_entitlement_statements",
    "stripe_customer_id",
    "stripe_customer_params",
    "stripe_meter_event_payload",
    "stripe_meter_export_event_from_row",
    "stripe_meter_export_idempotency_key",
    "stripe_meter_export_params",
    "stripe_meter_exports_from_usage_event",
    "stripe_webhook_event_from_payload",
    "stripe_webhook_event_params",
    "stripe_webhook_processing_statements",
    "update_workspace_subscription_status_sql",
    "upsert_entitlement_policy_sql",
    "upsert_price_book_sql",
    "upsert_stripe_customer_sql",
    "upsert_stripe_meter_export_sql",
    "upsert_stripe_webhook_event_sql",
    "upsert_workspace_balance_sql",
    "verify_stripe_webhook_signature",
    "workspace_balance_params",
]
