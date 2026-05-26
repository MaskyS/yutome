from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from yutome.hosted.ledger import JsonlUsageLedger
from yutome.hosted.models import EventStatus, UsageEvent, UsageSubject, normalize_unit_quantity, unit_quantity_decimal


UsageUnitValue = float | int | str | bool | None
LedgerLike = JsonlUsageLedger | Path | str


@dataclass(frozen=True)
class DemoUsageEventSpec:
    subject: UsageSubject
    operation: str
    actual_units: Mapping[str, UsageUnitValue]
    event_type: str = "cli_smoke_succeeded"
    status: EventStatus = "succeeded"
    provider_request_id: str | None = None
    raw_usage: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class UsageLedgerSummary:
    subject: UsageSubject
    operation: str
    event_count: int
    unit_totals: dict[str, int | float]
    status_counts: dict[str, int]

    @property
    def operation_key(self) -> str:
        return f"{self.subject}.{self.operation}"


DEFAULT_DEMO_USAGE_EVENT_SPECS: tuple[DemoUsageEventSpec, ...] = (
    DemoUsageEventSpec(
        subject="search_store",
        operation="hybrid_query",
        actual_units={"queries": 1, "candidate_count": 12, "latency_ms": 18.0},
    ),
    DemoUsageEventSpec(
        subject="voyage",
        operation="embed_documents",
        actual_units={"total_tokens": 128, "vectors": 4},
        provider_request_id="demo_voyage_embed_documents",
    ),
)


def append_demo_usage_event(
    ledger: LedgerLike,
    *,
    workspace_id: str = "ws_demo",
    subject: UsageSubject = "search_store",
    operation: str = "hybrid_query",
    actual_units: Mapping[str, UsageUnitValue] | None = None,
    event_type: str = "cli_smoke_succeeded",
    status: EventStatus = "succeeded",
    provider_request_id: str | None = None,
    reservation_id: str | None = None,
    raw_usage: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> UsageEvent:
    """Append one synthetic hosted usage event for CLI smoke diagnostics."""
    event = UsageEvent(
        reservation_id=reservation_id,
        workspace_id=workspace_id,
        subject=subject,
        operation=operation,
        event_type=event_type,
        status=status,
        actual_units=dict(actual_units or {"events": 1}),
        provider_request_id=provider_request_id,
        raw_usage=_synthetic_payload(raw_usage),
        metadata=_synthetic_metadata(metadata),
    )
    _coerce_ledger(ledger).append(event)
    return event


def append_demo_usage_events(
    ledger: LedgerLike,
    specs: Iterable[DemoUsageEventSpec] | None = None,
    *,
    workspace_id: str = "ws_demo",
    metadata: Mapping[str, Any] | None = None,
) -> list[UsageEvent]:
    events: list[UsageEvent] = []
    event_specs = DEFAULT_DEMO_USAGE_EVENT_SPECS if specs is None else specs
    for spec in event_specs:
        events.append(
            append_demo_usage_event(
                ledger,
                workspace_id=workspace_id,
                subject=spec.subject,
                operation=spec.operation,
                actual_units=spec.actual_units,
                event_type=spec.event_type,
                status=spec.status,
                provider_request_id=spec.provider_request_id,
                raw_usage=spec.raw_usage,
                metadata={**dict(metadata or {}), **dict(spec.metadata or {})},
            )
        )
    return events


def summarize_usage_ledger(ledger: LedgerLike, *, limit: int | None = None) -> list[UsageLedgerSummary]:
    return summarize_usage_events(_read_events(ledger, limit=limit))


def summarize_usage_events(events: Iterable[UsageEvent]) -> list[UsageLedgerSummary]:
    buckets: dict[tuple[UsageSubject, str], _UsageSummaryAccumulator] = {}
    for event in events:
        key = (event.subject, event.operation)
        if key not in buckets:
            buckets[key] = _UsageSummaryAccumulator(subject=event.subject, operation=event.operation)
        buckets[key].add(event)
    return [buckets[key].summary() for key in sorted(buckets)]


@dataclass
class _UsageSummaryAccumulator:
    subject: UsageSubject
    operation: str
    event_count: int = 0
    unit_totals: dict[str, Decimal] = field(default_factory=dict)
    status_counts: dict[str, int] = field(default_factory=dict)

    def add(self, event: UsageEvent) -> None:
        self.event_count += 1
        self.status_counts[event.status] = self.status_counts.get(event.status, 0) + 1
        for unit, value in event.actual_units.items():
            quantity = _usage_quantity(value)
            if quantity is not None:
                self.unit_totals[unit] = self.unit_totals.get(unit, Decimal("0")) + quantity

    def summary(self) -> UsageLedgerSummary:
        return UsageLedgerSummary(
            subject=self.subject,
            operation=self.operation,
            event_count=self.event_count,
            unit_totals={unit: _json_number(total) for unit, total in sorted(self.unit_totals.items())},
            status_counts=dict(sorted(self.status_counts.items())),
        )


def _read_events(ledger: LedgerLike, *, limit: int | None) -> list[UsageEvent]:
    jsonl_ledger = _coerce_ledger(ledger)
    if limit is not None:
        return jsonl_ledger.recent(limit=limit)
    if not jsonl_ledger.path.exists():
        return []
    rows: list[UsageEvent] = []
    with jsonl_ledger.path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(UsageEvent.model_validate(json.loads(line)))
    return rows


def _coerce_ledger(ledger: LedgerLike) -> JsonlUsageLedger:
    if isinstance(ledger, JsonlUsageLedger):
        return ledger
    return JsonlUsageLedger(Path(ledger))


def _synthetic_payload(raw_usage: Mapping[str, Any] | None) -> dict[str, Any]:
    return {**dict(raw_usage or {}), "synthetic": True}


def _synthetic_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        **dict(metadata or {}),
        "synthetic": True,
        "source": "yutome.hosted.cli_helpers",
        "purpose": "cli_smoke_diagnostic",
    }


def _usage_quantity(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return unit_quantity_decimal(normalize_unit_quantity(value))
    except (TypeError, ValueError):
        return None


def _json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


__all__ = [
    "DEFAULT_DEMO_USAGE_EVENT_SPECS",
    "DemoUsageEventSpec",
    "UsageLedgerSummary",
    "append_demo_usage_event",
    "append_demo_usage_events",
    "summarize_usage_events",
    "summarize_usage_ledger",
]
