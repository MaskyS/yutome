from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from yutome.hosted.control_plane import Job
from yutome.hosted.jobs import (
    active_job_lease_sql,
    claim_jobs_sql,
    job_repository_constraint_statements,
    release_job_lease,
    release_job_lease_sql,
    renew_job_lease,
    renew_job_lease_sql,
    retry_job_after,
    retry_job_sql,
)


NOW = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)


def _leased_job() -> Job:
    return Job(
        id="job_1",
        workspace_id="ws_alice",
        job_type="index_video",
        status="queued",
        idempotency_key="ws_alice:vid_1:index:h_123",
        lease_owner="worker-1",
        leased_at=NOW - timedelta(seconds=30),
        lease_expires_at=NOW + timedelta(seconds=60),
    )


def test_claim_jobs_sql_uses_skip_locked_and_claimable_filters() -> None:
    statement = claim_jobs_sql(
        lease_owner="worker-1",
        now=NOW,
        lease_seconds=120,
        limit=3,
        workspace_id="ws_alice",
        job_types=["index_video", "discover_source"],
        executor_kind="railway_worker",
        executor_ref="deploy_123",
    )

    assert "WITH claimable AS" in statement.sql
    assert "FOR UPDATE SKIP LOCKED" in statement.sql
    assert "status = ANY(%(claimable_statuses)s)" in statement.sql
    assert "(lease_owner IS NULL OR lease_expires_at <= %(now)s)" in statement.sql
    assert "workspace_id = %(workspace_id)s" in statement.sql
    assert "job_type = ANY(%(job_types)s)" in statement.sql
    assert "ORDER BY priority ASC, created_at ASC, id ASC" in statement.sql
    assert statement.params["lease_expires_at"] == NOW + timedelta(seconds=120)
    assert statement.params["claimable_statuses"] == ["queued", "retry_wait"]
    assert statement.params["limit"] == 3


def test_renew_release_and_retry_sql_are_owner_guarded() -> None:
    retry_at = NOW + timedelta(minutes=5)

    renew = renew_job_lease_sql(job_id="job_1", lease_owner="worker-1", now=NOW, lease_seconds=90)
    release = release_job_lease_sql(job_id="job_1", lease_owner="worker-1")
    retry = retry_job_sql(
        job_id="job_1",
        lease_owner="worker-1",
        now=NOW,
        retry_after=retry_at,
        error_code="provider_rate_limited",
        error_message="try later",
    )

    assert "lease_owner = %(lease_owner)s" in renew.sql
    assert "lease_expires_at > %(now)s" in renew.sql
    assert renew.params["lease_expires_at"] == NOW + timedelta(seconds=90)
    assert "lease_owner = NULL" in release.sql
    assert "leased_at = NULL" in release.sql
    assert "status = %(status)s" in retry.sql
    assert "lease_expires_at > %(now)s" in retry.sql
    assert "status <> ALL(%(terminal_statuses)s)" in retry.sql
    assert retry.params["status"] == "retry_wait"
    assert retry.params["retry_after"] == retry_at

    active = active_job_lease_sql(job_id="job_1", lease_owner="worker-1", now=NOW)
    assert "FOR UPDATE" in active.sql
    assert "lease_expires_at > %(now)s" in active.sql


def test_job_lease_model_helpers_respect_owner_and_terminal_boundaries() -> None:
    job = _leased_job()
    renewed = renew_job_lease(job, lease_owner="worker-1", now=NOW, lease_seconds=300)
    released = release_job_lease(job, lease_owner="worker-1")
    retry = retry_job_after(
        job,
        lease_owner="worker-1",
        retry_after=NOW + timedelta(minutes=10),
        error_code="transient_provider_failure",
    )
    terminal = job.model_copy(update={"status": "succeeded", "finished_at": NOW})

    assert renewed is not None
    assert renewed.lease_expires_at == NOW + timedelta(seconds=300)
    assert released is not None
    assert released.lease_owner is None
    assert retry is not None
    assert retry.status == "retry_wait"
    assert retry.lease_owner is None
    assert renew_job_lease(job, lease_owner="other-worker", now=NOW) is None
    assert retry_job_after(terminal, lease_owner="worker-1", retry_after=NOW, error_code="ignored") is None


def test_job_repository_constraints_include_idempotency_and_claimable_index() -> None:
    statements = "\n".join(job_repository_constraint_statements())

    assert "idx_jobs_workspace_idempotency_key" in statements
    assert "ON jobs(workspace_id, idempotency_key)" in statements
    assert "idx_jobs_claimable_lease" in statements
    assert "WHERE status IN ('queued', 'retry_wait')" in statements


def test_claim_jobs_sql_rejects_nonpositive_limits() -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        claim_jobs_sql(lease_owner="worker-1", now=NOW, limit=0)
