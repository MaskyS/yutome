from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from yutome.hosted.ids import input_hash
from yutome.hosted.models import EntitlementPolicy, UsageEvent, WorkspaceBalance


BillingProvider = Literal["polar"]
BillingReplayStatus = Literal["pending", "succeeded", "failed", "skipped"]
BillingRecordStatus = Literal["draft", "active", "paused", "archived"]


@dataclass(frozen=True)
class BillingSqlStatement:
    sql: str
    params: dict[str, Any]


class ProductLimit(BaseModel):
    product_code: str
    operation_key: str
    unit: str
    included_quantity: float | None = None
    hard_limit: float | None = None
    polar_meter_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PriceBookProduct(BaseModel):
    code: str
    name: str
    polar_product_id: str | None = None
    polar_price_id: str | None = None
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
    included_units: dict[str, dict[str, float]] = Field(default_factory=dict)
    hard_limits: dict[str, dict[str, float]] = Field(default_factory=dict)
    soft_limits: dict[str, dict[str, float]] = Field(default_factory=dict)
    grace_policy: dict[str, Any] = Field(default_factory=dict)
    status: BillingRecordStatus = "active"
    created_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_runtime_policy(self) -> EntitlementPolicy:
        return EntitlementPolicy(
            id=self.id,
            workspace_id=self.workspace_id,
            allow_all_operations=False,
            allowed_operations=set(self.allowed_operations),
            max_units_by_operation=dict(self.hard_limits),
        )


class WorkspaceBalanceSnapshot(BaseModel):
    workspace_id: str
    entitlement_policy_id: str
    period_start_at: datetime
    period_end_at: datetime
    used_units: dict[str, float] = Field(default_factory=dict)
    reserved_units: dict[str, float] = Field(default_factory=dict)
    remaining_units: dict[str, float] = Field(default_factory=dict)
    unlimited_units: tuple[str, ...] = ()
    updated_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_runtime_balance(self) -> WorkspaceBalance:
        return WorkspaceBalance(
            workspace_id=self.workspace_id,
            remaining_units=dict(self.remaining_units),
            unlimited_units=set(self.unlimited_units),
        )


class BillingCustomer(BaseModel):
    id: str
    workspace_id: str
    provider: BillingProvider = "polar"
    external_customer_id: str
    external_subscription_id: str | None = None
    subscription_status_snapshot: dict[str, Any] = Field(default_factory=dict)
    last_webhook_at: datetime | None = None
    status: BillingRecordStatus = "active"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolarEventIngestionEvent(BaseModel):
    name: str
    external_customer_id: str | None = None
    customer_id: str | None = None
    external_id: str | None = None
    timestamp: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolarUsageExport(BaseModel):
    events: tuple[PolarEventIngestionEvent, ...]
    idempotency_key: str
    source: str = "yutome_usage_ledger"


class PolarWebhookEvent(BaseModel):
    type: str
    timestamp: datetime
    data: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class PolarWebhookSnapshot(BaseModel):
    id: str
    event_type: str
    webhook_event_id: str | None = None
    workspace_id: str | None = None
    external_customer_id: str | None = None
    external_subscription_id: str | None = None
    customer_state_snapshot: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    replay_status: BillingReplayStatus = "pending"
    received_at: datetime
    processed_at: datetime | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None


