from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from yutome.hosted.ids import idempotency_key


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


UserStatus = Literal["active", "disabled", "suspended"]
WorkspaceStatus = Literal["active", "disabled", "suspended"]
WorkspaceRole = Literal["owner", "admin", "member", "viewer"]
AccountGrantKind = Literal["mcp_client", "cli_install", "account_session"]
AccountGrantStatus = Literal["active", "expired", "revoked", "disabled"]
YouTubeGrantStatus = Literal["active", "expired", "revoked", "invalid"]
SourceType = Literal["subscriptions", "subscription_collection", "channel", "handle", "playlist", "video", "url"]
SourceImport = Literal[
    "youtube_oauth",
    "public_api",
    "public_scrape",
    "yt_dlp",
    "manual_url",
    "onboarding",
    "manual",
    "oauth_sync",
    "cli",
]
SourceStatus = Literal["active", "pending", "disabled", "auth_failed", "not_found"]
DiscoveryDecisionCode = Literal["discoverable", "source_disabled", "source_auth_failed", "source_not_found"]
JobType = Literal[
    "discover_source",
    "index_video",
    "build_vector_index",
    "rebuild_bm25",
    "backfill_embeddings",
    "reindex_workspace",
]
JobStatus = Literal[
    "queued",
    "discovering",
    "queued_video_jobs",
    "preparing",
    "reserving_usage",
    "fetching_transcript",
    "fallback_transcription",
    "cleaning",
    "embedding",
    "writing_index",
    "reconciling_usage",
    "retry_wait",
    "denied",
    "failed",
    "succeeded",
    "cancelled",
]
JobOperationStatus = Literal[
    "planned",
    "denied",
    "reserved",
    "started",
    "succeeded",
    "failed_retryable",
    "failed_final",
    "reconciled",
    "released",
]
LeaseDenialReason = Literal["eligible", "terminal", "status_not_claimable", "run_after", "retry_after", "leased"]

TERMINAL_JOB_STATUSES = frozenset({"denied", "failed", "succeeded", "cancelled"})
CLAIMABLE_JOB_STATUSES = frozenset({"queued", "retry_wait"})
YOUTUBE_GRANT_SOURCE_TYPES = frozenset({"subscriptions", "subscription_collection"})
YOUTUBE_GRANT_IMPORT_SOURCES = frozenset({"youtube_oauth", "oauth_sync"})
PUBLIC_IMPORT_SOURCES = frozenset({"public_api", "public_scrape", "yt_dlp", "manual_url", "manual", "cli"})


class ControlPlaneModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class User(ControlPlaneModel):
    id: str
    email: str
    name: str | None = None
    status: UserStatus = "active"
    created_at: datetime = Field(default_factory=utc_now)


class Workspace(ControlPlaneModel):
    id: str
    owner_user_id: str
    name: str
    status: WorkspaceStatus = "active"
    created_at: datetime = Field(default_factory=utc_now)


class WorkspaceMember(ControlPlaneModel):
    workspace_id: str
    user_id: str
    role: WorkspaceRole = "member"
    created_at: datetime = Field(default_factory=utc_now)


class AccountGrant(ControlPlaneModel):
    """Yutome account authorization for MCP, CLI, or account sessions."""

    id: str
    user_id: str
    workspace_id: str
    kind: AccountGrantKind
    scopes: set[str] = Field(default_factory=set)
    status: AccountGrantStatus = "active"
    audience: str | None = None
    client_id: str | None = None
    install_id: str | None = None
    token_version: int = 1
    created_at: datetime = Field(default_factory=utc_now)
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    metadata_jsonb: dict[str, Any] = Field(default_factory=dict)

    def is_active(self, now: datetime | None = None) -> bool:
        now = now or utc_now()
        if self.status != "active" or self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > now

    def allows_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def access_token_props(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "grant_id": self.id,
            "client_id": self.client_id,
            "install_id": self.install_id,
            "scopes": sorted(self.scopes),
            "audience": self.audience,
            "token_version": self.token_version,
            "expires_at": self.expires_at,
        }


