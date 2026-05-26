"""Hosted-provider broker primitives.

The hosted package is intentionally small at first. It defines the shared
usage, entitlement, idempotency, and search-store contracts that later hosted
provider wrappers and workers can build on without changing the local-first
runtime behavior.
"""

from yutome.hosted.gate import UsageGate
from yutome.hosted.ids import idempotency_key, input_hash
from yutome.hosted.ledger import JsonlUsageLedger
from yutome.hosted.events import denied_usage_event, usage_event_from_normalization
from yutome.hosted.models import (
    EntitlementPolicy,
    ProviderAllocation,
    ServiceAllocation,
    UsageDecision,
    UsageEvent,
    UsageReservation,
    WorkspaceBalance,
)

__all__ = [
    "EntitlementPolicy",
    "JsonlUsageLedger",
    "ProviderAllocation",
    "ServiceAllocation",
    "UsageDecision",
    "UsageEvent",
    "UsageGate",
    "UsageReservation",
    "WorkspaceBalance",
    "denied_usage_event",
    "idempotency_key",
    "input_hash",
    "usage_event_from_normalization",
]