class BillingExportEvent(BaseModel):
    idempotency_key: str
    provider: BillingProvider = "polar"
    usage_event_id: str
    reservation_id: str | None = None
    workspace_id: str
    billing_customer_id: str | None = None
    price_book_id: str | None = None
    operation_key: str
    event_name: str
    external_meter_key: str | None = None
    replay_status: BillingReplayStatus = "pending"
    authorization_effect: Literal["none"] = "none"
    actual_units: dict[str, float] = Field(default_factory=dict)
    external_customer_id: str | None = None
    customer_id: str | None = None
    timestamp: datetime
    provider_event_id: str | None = None
    external_event_id: str | None = None
    attempt_count: int = 0
    last_error_code: str | None = None
    last_error_message: str | None = None
    exported_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_polar_event(self) -> PolarEventIngestionEvent:
        metadata = dict(self.metadata)
        metadata.update(self.actual_units)
        metadata["billing_export_idempotency_key"] = self.idempotency_key
        metadata["source_event_dedupe_key"] = self.source_event_dedupe_key
        if self.price_book_id is not None:
            metadata["price_book_id"] = self.price_book_id
        if self.external_meter_key is not None:
            metadata["external_meter_key"] = self.external_meter_key
        return PolarEventIngestionEvent(
            name=self.event_name,
            external_customer_id=self.external_customer_id,
            customer_id=self.customer_id,
            external_id=self.source_event_dedupe_key,
            timestamp=self.timestamp,
            metadata=metadata,
        )

    def to_polar_export(self) -> PolarUsageExport:
        return PolarUsageExport(events=(self.to_polar_event(),), idempotency_key=self.idempotency_key)

    @property
    def source_event_dedupe_key(self) -> str:
        return f"{self.provider}:{self.usage_event_id}:{self.operation_key}"


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
    provider: BillingProvider = "polar"
    replay_status: str
    external_customer_id: str | None = None
    customer_id: str | None = None
    external_meter_key: str | None = None
    external_event_id: str | None = None
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
    allocation_kind: str
    reservation_status: str
    entitlement_decision: dict[str, Any] = Field(default_factory=dict)
    estimated_units: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    created_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    usage_events: tuple[BillingDebugUsageEvent, ...] = ()
    billing_exports: tuple[BillingDebugExport, ...] = ()


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

CREATE TABLE IF NOT EXISTS billing_customers (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    provider text NOT NULL,
    external_customer_id text NOT NULL,
    external_subscription_id text,
    subscription_status_snapshot_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_webhook_at timestamptz,
    status text NOT NULL DEFAULT 'active',
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, provider),
    UNIQUE(provider, external_customer_id)
);

CREATE TABLE IF NOT EXISTS billing_exports (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    usage_event_id text NOT NULL REFERENCES usage_events(id),
    reservation_id text REFERENCES usage_reservations(id),
    billing_customer_id text REFERENCES billing_customers(id),
    price_book_id text REFERENCES price_books(id),
    provider text NOT NULL,
    external_customer_id text,
    customer_id text,
    external_meter_key text,
    external_event_id text,
    event_name text NOT NULL,
    export_units_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_event_dedupe_key text NOT NULL,
    status text NOT NULL DEFAULT 'pending',
    authorization_effect text NOT NULL DEFAULT 'none',
    attempt_count integer NOT NULL DEFAULT 0,
    last_error_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    event_timestamp timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    exported_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(provider, source_event_dedupe_key),
    UNIQUE(provider, external_event_id)
);

CREATE TABLE IF NOT EXISTS polar_webhook_snapshots (
    id text PRIMARY KEY,
    webhook_event_id text,
    event_type text NOT NULL,
    workspace_id text REFERENCES workspaces(id),
    external_customer_id text,
    external_subscription_id text,
    customer_state_snapshot_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    payload_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    replay_status text NOT NULL DEFAULT 'pending',
    received_at timestamptz NOT NULL,
    processed_at timestamptz,
    last_error_jsonb jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(webhook_event_id)
);

CREATE INDEX IF NOT EXISTS idx_billing_exports_replay
    ON billing_exports(status, updated_at)
    WHERE status IN ('pending', 'failed');
CREATE INDEX IF NOT EXISTS idx_polar_webhook_snapshots_replay
    ON polar_webhook_snapshots(replay_status, received_at)
    WHERE replay_status IN ('pending', 'failed');
