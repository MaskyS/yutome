from __future__ import annotations

from typing import get_args

from yutome.hosted.allocation_policy import (
    WebshareSubuserAllocation,
    decide_allocation_for_estimate,
    default_search_store_allocation,
    estimate_gemini_media_transcription,
    estimate_search_store_query,
    estimate_voyage_embeddings,
    estimate_webshare_proxy_fetch,
)
from yutome.hosted.models import CredentialMode, EntitlementPolicy, ProviderAllocation, WorkspaceBalance


def test_credential_modes_do_not_include_byo_local() -> None:
    assert "byo_local" not in get_args(CredentialMode)
    assert {"hosted", "byo_hosted", "disabled", "service_internal"} <= set(get_args(CredentialMode))


def test_webshare_subuser_allocation_maps_to_provider_allocation_metadata() -> None:
    subuser = WebshareSubuserAllocation(
        id="alloc_webshare_alice",
        workspace_id="ws_alice",
        subuser_id="451",
        label="Alice proxy pool",
        proxy_limit_gb=25,
        bandwidth_remaining_bytes=1024,
        max_thread_count=10,
    )

    allocation = subuser.to_provider_allocation()

    assert allocation.provider == "webshare"
    assert allocation.operation == "proxy_fetch"
    assert allocation.credential_mode == "hosted"
    assert allocation.external_allocation_id == "451"
    assert allocation.metadata["webshare_subuser_id"] == "451"
    assert allocation.metadata["bandwidth_remaining_bytes"] == 1024


def test_provider_allocation_policy_denies_disabled_gemini_fallback_before_call() -> None:
    estimate = estimate_gemini_media_transcription(duration_seconds=600, total_tokens_estimate=1000)
    disabled = ProviderAllocation(
        id="alloc_gemini_fallback_disabled",
        workspace_id="ws_alice",
        provider="gemini",
        operation="transcribe_media",
        credential_mode="hosted",
        status="disabled",
    )

    decision = decide_allocation_for_estimate(
        workspace_id="ws_alice",
        estimate=estimate,
        allocations=[disabled],
        policy=EntitlementPolicy(id="policy", workspace_id="ws_alice"),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"media_seconds": 10_000, "total_tokens": 10_000}),
        subject_id="vid_123",
    )

    assert decision.allowed is False
    assert decision.reservation.status == "denied"
    assert decision.reservation.decision.reason == "allocation_disabled"
    assert decision.denial_message == "This hosted operation is disabled for the workspace."


def test_voyage_estimate_reserves_hosted_allocation_with_stable_idempotency_key() -> None:
    estimate = estimate_voyage_embeddings(operation="embed_documents", total_tokens_estimate=900, vectors=10)
    allocation = ProviderAllocation(
        id="alloc_voyage",
        workspace_id="ws_alice",
        provider="voyage",
        operation="embed_documents",
        credential_mode="hosted",
    )

    left = decide_allocation_for_estimate(
        workspace_id="ws_alice",
        estimate=estimate,
        allocations=[allocation],
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={"voyage.embed_documents"},
        ),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"total_tokens": 1000, "vectors": 20}),
        subject_id="vid_123",
        idempotency_extras=["sip_default"],
    )
    right = decide_allocation_for_estimate(
        workspace_id="ws_alice",
        estimate=estimate,
        allocations=[allocation],
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={"voyage.embed_documents"},
        ),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"total_tokens": 1000, "vectors": 20}),
        subject_id="vid_123",
        idempotency_extras=["sip_default"],
    )

    assert left.allowed is True
    assert left.reservation.credential_mode == "hosted"
    assert left.reservation.idempotency_key == right.reservation.idempotency_key
    assert left.metadata["estimate_method"] == "voyage.count_tokens"


def test_webshare_quota_denial_uses_user_facing_balance_message() -> None:
    estimate = estimate_webshare_proxy_fetch(request_count=1, bytes_estimate=2048)
    allocation = WebshareSubuserAllocation(
        id="alloc_webshare",
        workspace_id="ws_alice",
        subuser_id="451",
        bandwidth_remaining_bytes=1024,
    ).to_provider_allocation()

    decision = decide_allocation_for_estimate(
        workspace_id="ws_alice",
        estimate=estimate,
        allocations=[allocation],
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={"webshare.proxy_fetch"},
        ),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"request_count": 10, "bytes": 1024}),
    )

    assert decision.allowed is False
    assert decision.reservation.decision.reason == "insufficient_balance"
    assert decision.denial_message == "This workspace does not have enough remaining usage balance."


def test_search_store_service_allocation_is_separate_from_provider_allocations() -> None:
    estimate = estimate_search_store_query(operation="hybrid_query", candidate_limit=40, query_vector_dimensions=1024)
    service_allocation = default_search_store_allocation(
        workspace_id="ws_alice",
        operation="hybrid_query",
        index_profile_ref="sip_default",
    )

    decision = decide_allocation_for_estimate(
        workspace_id="ws_alice",
        estimate=estimate,
        allocations=[service_allocation],
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={"search_store.hybrid_query"},
        ),
        balance=WorkspaceBalance(
            workspace_id="ws_alice",
            remaining_units={"queries": 10, "candidate_limit": 100, "query_vector_dimensions": 2048},
        ),
    )

    assert decision.allowed is True
    assert decision.reservation.subject == "search_store"
    assert decision.reservation.credential_mode == "service_internal"
    assert service_allocation.backend == "postgres_vectorchord"
    assert service_allocation.index_profile_ref == "sip_default"
