from __future__ import annotations

from datetime import datetime, timedelta, timezone

from yutome.hosted.control_plane import (
    Job,
    JobOperation,
    Source,
    SourceRefreshPolicy,
    YouTubeGrant,
    claim_job_lease,
    discoverable_sources,
    job_is_claimable,
    job_operation_idempotency_key,
    job_operation_key_matches,
    source_discovery_decision,
    source_refresh_policy_due,
    validate_terminal_job_state,
)


NOW = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)


def test_oauth_and_public_sources_can_coexist_in_workspace() -> None:
    grant = YouTubeGrant(
        id="yt_grant_alice",
        user_id="usr_alice",
        workspace_id="ws_alice",
        expires_at=NOW + timedelta(hours=1),
    )
    oauth_source = Source(
        id="src_alice_subs",
        workspace_id="ws_alice",
        source_type="subscriptions",
        source_url="youtube://subscriptions/mine",
        import_source="youtube_oauth",
        auth_grant_id=grant.id,
    )
    public_source = Source(
        id="src_alice_public_channel",
        workspace_id="ws_alice",
        source_type="channel",
        source_url="https://youtube.com/@examplechannel",
        import_source="manual_url",
        canonical_channel_id="@examplechannel",
    )

    sources = discoverable_sources([oauth_source, public_source], [grant], now=NOW)

    assert [source.id for source in sources] == ["src_alice_subs", "src_alice_public_channel"]
    assert oauth_source.requires_youtube_grant is True
    assert public_source.is_public_source is True


def test_expired_oauth_does_not_block_public_source_discovery() -> None:
    expired_grant = YouTubeGrant(
        id="yt_grant_expired",
        user_id="usr_alice",
        workspace_id="ws_alice",
        status="expired",
        expires_at=NOW - timedelta(minutes=1),
    )
    oauth_source = Source(
        id="src_alice_expired_subs",
        workspace_id="ws_alice",
        source_type="subscriptions",
        source_url="youtube://subscriptions/mine",
        import_source="youtube_oauth",
        auth_grant_id=expired_grant.id,
    )
    public_source = Source(
        id="src_alice_public_channel",
        workspace_id="ws_alice",
        source_type="channel",
        source_url="https://youtube.com/@examplechannel",
        import_source="manual_url",
        canonical_channel_id="@examplechannel",
    )

    oauth_decision = source_discovery_decision(oauth_source, [expired_grant], now=NOW)
    public_decision = source_discovery_decision(public_source, [expired_grant], now=NOW)

    assert oauth_decision.discoverable is False
    assert oauth_decision.code == "source_auth_failed"
    assert public_decision.discoverable is True
    assert public_decision.code == "discoverable"


def test_source_refresh_policy_due_logic_respects_enabled_time_lock_and_source_auth() -> None:
    source = Source(
        id="src_alice_public_channel",
        workspace_id="ws_alice",
        source_type="channel",
        source_url="https://youtube.com/@examplechannel",
        import_source="manual_url",
    )
    due_policy = SourceRefreshPolicy(
        id="srp_due",
        workspace_id="ws_alice",
        source_id=source.id,
        next_run_at=NOW - timedelta(seconds=1),
    )
    future_policy = due_policy.model_copy(update={"id": "srp_future", "next_run_at": NOW + timedelta(seconds=1)})
    disabled_policy = due_policy.model_copy(update={"id": "srp_disabled", "enabled": False})
    locked_policy = due_policy.model_copy(
        update={"id": "srp_locked", "locked_by": "worker-1", "locked_until": NOW + timedelta(minutes=5)}
    )
    expired_grant = YouTubeGrant(
        id="yt_grant_expired",
        user_id="usr_alice",
        workspace_id="ws_alice",
        status="expired",
        expires_at=NOW - timedelta(minutes=1),
    )
    oauth_source = Source(
        id="src_alice_subs",
        workspace_id="ws_alice",
        source_type="subscriptions",
        source_url="youtube://subscriptions/mine",
        import_source="youtube_oauth",
        auth_grant_id=expired_grant.id,
    )

    assert source_refresh_policy_due(due_policy, now=NOW, source=source) is True
    assert source_refresh_policy_due(future_policy, now=NOW, source=source) is False
    assert source_refresh_policy_due(disabled_policy, now=NOW, source=source) is False
    assert source_refresh_policy_due(locked_policy, now=NOW, source=source) is False
    assert source_refresh_policy_due(due_policy, now=NOW, source=oauth_source, youtube_grants=[expired_grant]) is False


def test_terminal_job_state_validation_requires_terminal_timestamps_and_errors() -> None:
    succeeded = Job(
        id="job_ok",
        workspace_id="ws_alice",
        job_type="index_video",
        status="succeeded",
        idempotency_key="ws_alice:src_1:index_policy_v1",
        finished_at=NOW,
    )
    failed_missing_error = succeeded.model_copy(
        update={"id": "job_failed", "status": "failed", "finished_at": NOW, "error_code": None}
    )
    cancelled_missing_cancel_time = succeeded.model_copy(
        update={"id": "job_cancelled", "status": "cancelled", "finished_at": None, "cancelled_at": None}
    )

    assert validate_terminal_job_state(succeeded) == []
    assert validate_terminal_job_state(failed_missing_error) == ["failed_job_missing_error_code"]
    assert validate_terminal_job_state(cancelled_missing_cancel_time) == ["cancelled_job_missing_cancelled_at"]


def test_job_lease_claim_eligibility_excludes_terminal_future_and_active_leases() -> None:
    claimable = Job(
        id="job_claimable",
        workspace_id="ws_alice",
        job_type="discover_source",
        status="queued",
        idempotency_key="ws_alice:src_1:discover_source:v1",
    )
    future = claimable.model_copy(update={"id": "job_future", "run_after": NOW + timedelta(minutes=1)})
    terminal = claimable.model_copy(update={"id": "job_done", "status": "succeeded", "finished_at": NOW})
    leased = claimable.model_copy(
        update={
            "id": "job_leased",
            "lease_owner": "worker-1",
            "leased_at": NOW - timedelta(seconds=30),
            "lease_expires_at": NOW + timedelta(minutes=5),
        }
    )

    claimed = claim_job_lease(claimable, lease_owner="worker-1", now=NOW, lease_seconds=60)

    assert job_is_claimable(claimable, now=NOW) is True
    assert claimed is not None
    assert claimed.lease_owner == "worker-1"
    assert claimed.lease_expires_at == NOW + timedelta(seconds=60)
    assert job_is_claimable(future, now=NOW) is False
    assert job_is_claimable(terminal, now=NOW) is False
    assert job_is_claimable(leased, now=NOW) is False


def test_job_operation_idempotency_key_shape_includes_workspace_video_operation_hash_and_extras() -> None:
    key = job_operation_idempotency_key(
        workspace_id="ws_alice",
        video_id="vid_123",
        operation="search_store.index_write",
        input_hash_value="h_index_123",
        extras=["sip_voyage4lite_bm25_default"],
    )
    operation = JobOperation(
        workspace_id="ws_alice",
        job_id="job_alice_index_001",
        operation="search_store.index_write",
        video_id="vid_123",
        input_hash="h_index_123",
        idempotency_key=key,
        status="succeeded",
        metadata_jsonb={"idempotency_extras": ["sip_voyage4lite_bm25_default"]},
    )

    assert key == "ws_alice:vid_123:search_store.index_write:h_index_123:sip_voyage4lite_bm25_default"
    assert job_operation_key_matches(operation) is True