""".strip()


def billing_export_idempotency_key(
    event: UsageEvent,
    *,
    provider: BillingProvider = "polar",
    version: str = "v1",
) -> str:
    return input_hash(
        {
            "provider": provider,
            "version": version,
            "usage_event_id": event.id,
        },
        prefix="bill",
    )


def billing_export_event_from_usage_event(
    event: UsageEvent,
    *,
    event_name: str | None = None,
    billing_customer_id: str | None = None,
    external_customer_id: str | None = None,
    customer_id: str | None = None,
    price_book_id: str | None = None,
    price_book_version: str | None = None,
    product_code: str | None = None,
    external_meter_key: str | None = None,
) -> BillingExportEvent:
    replay_status: BillingReplayStatus = "pending" if event.status in {"succeeded", "released"} else "skipped"
    metadata = {
        "usage_event_id": event.id,
        "reservation_id": event.reservation_id,
        "workspace_id": event.workspace_id,
        "subject": event.subject,
        "operation": event.operation,
        "operation_key": event.operation_key,
        "event_type": event.event_type,
        "status": event.status,
        "provider_request_id": event.provider_request_id,
    }
    if price_book_version is not None:
        metadata["price_book_version"] = price_book_version
    if product_code is not None:
        metadata["product_code"] = product_code
    metadata.update(event.metadata)

    return BillingExportEvent(
        idempotency_key=billing_export_idempotency_key(event),
        usage_event_id=event.id,
        reservation_id=event.reservation_id,
        workspace_id=event.workspace_id,
        billing_customer_id=billing_customer_id,
        price_book_id=price_book_id,
        operation_key=event.operation_key,
        event_name=event_name or f"yutome.{event.operation_key}",
        external_meter_key=external_meter_key,
        replay_status=replay_status,
        actual_units=_numeric_units(event.actual_units),
        external_customer_id=external_customer_id or event.workspace_id,
        customer_id=customer_id,
        timestamp=event.created_at,
        metadata={key: value for key, value in metadata.items() if value is not None},
    )


def mark_billing_export_replay(
    export: BillingExportEvent,
    *,
    replay_status: BillingReplayStatus,
    provider_event_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> BillingExportEvent:
    return export.model_copy(
        update={
            "replay_status": replay_status,
            "provider_event_id": provider_event_id,
            "external_event_id": provider_event_id,
            "last_error_code": error_code,
            "last_error_message": error_message,
            "attempt_count": export.attempt_count + 1,
        }
    )


def polar_webhook_event_from_payload(payload: dict[str, Any]) -> PolarWebhookEvent:
    return PolarWebhookEvent(
        type=str(payload["type"]),
        timestamp=payload["timestamp"],
        data=dict(payload.get("data") or {}),
        raw=dict(payload),
    )


def polar_webhook_snapshot_from_payload(
    payload: dict[str, Any],
    *,
    workspace_id: str | None = None,
    received_at: datetime | None = None,
) -> PolarWebhookSnapshot:
    event = polar_webhook_event_from_payload(payload)
    data = event.data
    customer = _first_mapping(data, "customer", "customer_state") or {}
    subscription = _first_mapping(data, "subscription") or {}
    webhook_event_id = _optional_string(payload.get("id") or payload.get("event_id"))

    return PolarWebhookSnapshot(
        id=polar_webhook_snapshot_id(payload),
        webhook_event_id=webhook_event_id,
        event_type=event.type,
        workspace_id=workspace_id or _optional_string(customer.get("external_id") or customer.get("external_customer_id")),
        external_customer_id=_optional_string(customer.get("id") or data.get("customer_id") or data.get("external_customer_id")),
        external_subscription_id=_optional_string(subscription.get("id") or data.get("subscription_id")),
        customer_state_snapshot=dict(data) if event.type == "customer.state_changed" else {},
        payload=event.raw,
        received_at=received_at or event.timestamp,
    )


def polar_webhook_snapshot_id(payload: dict[str, Any]) -> str:
    event_id = payload.get("id") or payload.get("event_id")
    if event_id is not None:
        return f"polar_wh_{event_id}"
    return input_hash(payload, prefix="polar_wh")


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


def upsert_price_book_sql(price_book: PriceBook) -> BillingSqlStatement:
    return BillingSqlStatement(
        sql="""
INSERT INTO price_books (
    id,
    version,
    effective_at,
    currency,
    products_jsonb,
    unit_mapping_jsonb,
    status,
    metadata_json,
    created_at
)
VALUES (
    %(id)s,
    %(version)s,
    %(effective_at)s,
    %(currency)s,
    %(products_jsonb)s::jsonb,
    %(unit_mapping_jsonb)s::jsonb,
    %(status)s,
    %(metadata_json)s::jsonb,
    COALESCE(%(created_at)s, now())
)
ON CONFLICT (version) DO UPDATE
SET effective_at = EXCLUDED.effective_at,
    currency = EXCLUDED.currency,
    products_jsonb = EXCLUDED.products_jsonb,
    unit_mapping_jsonb = EXCLUDED.unit_mapping_jsonb,
    status = EXCLUDED.status,
    metadata_json = EXCLUDED.metadata_json,
    updated_at = now()
