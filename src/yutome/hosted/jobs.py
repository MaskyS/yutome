from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal

from yutome.hosted.control_plane import CLAIMABLE_JOB_STATUSES, TERMINAL_JOB_STATUSES, Job
from yutome.hosted.repositories import SqlStatement


JobRetryStatus = Literal["retry_wait"]


JOB_IDEMPOTENCY_CONSTRAINT_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_workspace_idempotency_key
    ON jobs(workspace_id, idempotency_key);
""".strip()

JOB_CLAIMABLE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_jobs_claimable_lease
    ON jobs(priority, created_at, id)
    WHERE status IN ('queued', 'retry_wait');
""".strip()


def job_repository_constraint_statements() -> list[str]:
    return [JOB_IDEMPOTENCY_CONSTRAINT_SQL, JOB_CLAIMABLE_INDEX_SQL]


def claim_jobs_sql(
    *,
    lease_owner: str,
    now: datetime,
    lease_seconds: int = 900,
    limit: int = 1,
    workspace_id: str | None = None,
    job_types: Sequence[str] | None = None,
    executor_kind: str | None = None,
    executor_ref: str | None = None,
) -> SqlStatement:
    """Build the queue claim statement for Postgres workers.

    Leasing is deliberately separate from job business-state transitions. The
    worker claims rows atomically, then explicit job code advances status.
    """

    _validate_positive("lease_seconds", lease_seconds)
    _validate_positive("limit", limit)

    filters: list[str] = [
        "status = ANY(%(claimable_statuses)s)",
        "(run_after IS NULL OR run_after <= %(now)s)",
        "(retry_after IS NULL OR retry_after <= %(now)s)",
        "(lease_owner IS NULL OR lease_expires_at <= %(now)s)",
    ]
    params = _lease_params(
        lease_owner=lease_owner,
        now=now,
        lease_seconds=lease_seconds,
    )
    params.update(
        {
            "claimable_statuses": sorted(CLAIMABLE_JOB_STATUSES),
            "limit": limit,
            "executor_kind": executor_kind,
            "executor_ref": executor_ref,
        }
    )

    if workspace_id is not None:
        filters.append("workspace_id = %(workspace_id)s")
        params["workspace_id"] = workspace_id
    if job_types:
        filters.append("job_type = ANY(%(job_types)s)")
        params["job_types"] = list(job_types)

    where_sql = "\n      AND ".join(filters)

    return SqlStatement(
        sql=f"""
WITH claimable AS (
    SELECT id
    FROM jobs
    WHERE {where_sql}
    ORDER BY priority ASC, created_at ASC, id ASC
    LIMIT %(limit)s
    FOR UPDATE SKIP LOCKED
)
UPDATE jobs
SET lease_owner = %(lease_owner)s,
    leased_at = %(now)s,
    lease_expires_at = %(lease_expires_at)s,
    executor_kind = COALESCE(%(executor_kind)s, executor_kind),
    executor_ref = COALESCE(%(executor_ref)s, executor_ref)
FROM claimable
WHERE jobs.id = claimable.id
RETURNING jobs.*;
""".strip(),
        params=params,
    )


def renew_job_lease_sql(
    *,
    job_id: str,
    lease_owner: str,
    now: datetime,
    lease_seconds: int = 900,
) -> SqlStatement:
    _validate_positive("lease_seconds", lease_seconds)
    params = _lease_params(lease_owner=lease_owner, now=now, lease_seconds=lease_seconds)
    params.update({"job_id": job_id, "terminal_statuses": sorted(TERMINAL_JOB_STATUSES)})
    return SqlStatement(
        sql="""
UPDATE jobs
SET lease_expires_at = %(lease_expires_at)s
WHERE id = %(job_id)s
  AND lease_owner = %(lease_owner)s
  AND lease_expires_at > %(now)s
  AND status <> ALL(%(terminal_statuses)s)
RETURNING *;
""".strip(),
        params=params,
    )


def release_job_lease_sql(*, job_id: str, lease_owner: str) -> SqlStatement:
    return SqlStatement(
        sql="""
UPDATE jobs
SET lease_owner = NULL,
    leased_at = NULL,
    lease_expires_at = NULL
WHERE id = %(job_id)s
  AND lease_owner = %(lease_owner)s
RETURNING *;
""".strip(),
        params={"job_id": job_id, "lease_owner": lease_owner},
    )


def retry_job_sql(
    *,
    job_id: str,
    lease_owner: str,
    retry_after: datetime,
    error_code: str,
    error_message: str | None = None,
) -> SqlStatement:
    return SqlStatement(
        sql="""
UPDATE jobs
SET status = %(status)s,
    retry_after = %(retry_after)s,
    error_code = %(error_code)s,
    error_message = %(error_message)s,
    lease_owner = NULL,
    leased_at = NULL,
    lease_expires_at = NULL
WHERE id = %(job_id)s
  AND lease_owner = %(lease_owner)s
  AND status <> ALL(%(terminal_statuses)s)
RETURNING *;
""".strip(),
        params={
            "job_id": job_id,
            "lease_owner": lease_owner,
            "retry_after": retry_after,
            "error_code": error_code,
            "error_message": error_message,
            "status": "retry_wait",
            "terminal_statuses": sorted(TERMINAL_JOB_STATUSES),
        },
    )


def renew_job_lease(
    job: Job,
    *,
    lease_owner: str,
    now: datetime,
    lease_seconds: int = 900,
) -> Job | None:
    _validate_positive("lease_seconds", lease_seconds)
    if job.terminal or job.lease_owner != lease_owner or not job.has_active_lease(now):
        return None
    return job.model_copy(update={"lease_expires_at": now + timedelta(seconds=lease_seconds)})


def release_job_lease(job: Job, *, lease_owner: str) -> Job | None:
    if job.lease_owner != lease_owner:
        return None
    return job.model_copy(update={"lease_owner": None, "leased_at": None, "lease_expires_at": None})


def retry_job_after(
    job: Job,
    *,
    lease_owner: str,
    retry_after: datetime,
    error_code: str,
    error_message: str | None = None,
) -> Job | None:
    if job.terminal or job.lease_owner != lease_owner:
        return None
    return job.model_copy(
        update={
            "status": "retry_wait",
            "retry_after": retry_after,
            "error_code": error_code,
            "error_message": error_message,
            "lease_owner": None,
            "leased_at": None,
            "lease_expires_at": None,
        }
    )


def _lease_params(*, lease_owner: str, now: datetime, lease_seconds: int) -> dict[str, object]:
    return {
        "lease_owner": lease_owner,
        "now": now,
        "lease_expires_at": now + timedelta(seconds=lease_seconds),
    }


def _validate_positive(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


__all__: Sequence[str] = [
    "JOB_CLAIMABLE_INDEX_SQL",
    "JOB_IDEMPOTENCY_CONSTRAINT_SQL",
    "claim_jobs_sql",
    "job_repository_constraint_statements",
    "release_job_lease",
    "release_job_lease_sql",
    "renew_job_lease",
    "renew_job_lease_sql",
    "retry_job_after",
    "retry_job_sql",
]
