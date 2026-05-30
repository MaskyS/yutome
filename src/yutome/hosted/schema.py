from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, MetaData, Numeric, Table, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.types import UserDefinedType


hosted_metadata = MetaData()


class BM25Vector(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **_kw: object) -> str:
        return "bm25vector"


class Vector(UserDefinedType):
    cache_ok = True

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions

    def get_col_spec(self, **_kw: object) -> str:
        return f"vector({self.dimensions})"


users = Table(
    "users",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("email", Text),
    Column("normalized_email", Text),
    Column("name", Text),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

Index("idx_users_normalized_email", users.c.normalized_email, unique=True)

workspaces = Table(
    "workspaces",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("owner_user_id", Text, ForeignKey("users.id")),
    Column("name", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    # Personal plan (flat seat + metered overage) lifecycle. `trialing` and `active` both grant
    # ingest; a workspace whose trial_ends_at has passed with no active/trialing subscription is
    # trial-expiry read-only. The Stripe webhook mirror keeps subscription_status in sync.
    Column("subscription_status", Text, nullable=False, server_default=text("'trialing'")),
    Column("trial_ends_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

workspace_members = Table(
    "workspace_members",
    hosted_metadata,
    Column("workspace_id", Text, ForeignKey("workspaces.id"), primary_key=True),
    Column("user_id", Text, ForeignKey("users.id"), primary_key=True),
    Column("role", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

account_sessions = Table(
    "account_sessions",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("user_id", Text, ForeignKey("users.id"), nullable=False),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("session_hash", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("scopes", ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]")),
    Column("audience", Text),
    Column("client_id", Text),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("last_used_at", DateTime(timezone=True)),
    Column("expires_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
)

Index("idx_account_sessions_session_hash", account_sessions.c.session_hash, unique=True)

email_login_tokens = Table(
    "email_login_tokens",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("token_hash", Text, nullable=False),
    Column("normalized_email", Text, nullable=False),
    Column("name", Text),
    Column("workspace_name", Text),
    Column("redirect_path", Text),
    Column("user_agent", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("consumed_at", DateTime(timezone=True)),
)

Index("idx_email_login_tokens_token_hash", email_login_tokens.c.token_hash, unique=True)
Index("idx_email_login_tokens_email", email_login_tokens.c.normalized_email, email_login_tokens.c.created_at.desc())

provider_allocations = Table(
    "provider_allocations",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("provider", Text, nullable=False),
    Column("operation", Text, nullable=False),
    Column("credential_mode", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("model_or_plan", Text),
    Column("external_allocation_id", Text),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

service_allocations = Table(
    "service_allocations",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("service", Text, nullable=False),
    Column("operation", Text, nullable=False),
    Column("credential_mode", Text, nullable=False, server_default=text("'service_internal'")),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("backend", Text, nullable=False),
    Column("index_profile_ref", Text),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

usage_reservations = Table(
    "usage_reservations",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("subject", Text, nullable=False),
    Column("operation", Text, nullable=False),
    Column("allocation_id", Text),
    Column("credential_mode", Text, nullable=False),
    Column("estimated_units_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("idempotency_key", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("decision_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("workspace_id", "idempotency_key"),
)

usage_events = Table(
    "usage_events",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("reservation_id", Text, ForeignKey("usage_reservations.id")),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("subject", Text, nullable=False),
    Column("operation", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("actual_units_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("provider_request_id", Text),
    Column("error_code", Text),
    Column("raw_usage_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

Index("idx_usage_events_workspace_created", usage_events.c.workspace_id, usage_events.c.created_at.desc())
Index("idx_usage_events_subject_operation", usage_events.c.subject, usage_events.c.operation, usage_events.c.created_at.desc())
Index(
    "idx_usage_events_provider_request_idempotency",
    usage_events.c.workspace_id,
    usage_events.c.subject,
    usage_events.c.operation,
    usage_events.c.event_type,
    usage_events.c.provider_request_id,
    unique=True,
    postgresql_where=usage_events.c.provider_request_id.is_not(None),
)

account_grants = Table(
    "account_grants",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("user_id", Text, ForeignKey("users.id"), nullable=False),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("kind", Text, nullable=False),
    Column("scopes", ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]")),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("audience", Text),
    Column("client_id", Text),
    Column("install_id", Text),
    Column("token_version", Integer, nullable=False, server_default=text("1")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("last_used_at", DateTime(timezone=True)),
    Column("expires_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
)

Index("idx_account_grants_workspace_status", account_grants.c.workspace_id, account_grants.c.status)
Index(
    "idx_account_grants_install_id",
    account_grants.c.install_id,
    unique=True,
    postgresql_where=account_grants.c.install_id.is_not(None),
)

api_keys = Table(
    "api_keys",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("user_id", Text, ForeignKey("users.id"), nullable=False),
    Column("key_hash", Text, nullable=False),
    Column("name", Text),
    Column("scopes", ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]")),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("last_used_at", DateTime(timezone=True)),
    Column("expires_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
)

Index("idx_api_keys_key_hash", api_keys.c.key_hash, unique=True)
Index("idx_api_keys_workspace_status", api_keys.c.workspace_id, api_keys.c.status)

youtube_grants = Table(
    "youtube_grants",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("user_id", Text, ForeignKey("users.id"), nullable=False),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("scopes", ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]")),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("last_used_at", DateTime(timezone=True)),
    Column("expires_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
)

price_books = Table(
    "price_books",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("version", Text, nullable=False),
    Column("effective_at", DateTime(timezone=True)),
    Column("currency", Text, nullable=False, server_default=text("'usd'")),
    Column("products_jsonb", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("unit_mapping_jsonb", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("version"),
)

entitlement_policies = Table(
    "entitlement_policies",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("plan_key", Text, nullable=False),
    Column("price_book_id", Text, ForeignKey("price_books.id"), nullable=False),
    Column("allowed_operations", ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]")),
    Column("included_units_jsonb", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("hard_limits_jsonb", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("soft_limits_jsonb", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("grace_policy_jsonb", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("workspace_id", "plan_key", "price_book_id"),
)

workspace_balances = Table(
    "workspace_balances",
    hosted_metadata,
    Column("workspace_id", Text, ForeignKey("workspaces.id"), primary_key=True),
    Column("entitlement_policy_id", Text, ForeignKey("entitlement_policies.id"), nullable=False),
    Column("period_start_at", DateTime(timezone=True), nullable=False),
    Column("period_end_at", DateTime(timezone=True), nullable=False),
    Column("used_units_jsonb", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("reserved_units_jsonb", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("remaining_units_jsonb", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("unlimited_units", ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

stripe_customers = Table(
    "stripe_customers",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False, unique=True),
    Column("stripe_customer_id", Text, nullable=False, unique=True),
    Column("stripe_subscription_id", Text),
    Column("subscription_status", Text, nullable=False, server_default=text("'none'")),
    Column("subscription_status_snapshot_jsonb", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("last_webhook_at", DateTime(timezone=True)),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

stripe_meter_exports = Table(
    "stripe_meter_exports",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("usage_event_id", Text, ForeignKey("usage_events.id"), nullable=False),
    Column("reservation_id", Text, ForeignKey("usage_reservations.id")),
    Column("stripe_customer_id", Text),
    Column("meter_unit", Text, nullable=False),
    Column("event_name", Text, nullable=False),
    Column("value_text", Text, nullable=False),
    Column("source_event_dedupe_key", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("stripe_meter_event_identifier", Text),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("last_error_jsonb", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("event_timestamp", DateTime(timezone=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("exported_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("source_event_dedupe_key"),
)

Index(
    "idx_stripe_meter_exports_replay",
    stripe_meter_exports.c.status,
    stripe_meter_exports.c.updated_at,
    postgresql_where=stripe_meter_exports.c.status.in_(["pending", "failed"]),
)

stripe_webhook_events = Table(
    "stripe_webhook_events",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("type", Text, nullable=False),
    Column("workspace_id", Text, ForeignKey("workspaces.id")),
    Column("payload_jsonb", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("processed_at", DateTime(timezone=True)),
    Column("last_error_jsonb", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

Index(
    "idx_stripe_webhook_events_replay",
    stripe_webhook_events.c.status,
    stripe_webhook_events.c.received_at,
    postgresql_where=stripe_webhook_events.c.status.in_(["pending", "failed"]),
)

sources = Table(
    "sources",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("source_type", Text, nullable=False),
    Column("source_url", Text, nullable=False),
    Column("canonical_channel_id", Text),
    Column("canonical_playlist_id", Text),
    Column("canonical_video_id", Text),
    Column("display_name", Text),
    Column("selected", Boolean, nullable=False, server_default=text("true")),
    Column("auto_index_allowed", Boolean, nullable=False, server_default=text("true")),
    Column("import_source", Text, nullable=False),
    Column("auth_grant_id", Text, ForeignKey("youtube_grants.id")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("last_discovered_at", DateTime(timezone=True)),
    Column("last_indexed_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("workspace_id", "source_url"),
)

Index("idx_sources_workspace_status", sources.c.workspace_id, sources.c.status)

source_refresh_policies = Table(
    "source_refresh_policies",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("source_id", Text, ForeignKey("sources.id"), nullable=False),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("cadence_seconds", Integer, nullable=False, server_default=text("900")),
    Column("jitter_seconds", Integer, nullable=False, server_default=text("0")),
    Column("next_run_at", DateTime(timezone=True), nullable=False),
    Column("last_started_at", DateTime(timezone=True)),
    Column("last_succeeded_at", DateTime(timezone=True)),
    Column("cursor_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("max_new_videos_per_run", Integer),
    Column("max_index_jobs_per_day", Integer),
    Column("policy_snapshot_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("failure_code", Text),
    Column("failure_message", Text),
    Column("locked_by", Text),
    Column("locked_until", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("workspace_id", "source_id"),
)

Index(
    "idx_source_refresh_due",
    source_refresh_policies.c.workspace_id,
    source_refresh_policies.c.next_run_at,
    postgresql_where=source_refresh_policies.c.enabled.is_(True),
)

jobs = Table(
    "jobs",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("source_id", Text, ForeignKey("sources.id")),
    Column("job_type", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'queued'")),
    Column("priority", Integer, nullable=False, server_default=text("100")),
    Column("idempotency_key", Text, nullable=False),
    Column("run_after", DateTime(timezone=True)),
    Column("executor_kind", Text),
    Column("executor_ref", Text),
    Column("lease_owner", Text),
    Column("leased_at", DateTime(timezone=True)),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("retry_after", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("cancelled_at", DateTime(timezone=True)),
    Column("error_code", Text),
    Column("error_message", Text),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    UniqueConstraint("workspace_id", "idempotency_key"),
)

Index(
    "idx_jobs_claimable",
    jobs.c.priority.asc(),
    jobs.c.run_after.asc().nulls_first(),
    jobs.c.created_at.asc(),
    postgresql_where=jobs.c.status.in_(["queued", "retry_wait"]),
)
Index("idx_jobs_workspace_status", jobs.c.workspace_id, jobs.c.status, jobs.c.created_at.desc())

job_operations = Table(
    "job_operations",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("job_id", Text, ForeignKey("jobs.id"), nullable=False),
    Column("operation", Text, nullable=False),
    Column("source_id", Text, ForeignKey("sources.id")),
    Column("video_id", Text),
    Column("input_hash", Text, nullable=False),
    Column("idempotency_key", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'planned'")),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("usage_reservation_id", Text, ForeignKey("usage_reservations.id")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("output_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("workspace_id", "idempotency_key"),
)

Index("idx_job_operations_job_status", job_operations.c.job_id, job_operations.c.status, job_operations.c.created_at)

videos = Table(
    "videos",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("source_id", Text, ForeignKey("sources.id")),
    Column("youtube_video_id", Text, nullable=False),
    Column("active_transcript_version_id", Text),
    Column("channel_id", Text),
    Column("title", Text, nullable=False, server_default=text("''")),
    Column("description", Text, nullable=False, server_default=text("''")),
    Column("published_at", DateTime(timezone=True)),
    Column("duration_seconds", Integer),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("workspace_id", "youtube_video_id"),
)

Index("idx_videos_workspace_channel", videos.c.workspace_id, videos.c.channel_id, videos.c.published_at.desc())
Index(
    "idx_videos_active_transcript",
    videos.c.workspace_id,
    videos.c.active_transcript_version_id,
    postgresql_where=videos.c.active_transcript_version_id.is_not(None),
)

transcript_versions = Table(
    "transcript_versions",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("video_id", Text, ForeignKey("videos.id"), nullable=False),
    Column("source", Text, nullable=False),
    Column("language_code", Text),
    Column("content_hash", Text, nullable=False),
    Column("provider_request_id", Text),
    Column("usage_event_id", Text, ForeignKey("usage_events.id")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

search_index_profiles = Table(
    "search_index_profiles",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("backend", Text, nullable=False, server_default=text("'postgres_vectorchord'")),
    Column("embedding_model", Text, nullable=False, server_default=text("'voyage-4-lite'")),
    Column("embedding_dimension", Integer, nullable=False, server_default=text("1024")),
    Column("chunking_version", Text, nullable=False),
    Column("tokenizer", Text, nullable=False, server_default=text("'yutome_llmlingua2'")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("workspace_id", "backend", "embedding_model", "embedding_dimension", "chunking_version", "tokenizer"),
)

chunks = Table(
    "chunks",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("video_id", Text, ForeignKey("videos.id"), nullable=False),
    Column("transcript_version_id", Text, ForeignKey("transcript_versions.id"), nullable=False),
    Column("index_profile_id", Text, ForeignKey("search_index_profiles.id"), nullable=False),
    Column("chunk_index", Integer, nullable=False),
    Column("start_seconds", Numeric),
    Column("end_seconds", Numeric),
    Column("text", Text, nullable=False),
    Column("bm25_document", BM25Vector(), nullable=False),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("workspace_id", "transcript_version_id", "index_profile_id", "chunk_index"),
)

Index("idx_chunks_bm25_document", chunks.c.bm25_document, postgresql_using="bm25", postgresql_ops={"bm25_document": "bm25_ops"})
Index("idx_chunks_workspace_video", chunks.c.workspace_id, chunks.c.video_id, chunks.c.chunk_index)

chunk_embeddings = Table(
    "chunk_embeddings",
    hosted_metadata,
    Column("id", Text, primary_key=True),
    Column("workspace_id", Text, ForeignKey("workspaces.id"), nullable=False),
    Column("chunk_id", Text, ForeignKey("chunks.id"), nullable=False),
    Column("index_profile_id", Text, ForeignKey("search_index_profiles.id"), nullable=False),
    Column("embedding", Vector(1024), nullable=False),
    Column("usage_event_id", Text, ForeignKey("usage_events.id")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("workspace_id", "chunk_id", "index_profile_id"),
)

Index("idx_chunk_embeddings_workspace_profile", chunk_embeddings.c.workspace_id, chunk_embeddings.c.index_profile_id)

__all__ = [
    "hosted_metadata",
    "account_grants",
    "account_sessions",
    "api_keys",
    "chunk_embeddings",
    "chunks",
    "email_login_tokens",
    "entitlement_policies",
    "job_operations",
    "jobs",
    "price_books",
    "provider_allocations",
    "search_index_profiles",
    "service_allocations",
    "source_refresh_policies",
    "sources",
    "stripe_customers",
    "stripe_meter_exports",
    "stripe_webhook_events",
    "transcript_versions",
    "usage_events",
    "usage_reservations",
    "users",
    "videos",
    "workspace_balances",
    "workspace_members",
    "workspaces",
    "youtube_grants",
]