RETURNING *;
""".strip(),
        params=price_book_params(price_book),
    )


def upsert_entitlement_policy_sql(policy: EntitlementPolicyRecord) -> BillingSqlStatement:
    return BillingSqlStatement(
        sql="""
INSERT INTO entitlement_policies (
    id,
    workspace_id,
    plan_key,
    price_book_id,
    allowed_operations,
    included_units_jsonb,
    hard_limits_jsonb,
    soft_limits_jsonb,
    grace_policy_jsonb,
    status,
    metadata_json,
    created_at
)
VALUES (
    %(id)s,
    %(workspace_id)s,
    %(plan_key)s,
    %(price_book_id)s,
    %(allowed_operations)s,
    %(included_units_jsonb)s::jsonb,
    %(hard_limits_jsonb)s::jsonb,
    %(soft_limits_jsonb)s::jsonb,
    %(grace_policy_jsonb)s::jsonb,
    %(status)s,
    %(metadata_json)s::jsonb,
    COALESCE(%(created_at)s, now())
)
ON CONFLICT (workspace_id, plan_key, price_book_id) DO UPDATE
SET allowed_operations = EXCLUDED.allowed_operations,
    included_units_jsonb = EXCLUDED.included_units_jsonb,
    hard_limits_jsonb = EXCLUDED.hard_limits_jsonb,
    soft_limits_jsonb = EXCLUDED.soft_limits_jsonb,
    grace_policy_jsonb = EXCLUDED.grace_policy_jsonb,
    status = EXCLUDED.status,
    metadata_json = EXCLUDED.metadata_json,
    updated_at = now()
RETURNING *;
""".strip(),
        params=entitlement_policy_params(policy),
    )


def upsert_workspace_balance_sql(balance: WorkspaceBalanceSnapshot) -> BillingSqlStatement:
    return BillingSqlStatement(
        sql="""
INSERT INTO workspace_balances (
    workspace_id,
    entitlement_policy_id,
    period_start_at,
    period_end_at,
    used_units_jsonb,
    reserved_units_jsonb,
    remaining_units_jsonb,
    unlimited_units,
    metadata_json,
    updated_at
)
VALUES (
    %(workspace_id)s,
    %(entitlement_policy_id)s,
    %(period_start_at)s,
    %(period_end_at)s,
    %(used_units_jsonb)s::jsonb,
    %(reserved_units_jsonb)s::jsonb,
    %(remaining_units_jsonb)s::jsonb,
    %(unlimited_units)s,
    %(metadata_json)s::jsonb,
    COALESCE(%(updated_at)s, now())
)
ON CONFLICT (workspace_id) DO UPDATE
SET entitlement_policy_id = EXCLUDED.entitlement_policy_id,
    period_start_at = EXCLUDED.period_start_at,
    period_end_at = EXCLUDED.period_end_at,
    used_units_jsonb = EXCLUDED.used_units_jsonb,
    reserved_units_jsonb = EXCLUDED.reserved_units_jsonb,
    remaining_units_jsonb = EXCLUDED.remaining_units_jsonb,
    unlimited_units = EXCLUDED.unlimited_units,
    metadata_json = EXCLUDED.metadata_json,
    updated_at = EXCLUDED.updated_at
RETURNING *;
""".strip(),
        params=workspace_balance_params(balance),
    )


def upsert_billing_customer_sql(customer: BillingCustomer) -> BillingSqlStatement:
    return BillingSqlStatement(
        sql="""