class YouTubeGrant(ControlPlaneModel):
    """YouTube OAuth authorization used only for source discovery."""

    id: str
    user_id: str
    workspace_id: str
    scopes: set[str] = Field(default_factory=set)
    status: YouTubeGrantStatus = "active"
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    metadata_jsonb: dict[str, Any] = Field(default_factory=dict)

    def is_active(self, now: datetime | None = None) -> bool:
        now = now or utc_now()
        if self.status != "active" or self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > now


class Source(ControlPlaneModel):
    id: str
    workspace_id: str
    source_type: SourceType
    source_url: str
    canonical_channel_id: str | None = None
    canonical_playlist_id: str | None = None
    canonical_video_id: str | None = None
    display_name: str | None = None
    selected: bool = True
    auto_index_allowed: bool = True
    import_source: SourceImport
    auth_grant_id: str | None = None
    metadata_jsonb: dict[str, Any] = Field(default_factory=dict)
    status: SourceStatus = "active"
    last_discovered_at: datetime | None = None
    last_indexed_at: datetime | None = None

    @property
    def requires_youtube_grant(self) -> bool:
        return (
            self.auth_grant_id is not None
            or self.source_type in YOUTUBE_GRANT_SOURCE_TYPES
            or self.import_source in YOUTUBE_GRANT_IMPORT_SOURCES
        )

    @property
    def is_public_source(self) -> bool:
        return not self.requires_youtube_grant and self.import_source in PUBLIC_IMPORT_SOURCES

    @property
    def canonical_ref(self) -> str:
        if self.source_type in YOUTUBE_GRANT_SOURCE_TYPES:
            return "youtube:subscriptions:mine"
        if self.canonical_video_id:
            return f"youtube:video:{self.canonical_video_id}"
        if self.canonical_playlist_id:
            return f"youtube:playlist:{self.canonical_playlist_id}"
        if self.canonical_channel_id:
            return f"youtube:channel:{self.canonical_channel_id}"
        return self.source_url


class SourceDiscoveryDecision(ControlPlaneModel):
    source_id: str
    discoverable: bool
    code: DiscoveryDecisionCode
    message: str | None = None


class SourceRefreshPolicy(ControlPlaneModel):
    id: str
    workspace_id: str
    source_id: str
    enabled: bool = True
    cadence_seconds: int = Field(default=900, gt=0)
    jitter_seconds: int = Field(default=0, ge=0)
    next_run_at: datetime
    last_started_at: datetime | None = None
    last_succeeded_at: datetime | None = None
    cursor_jsonb: dict[str, Any] = Field(default_factory=dict)
    max_new_videos_per_run: int | None = Field(default=None, gt=0)
    max_index_jobs_per_day: int | None = Field(default=None, gt=0)
    policy_snapshot_jsonb: dict[str, Any] = Field(default_factory=dict)
    failure_code: str | None = None
    failure_message: str | None = None
    locked_by: str | None = None
    locked_until: datetime | None = None

    def is_locked(self, now: datetime | None = None) -> bool:
        now = now or utc_now()
        return self.locked_until is not None and self.locked_until > now

    def is_due(self, now: datetime | None = None) -> bool:
        now = now or utc_now()
        return self.enabled and self.next_run_at <= now and not self.is_locked(now)


class Job(ControlPlaneModel):
    id: str
    workspace_id: str
    source_id: str | None = None
    job_type: JobType
    status: JobStatus = "queued"
    priority: int = 100
    idempotency_key: str
    run_after: datetime | None = None
    executor_kind: str | None = None
    executor_ref: str | None = None
    lease_owner: str | None = None
    leased_at: datetime | None = None
    lease_expires_at: datetime | None = None
    retry_after: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancelled_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    metadata_jsonb: dict[str, Any] = Field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return is_terminal_job_status(self.status)

    def has_active_lease(self, now: datetime | None = None) -> bool:
        now = now or utc_now()
        return self.lease_owner is not None and self.lease_expires_at is not None and self.lease_expires_at > now


