from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from yutome.hosted.control_plane import Job
from yutome.hosted.jobs import (
    claim_jobs_sql,
    release_job_lease,
    renew_job_lease,
    retry_job_after,
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


def test_claim_jobs_sql_rejects_nonpositive_limits() -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        claim_jobs_sql(lease_owner="worker-1", now=NOW, limit=0)
