from __future__ import annotations

from yutome.hosted.allocations import operation_key, resolve_allocation
from yutome.hosted.gate import UsageGate
from yutome.hosted.models import (
    EntitlementPolicy,
    ProviderAllocation,
    ServiceAllocation,
    WorkspaceBalance,
)


def test_operation_key_matches_usage_gate_shape() -> None:
    assert operation_key("voyage", "embed_documents") == "voyage.embed_documents"


def test_resolve_allocation_prefers_exact_operation_over_wildcard() -> None:
    wildcard = ProviderAllocation(
        id="alloc_voyage_any",
        workspace_id="ws_alice",
        provider="voyage",
        operation="*",
    )
    exact = ProviderAllocation(
        id="alloc_voyage_embed",
        workspace_id="ws_alice",
        provider="voyage",
        operation="embed_documents",
    )

    resolution = resolve_allocation(
        [wildcard, exact],
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
    )

    assert resolution.reason == "matched_exact"
    assert resolution.allocation == exact
    assert resolution.operation_key == "voyage.embed_documents"


def test_resolve_allocation_uses_exact_disabled_override_before_wildcard_allow() -> None:
    wildcard = ProviderAllocation(
        id="alloc_gemini_any",
        workspace_id="ws_alice",
        provider="gemini",
        operation="*",
    )
    disabled_exact = ProviderAllocation(
        id="alloc_gemini_transcribe_disabled",
        workspace_id="ws_alice",
        provider="gemini",
        operation="transcribe_media",
        status="disabled",
    )

    resolution = resolve_allocation(
        [wildcard, disabled_exact],
        workspace_id="ws_alice",
        subject="gemini",
        operation="transcribe_media",
    )
    reservation = UsageGate().reserve(
        workspace_id="ws_alice",
        subject="gemini",
        operation="transcribe_media",
        estimated_units={"media_seconds": 30},
        allocation=resolution.allocation,
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={"gemini.transcribe_media"},
        ),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"media_seconds": 60}),
        idempotency_key="idem",
    )

    assert resolution.reason == "matched_exact"
    assert resolution.allocation == disabled_exact
    assert reservation.status == "denied"
    assert reservation.decision.reason == "allocation_disabled"


def test_resolve_allocation_is_workspace_scoped() -> None:
    other_workspace = ProviderAllocation(
        id="alloc_other",
        workspace_id="ws_bob",
        provider="voyage",
        operation="embed_documents",
    )

    resolution = resolve_allocation(
        [other_workspace],
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
    )
    reservation = UsageGate().reserve(
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
        estimated_units={"total_tokens": 10},
        allocation=resolution.allocation,
        policy=EntitlementPolicy(id="policy", workspace_id="ws_alice"),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"total_tokens": 20}),
        idempotency_key="idem",
    )

    assert resolution.reason == "missing"
    assert reservation.status == "denied"
    assert reservation.decision.reason == "allocation_missing"


def test_resolve_service_allocation_composes_with_usage_gate_allow() -> None:
    allocation = ServiceAllocation(
        id="svc_search",
        workspace_id="ws_alice",
        service="search_store",
        operation="hybrid_query",
        backend="postgres_vectorchord",
    )

    resolution = resolve_allocation(
        [allocation],
        workspace_id="ws_alice",
        subject="search_store",
        operation="hybrid_query",
    )
    reservation = UsageGate().reserve(
        workspace_id="ws_alice",
        subject="search_store",
        operation="hybrid_query",
        estimated_units={"queries": 1},
        allocation=resolution.allocation,
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={"search_store.hybrid_query"},
        ),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"queries": 5}),
        idempotency_key="idem",
    )

    assert resolution.reason == "matched_exact"
    assert reservation.status == "reserved"
    assert reservation.allocation_id == "svc_search"
