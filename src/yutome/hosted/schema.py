from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Index, MetaData, Table, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB


hosted_metadata = MetaData()

workspaces = Table(
    "workspaces",
    hosted_metadata,
    Column("id", Text, primary_key=True),
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

__all__ = [
    "hosted_metadata",
    "usage_events",
    "usage_reservations",
    "workspaces",
]