INSERT INTO billing_customers (
    id,
    workspace_id,
    provider,
    external_customer_id,
    external_subscription_id,
    subscription_status_snapshot_jsonb,
    last_webhook_at,
    status,
    metadata_json,
    created_at,
    updated_at
)
VALUES (
    %(id)s,
    %(workspace_id)s,
    %(provider)s,
    %(external_customer_id)s,
    %(external_subscription_id)s,
    %(subscription_status_snapshot_jsonb)s::jsonb,
    %(last_webhook_at)s,
    %(status)s,
    %(metadata_json)s::jsonb,
    COALESCE(%(created_at)s, now()),
    COALESCE(%(updated_at)s, now())
)
ON CONFLICT (workspace_id, provider) DO UPDATE
SET external_customer_id = EXCLUDED.external_customer_id,
    external_subscription_id = EXCLUDED.external_subscription_id,
    subscription_status_snapshot_jsonb = EXCLUDED.subscription_status_snapshot_jsonb,
    last_webhook_at = EXCLUDED.last_webhook_at,
    status = EXCLUDED.status,
    metadata_json = EXCLUDED.metadata_json,
    updated_at = now()
RETURNING *;
""".strip(),
        params=billing_customer_params(customer),
    )


def upsert_billing_export_sql(export: BillingExportEvent) -> BillingSqlStatement:
    return BillingSqlStatement(
        sql="""
INSERT INTO billing_exports (
    id,
    workspace_id,
    usage_event_id,
    reservation_id,
    billing_customer_id,
    price_book_id,
    provider,
    external_customer_id,
    customer_id,
    external_meter_key,
    external_event_id,
    event_name,
    export_units_jsonb,
    source_event_dedupe_key,
    status,
    authorization_effect,
    attempt_count,
    last_error_jsonb,
    metadata_json,
    event_timestamp,
    exported_at
)
VALUES (
    %(id)s,
    %(workspace_id)s,
    %(usage_event_id)s,
    %(reservation_id)s,
    %(billing_customer_id)s,
    %(price_book_id)s,
    %(provider)s,
    %(external_customer_id)s,
    %(customer_id)s,
    %(external_meter_key)s,
    %(external_event_id)s,
    %(event_name)s,
    %(export_units_jsonb)s::jsonb,
    %(source_event_dedupe_key)s,
    %(status)s,
    %(authorization_effect)s,
    %(attempt_count)s,
    %(last_error_jsonb)s::jsonb,
    %(metadata_json)s::jsonb,
    %(event_timestamp)s,
    %(exported_at)s
)
ON CONFLICT (provider, source_event_dedupe_key) DO UPDATE
SET external_event_id = COALESCE(EXCLUDED.external_event_id, billing_exports.external_event_id),
    status = EXCLUDED.status,
    attempt_count = GREATEST(EXCLUDED.attempt_count, billing_exports.attempt_count),
    last_error_jsonb = EXCLUDED.last_error_jsonb,
    metadata_json = EXCLUDED.metadata_json,
    exported_at = COALESCE(EXCLUDED.exported_at, billing_exports.exported_at),
    updated_at = now()
RETURNING *;
""".strip(),
        params=billing_export_params(export),
    )


def upsert_polar_webhook_snapshot_sql(snapshot: PolarWebhookSnapshot) -> BillingSqlStatement:
    return BillingSqlStatement(
        sql="""
INSERT INTO polar_webhook_snapshots (
    id,
    webhook_event_id,
    event_type,
    workspace_id,
    external_customer_id,
    external_subscription_id,
    customer_state_snapshot_jsonb,
    payload_jsonb,
    replay_status,
    received_at,
    processed_at,
    last_error_jsonb
)
VALUES (
    %(id)s,
    %(webhook_event_id)s,
    %(event_type)s,
    %(workspace_id)s,
    %(external_customer_id)s,
    %(external_subscription_id)s,
    %(customer_state_snapshot_jsonb)s::jsonb,
    %(payload_jsonb)s::jsonb,
    %(replay_status)s,
    %(received_at)s,
    %(processed_at)s,
    %(last_error_jsonb)s::jsonb
)
ON CONFLICT (id) DO UPDATE
SET replay_status = EXCLUDED.replay_status,
    processed_at = COALESCE(EXCLUDED.processed_at, polar_webhook_snapshots.processed_at),
    last_error_jsonb = EXCLUDED.last_error_jsonb
