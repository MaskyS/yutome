"""hosted fresh baseline

Revision ID: 20260526_0001
Revises:
Create Date: 2026-05-26 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from yutome.hosted.postgres import hosted_schema_statements


revision: str = "20260526_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for statement in hosted_schema_statements():
        op.execute(statement)


def downgrade() -> None:
    for table_name in (
        "stripe_webhook_events",
        "stripe_meter_exports",
        "workspace_balances",
        "entitlement_policies",
        "stripe_customers",
        "price_books",
        "chunk_embeddings",
        "chunks",
        "search_index_profiles",
        "transcript_versions",
        "videos",
        "job_operations",
        "jobs",
        "source_refresh_policies",
        "sources",
        "youtube_grants",
        "account_grants",
        "usage_events",
        "usage_reservations",
        "service_allocations",
        "provider_allocations",
        "account_sessions",
        "workspace_members",
        "workspaces",
        "users",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table_name} CASCADE")
