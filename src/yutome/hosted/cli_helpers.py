from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from yutome.hosted.models import UsageEvent, UsageSubject, normalize_unit_quantity, unit_quantity_decimal


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
    "UsageLedgerSummary",
    "summarize_usage_events",
]