class LeaseEligibility(ControlPlaneModel):
    eligible: bool
    reason: LeaseDenialReason = "eligible"
    message: str | None = None


class JobOperation(ControlPlaneModel):
    id: str = Field(default_factory=lambda: f"op_{uuid4().hex}")
    workspace_id: str
    job_id: str
    operation: str
    source_id: str | None = None
    video_id: str | None = None
    input_hash: str
    idempotency_key: str
    status: JobOperationStatus = "planned"
    attempt_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata_jsonb: dict[str, Any] = Field(default_factory=dict)

    @property
    def subject_id(self) -> str | None:
        return self.video_id or self.source_id


def _youtube_grant_map(grants: Mapping[str, YouTubeGrant] | Iterable[YouTubeGrant]) -> Mapping[str, YouTubeGrant]:
    if isinstance(grants, Mapping):
        return grants
    return {grant.id: grant for grant in grants}


def source_discovery_decision(
    source: Source,
    youtube_grants: Mapping[str, YouTubeGrant] | Iterable[YouTubeGrant] = (),
    *,
    now: datetime | None = None,
) -> SourceDiscoveryDecision:
    now = now or utc_now()
    if not source.selected or not source.auto_index_allowed or source.status == "disabled":
        return SourceDiscoveryDecision(
            source_id=source.id,
            discoverable=False,
            code="source_disabled",
            message="source is not selected for hosted auto-indexing",
        )
    if source.status == "not_found":
        return SourceDiscoveryDecision(
            source_id=source.id,
            discoverable=False,
            code="source_not_found",
            message="source no longer resolves",
        )
    if source.status == "auth_failed":
        return SourceDiscoveryDecision(
            source_id=source.id,
            discoverable=False,
            code="source_auth_failed",
            message="source auth is already marked failed",
        )

    if source.requires_youtube_grant:
        grants_by_id = _youtube_grant_map(youtube_grants)
        grant = grants_by_id.get(source.auth_grant_id or "")
        if grant is None or not grant.is_active(now):
            return SourceDiscoveryDecision(
                source_id=source.id,
                discoverable=False,
                code="source_auth_failed",
                message="YouTube OAuth grant is missing, expired, revoked, or invalid",
            )

    return SourceDiscoveryDecision(source_id=source.id, discoverable=True, code="discoverable")


def discoverable_sources(
    sources: Iterable[Source],
    youtube_grants: Mapping[str, YouTubeGrant] | Iterable[YouTubeGrant] = (),
    *,
    now: datetime | None = None,
) -> list[Source]:
    return [source for source in sources if source_discovery_decision(source, youtube_grants, now=now).discoverable]


def source_refresh_policy_due(
    policy: SourceRefreshPolicy,
    *,
    now: datetime | None = None,
    source: Source | None = None,
    youtube_grants: Mapping[str, YouTubeGrant] | Iterable[YouTubeGrant] = (),
) -> bool:
    now = now or utc_now()
    if not policy.is_due(now):
        return False
    if source is None:
        return True
    return source_discovery_decision(source, youtube_grants, now=now).discoverable


def advance_source_refresh_policy(
    policy: SourceRefreshPolicy,
    *,
    started_at: datetime,
    jitter_offset_seconds: int = 0,
) -> SourceRefreshPolicy:
    bounded_jitter = max(min(jitter_offset_seconds, policy.jitter_seconds), -policy.jitter_seconds)
    next_run_at = started_at + timedelta(seconds=policy.cadence_seconds + bounded_jitter)
    return policy.model_copy(update={"last_started_at": started_at, "next_run_at": next_run_at})


def is_terminal_job_status(status: JobStatus) -> bool:
    return status in TERMINAL_JOB_STATUSES