RETURNING *;
""".strip(),
        params=polar_webhook_snapshot_params(snapshot),
    )


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
        reservation.allocation_kind,
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
                    'id', billing_export.id,
                    'usage_event_id', billing_export.usage_event_id,
                    'provider', billing_export.provider,
                    'replay_status', billing_export.status,
                    'external_customer_id', billing_export.external_customer_id,
                    'customer_id', billing_export.customer_id,
                    'external_meter_key', billing_export.external_meter_key,
                    'external_event_id', billing_export.external_event_id,
                    'source_event_dedupe_key', billing_export.source_event_dedupe_key,
                    'attempt_count', billing_export.attempt_count,
                    'last_error', billing_export.last_error_jsonb,
                    'exported_at', billing_export.exported_at,
                    'updated_at', billing_export.updated_at
                )
                ORDER BY billing_export.updated_at DESC, billing_export.id DESC
            )
            FROM billing_exports AS billing_export
            WHERE billing_export.workspace_id = recent_reservations.workspace_id
              AND (
                  billing_export.reservation_id = recent_reservations.reservation_id
                  OR billing_export.usage_event_id IN (
                      SELECT usage_event.id
                      FROM usage_events AS usage_event
                      WHERE usage_event.workspace_id = recent_reservations.workspace_id
                        AND usage_event.reservation_id = recent_reservations.reservation_id
                  )
              )
        ),
        '[]'::jsonb
    ) AS billing_exports_json
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
        for event in _json_value(row.get("usage_events_json"), default=[])
    )
    billing_exports = tuple(
        BillingDebugExport.model_validate(export)
        for export in _json_value(row.get("billing_exports_json"), default=[])
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
        allocation_kind=str(row["allocation_kind"]),
        reservation_status=str(row["reservation_status"]),
        entitlement_decision=dict(_json_value(row.get("decision_json"))),
        estimated_units=dict(_json_value(row.get("estimated_units_json"))),
        idempotency_key=str(row["idempotency_key"]),
        created_at=row.get("created_at"),
        metadata=dict(_json_value(row.get("metadata_json"))),
        usage_events=usage_events,
        billing_exports=billing_exports,
    )


def _numeric_units(units: dict[str, float | int | str | bool | None]) -> dict[str, float]:
    return {unit: float(quantity) for unit, quantity in units.items() if isinstance(quantity, (int, float)) and not isinstance(quantity, bool)}


def price_book_params(price_book: PriceBook) -> dict[str, Any]:
    return {
        "id": price_book.id,
        "version": price_book.version,
        "effective_at": price_book.effective_at,
        "currency": price_book.currency,
        "products_jsonb": _json_param([product.model_dump(mode="json") for product in price_book.products]),
        "unit_mapping_jsonb": _json_param(price_book.unit_mapping),
        "status": price_book.status,
        "metadata_json": _json_param(price_book.metadata),
        "created_at": price_book.created_at,
    }


def entitlement_policy_params(policy: EntitlementPolicyRecord) -> dict[str, Any]:
    return {
        "id": policy.id,
        "workspace_id": policy.workspace_id,
        "plan_key": policy.plan_key,
        "price_book_id": policy.price_book_id,
        "allowed_operations": list(policy.allowed_operations),
        "included_units_jsonb": _json_param(policy.included_units),
        "hard_limits_jsonb": _json_param(policy.hard_limits),
        "soft_limits_jsonb": _json_param(policy.soft_limits),
        "grace_policy_jsonb": _json_param(policy.grace_policy),
        "status": policy.status,
        "metadata_json": _json_param(policy.metadata),
        "created_at": policy.created_at,
    }


def workspace_balance_params(balance: WorkspaceBalanceSnapshot) -> dict[str, Any]:
    return {
        "workspace_id": balance.workspace_id,
        "entitlement_policy_id": balance.entitlement_policy_id,
        "period_start_at": balance.period_start_at,
        "period_end_at": balance.period_end_at,
        "used_units_jsonb": _json_param(balance.used_units),
        "reserved_units_jsonb": _json_param(balance.reserved_units),
        "remaining_units_jsonb": _json_param(balance.remaining_units),
        "unlimited_units": list(balance.unlimited_units),
        "metadata_json": _json_param(balance.metadata),
        "updated_at": balance.updated_at,
    }


def billing_customer_params(customer: BillingCustomer) -> dict[str, Any]:
    return {
        "id": customer.id,
        "workspace_id": customer.workspace_id,
        "provider": customer.provider,
        "external_customer_id": customer.external_customer_id,
        "external_subscription_id": customer.external_subscription_id,
        "subscription_status_snapshot_jsonb": _json_param(customer.subscription_status_snapshot),
        "last_webhook_at": customer.last_webhook_at,
        "status": customer.status,
        "metadata_json": _json_param(customer.metadata),
        "created_at": customer.created_at,
        "updated_at": customer.updated_at,
    }


def billing_export_params(export: BillingExportEvent) -> dict[str, Any]:
    return {
        "id": export.idempotency_key,
        "workspace_id": export.workspace_id,
        "usage_event_id": export.usage_event_id,
        "reservation_id": export.reservation_id,
        "billing_customer_id": export.billing_customer_id,
        "price_book_id": export.price_book_id,
        "provider": export.provider,
        "external_customer_id": export.external_customer_id,
        "customer_id": export.customer_id,
        "external_meter_key": export.external_meter_key,
        "external_event_id": export.external_event_id or export.provider_event_id,
        "event_name": export.event_name,
        "export_units_jsonb": _json_param(export.actual_units),
        "source_event_dedupe_key": export.source_event_dedupe_key,
        "status": export.replay_status,
        "authorization_effect": export.authorization_effect,
        "attempt_count": export.attempt_count,
        "last_error_jsonb": _json_param(_error_snapshot(export.last_error_code, export.last_error_message)),
        "metadata_json": _json_param(export.metadata),
        "event_timestamp": export.timestamp,
        "exported_at": export.exported_at,
    }


def polar_webhook_snapshot_params(snapshot: PolarWebhookSnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "webhook_event_id": snapshot.webhook_event_id,
        "event_type": snapshot.event_type,
        "workspace_id": snapshot.workspace_id,
        "external_customer_id": snapshot.external_customer_id,
        "external_subscription_id": snapshot.external_subscription_id,
        "customer_state_snapshot_jsonb": _json_param(snapshot.customer_state_snapshot),
        "payload_jsonb": _json_param(snapshot.payload),
        "replay_status": snapshot.replay_status,
        "received_at": snapshot.received_at,
        "processed_at": snapshot.processed_at,
        "last_error_jsonb": _json_param(_error_snapshot(snapshot.last_error_code, snapshot.last_error_message)),
    }


def _json_param(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_value(value: Any, *, default: Any | None = None) -> Any:
    if value is None:
        return {} if default is None else default
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, bytes):
        return json.loads(value.decode("utf-8"))
    return value


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


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


__all__ = [
    "BillingDebugExport",
    "BillingDebugReservation",
    "BillingDebugSnapshot",
    "BillingDebugUsageEvent",
    "BillingExportEvent",
    "BillingCustomer",
    "BillingProvider",
    "BillingRecordStatus",
    "BillingReplayStatus",
    "BillingSqlStatement",
    "EntitlementPolicyRecord",
    "POSTGRES_BILLING_SCHEMA_SQL",
    "PolarEventIngestionEvent",
    "PolarUsageExport",
    "PolarWebhookEvent",
    "PolarWebhookSnapshot",
    "PriceBook",
    "PriceBookProduct",
    "ProductLimit",
    "WorkspaceBalanceSnapshot",
    "billing_customer_params",
    "billing_debug_reservation_from_row",
    "billing_debug_snapshot_from_rows",
    "billing_debug_snapshot_sql",
    "billing_export_event_from_usage_event",
    "billing_export_idempotency_key",
    "billing_export_params",
    "billing_schema_statements",
    "entitlement_policy_params",
    "mark_billing_export_replay",
    "polar_webhook_event_from_payload",
    "polar_webhook_snapshot_from_payload",
    "polar_webhook_snapshot_id",
    "polar_webhook_snapshot_params",
    "price_book_params",
    "upsert_billing_customer_sql",
    "upsert_billing_export_sql",
    "upsert_entitlement_policy_sql",
    "upsert_polar_webhook_snapshot_sql",
    "upsert_price_book_sql",
    "upsert_workspace_balance_sql",
    "workspace_balance_params",
]