def validate_terminal_job_state(job: Job) -> list[str]:
    errors: list[str] = []
    if not job.terminal:
        if job.finished_at is not None:
            errors.append("nonterminal_job_has_finished_at")
        if job.cancelled_at is not None:
            errors.append("nonterminal_job_has_cancelled_at")
        return errors

    if job.status == "succeeded":
        if job.finished_at is None:
            errors.append("succeeded_job_missing_finished_at")
        if job.error_code is not None:
            errors.append("succeeded_job_has_error_code")
    elif job.status in {"failed", "denied"}:
        if job.finished_at is None:
            errors.append(f"{job.status}_job_missing_finished_at")
        if job.error_code is None:
            errors.append(f"{job.status}_job_missing_error_code")
    elif job.status == "cancelled":
        if job.cancelled_at is None:
            errors.append("cancelled_job_missing_cancelled_at")
    return errors


def job_lease_eligibility(job: Job, *, now: datetime | None = None) -> LeaseEligibility:
    now = now or utc_now()
    if job.terminal:
        return LeaseEligibility(eligible=False, reason="terminal", message="terminal jobs are not claimable")
    if job.status not in CLAIMABLE_JOB_STATUSES:
        return LeaseEligibility(eligible=False, reason="status_not_claimable", message="job status is not claimable")
    if job.run_after is not None and job.run_after > now:
        return LeaseEligibility(eligible=False, reason="run_after", message="job run_after is in the future")
    if job.retry_after is not None and job.retry_after > now:
        return LeaseEligibility(eligible=False, reason="retry_after", message="job retry_after is in the future")
    if job.has_active_lease(now):
        return LeaseEligibility(eligible=False, reason="leased", message="job has an active lease")
    return LeaseEligibility(eligible=True)


def job_is_claimable(job: Job, *, now: datetime | None = None) -> bool:
    return job_lease_eligibility(job, now=now).eligible


def claim_job_lease(
    job: Job,
    *,
    lease_owner: str,
    now: datetime | None = None,
    lease_seconds: int = 900,
) -> Job | None:
    now = now or utc_now()
    if not job_is_claimable(job, now=now):
        return None
    return job.model_copy(
        update={
            "lease_owner": lease_owner,
            "leased_at": now,
            "lease_expires_at": now + timedelta(seconds=lease_seconds),
        }
    )


def job_operation_idempotency_key(
    *,
    workspace_id: str,
    operation: str,
    input_hash_value: str,
    source_id: str | None = None,
    video_id: str | None = None,
    extras: Sequence[str] | None = None,
) -> str:
    return idempotency_key(
        workspace_id=workspace_id,
        subject_id=video_id or source_id,
        operation=operation,
        input_hash_value=input_hash_value,
        extras=extras,
    )


def job_operation_key_matches(operation: JobOperation) -> bool:
    expected = job_operation_idempotency_key(
        workspace_id=operation.workspace_id,
        operation=operation.operation,
        input_hash_value=operation.input_hash,
        source_id=operation.source_id,
        video_id=operation.video_id,
        extras=operation.metadata_jsonb.get("idempotency_extras"),
    )
    return operation.idempotency_key == expected


__all__ = [
    "AccountGrant",
    "CLAIMABLE_JOB_STATUSES",
    "Job",
    "JobOperation",
    "LeaseEligibility",
    "Source",
    "SourceDiscoveryDecision",
    "SourceRefreshPolicy",
    "TERMINAL_JOB_STATUSES",
    "User",
    "Workspace",
    "WorkspaceMember",
    "YouTubeGrant",
    "advance_source_refresh_policy",
    "claim_job_lease",
    "discoverable_sources",
    "is_terminal_job_status",
    "job_is_claimable",
    "job_lease_eligibility",
    "job_operation_idempotency_key",
    "job_operation_key_matches",
    "source_discovery_decision",
    "source_refresh_policy_due",
    "utc_now",
    "validate_terminal_job_state",
]
