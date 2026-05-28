"""Read-only dashboard projections for the hosted web frontend.

This module backs the authenticated dashboard's read endpoints
(`/account/summary`, `/account/library`, `/account/assistants`). It is
deliberately separate from `entitlements.py`: the gate path there loads the
minimal `UsageGate` inputs and **fails closed** (missing policy/balance → deny),
whereas a dashboard read of an unprovisioned or out-of-period workspace should
**fail soft** — return a clear "no active plan" projection, never a denial.

SQL is built with SQLAlchemy Core (compiled to the psycopg `%(name)s` shape) to
match the repository layer; these are SELECTs, not the upserts in the repository
module. None of these reads is metered — they never go through `UsageGate`, so
rendering the dashboard cannot spend a workspace's `queries` budget.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field
from sqlalchemy import bindparam, distinct, func, select

from yutome.hosted.models import jsonable_exact
from yutome.hosted.repositories import SqlStatement
from yutome.hosted.schema import (
    account_grants,
    entitlement_policies,
    sources,
    usage_events,
    videos,
    workspace_balances,
    workspaces,
)
from yutome.hosted.sqlalchemy_core import compile_postgres_statement


class SqlConnection(Protocol):
    def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> Any:
        ...


# Unit quantities are display passthrough already normalized by jsonable_exact
# (ints stay int, Decimals become strings). Annotate as Any so pydantic does not
# coerce a "9985.5" string into a float and lose exactness.
UnitValue = Any
WorkspaceState = Literal["active", "no_active_plan"]


class WorkspaceRef(BaseModel):
    id: str
    name: str | None = None


class BalancePeriod(BaseModel):
    start_at: datetime
    end_at: datetime


class WorkspaceUnit(BaseModel):
    unit: str
    included: UnitValue = None
    used: UnitValue = None
    reserved: UnitValue = None
    remaining: UnitValue = None
    unlimited: bool = False


EntitlementFormat = Literal["count", "minutes", "bytes", "ratio"]


class WorkspaceEntitlement(BaseModel):
    """A single user-facing usage line: a metered unit translated to a plain label
    the workspace owner understands, with `remaining` clamped to never go below 0."""

    key: str
    label: str
    description: str
    format: EntitlementFormat
    included: int | None = None
    used: int = 0
    remaining: int | None = None
    unlimited: bool = False
    percent: float | None = None


class WorkspaceSummary(BaseModel):
    state: WorkspaceState
    plan_key: str | None = None
    workspace: WorkspaceRef
    period: BalancePeriod | None = None
    # Raw per-unit balances (every metered key, including internal telemetry).
    # Kept for debugging/back-compat; the dashboard renders `entitlements` instead.
    units: list[WorkspaceUnit] = Field(default_factory=list)
    # Curated, user-facing view: only the units that mean something to a workspace
    # owner, with plain labels and clamped remaining. See _ENTITLEMENT_CATALOG.
    entitlements: list[WorkspaceEntitlement] = Field(default_factory=list)
    # Actual AI cost this period in USD (Gemini cleanup + Voyage embeddings),
    # priced from real per-provider token usage. None when no active period.
    ai_spend_usd: float | None = None


class LibraryVideo(BaseModel):
    video_id: str
    title: str | None = None
    channel_id: str | None = None
    published_at: datetime | None = None
    duration_seconds: int | None = None


class LibraryOverview(BaseModel):
    counts: dict[str, int] = Field(default_factory=dict)
    recent: list[LibraryVideo] = Field(default_factory=list)


class ConnectedAssistant(BaseModel):
    grant_id: str
    client_id: str | None = None
    scopes: list[str] = Field(default_factory=list)
    audience: str | None = None
    status: str
    token_version: int | None = None
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    expires_at: datetime | None = None


def _active_workspace_sql(*, workspace_id: str) -> SqlStatement:
    statement = (
        select(workspaces.c.id, workspaces.c.name, workspaces.c.status)
        .where(workspaces.c.id == bindparam("workspace_id", value=workspace_id))
        .limit(bindparam("limit", value=1))
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def _summary_policy_sql(*, workspace_id: str) -> SqlStatement:
    statement = (
        select(
            entitlement_policies.c.id,
            entitlement_policies.c.plan_key,
            entitlement_policies.c.included_units_jsonb,
        )
        .where(
            entitlement_policies.c.workspace_id == bindparam("workspace_id", value=workspace_id),
            entitlement_policies.c.status == bindparam("status", value="active"),
        )
        .order_by(
            entitlement_policies.c.updated_at.desc(),
            entitlement_policies.c.created_at.desc(),
            entitlement_policies.c.id,
        )
        .limit(bindparam("limit", value=1))
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def _summary_balance_sql(*, workspace_id: str, entitlement_policy_id: str) -> SqlStatement:
    statement = (
        select(
            workspace_balances.c.period_start_at,
            workspace_balances.c.period_end_at,
            workspace_balances.c.used_units_jsonb,
            workspace_balances.c.reserved_units_jsonb,
            workspace_balances.c.remaining_units_jsonb,
            workspace_balances.c.unlimited_units,
        )
        .where(
            workspace_balances.c.workspace_id == bindparam("workspace_id", value=workspace_id),
            workspace_balances.c.entitlement_policy_id == bindparam(
                "entitlement_policy_id",
                value=entitlement_policy_id,
            ),
            workspace_balances.c.period_start_at <= func.now(),
            workspace_balances.c.period_end_at > func.now(),
        )
        .limit(bindparam("limit", value=1))
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def _library_counts_sql(*, workspace_id: str) -> SqlStatement:
    videos_count = (
        select(func.count())
        .select_from(videos)
        .where(videos.c.workspace_id == bindparam("workspace_id", value=workspace_id))
        .scalar_subquery()
        .label("videos")
    )
    channels_count = (
        select(func.count(distinct(videos.c.channel_id)))
        .where(
            videos.c.workspace_id == bindparam("workspace_id", value=workspace_id),
            videos.c.channel_id.is_not(None),
        )
        .scalar_subquery()
        .label("channels")
    )
    sources_count = (
        select(func.count())
        .select_from(sources)
        .where(
            sources.c.workspace_id == bindparam("workspace_id", value=workspace_id),
            sources.c.status == bindparam("source_status", value="active"),
        )
        .scalar_subquery()
        .label("sources")
    )
    statement = select(videos_count, channels_count, sources_count)
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def _library_recent_sql(*, workspace_id: str, limit: int) -> SqlStatement:
    statement = (
        select(
            videos.c.youtube_video_id,
            videos.c.title,
            videos.c.channel_id,
            videos.c.published_at,
            videos.c.duration_seconds,
        )
        .where(videos.c.workspace_id == bindparam("workspace_id", value=workspace_id))
        .order_by(videos.c.published_at.desc().nullslast(), videos.c.created_at.desc())
        .limit(bindparam("limit", value=limit))
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def _assistants_sql(*, workspace_id: str) -> SqlStatement:
    statement = (
        select(
            account_grants.c.id,
            account_grants.c.client_id,
            account_grants.c.scopes,
            account_grants.c.audience,
            account_grants.c.status,
            account_grants.c.token_version,
            account_grants.c.created_at,
            account_grants.c.last_used_at,
            account_grants.c.expires_at,
        )
        .where(
            account_grants.c.workspace_id == bindparam("workspace_id", value=workspace_id),
            account_grants.c.status == bindparam("status", value="active"),
            account_grants.c.kind == bindparam("kind", value="mcp_client"),
        )
        .order_by(account_grants.c.created_at.desc())
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def load_active_workspace(connection: SqlConnection, *, workspace_id: str) -> dict[str, Any] | None:
    """Return the workspace row when it exists and is active, else None.

    Used by the API auth layer to 404 a verified session whose workspace has
    been removed or disabled, rather than serving an empty dashboard.
    """

    statement = _active_workspace_sql(workspace_id=workspace_id)
    row = _one(connection.execute(statement.sql, statement.params))
    if row is None or str(row.get("status")) != "active":
        return None
    return row


# User-facing usage catalog: the few metered units that mean something to a
# workspace owner, in display order. Everything else (token breakdowns, vector
# dimensions, latency, request byte sizes, …) is intentionally omitted so the
# dashboard never surfaces internal cost/telemetry. `total_tokens` is shown as a
# `ratio` (meter + percent only) because the glossary says not to expose raw
# tokens. See docs/hosted-glossary.md "User-Facing Entitlements".
_ENTITLEMENT_CATALOG: tuple[tuple[str, str, EntitlementFormat, str], ...] = (
    ("queries", "Searches", "count", "Transcript searches across your library."),
    ("media_seconds", "Transcription", "minutes", "Audio transcribed when a video has no usable captions."),
    ("bytes", "Bandwidth", "bytes", "Data fetched through reliable proxies while indexing."),
)


# AI cost is the real dollar cost of Gemini cleanup + Voyage embeddings, billed at
# 150% of published list price (fetched 2026-05-27):
#   gemini-3.1-flash-lite: $0.25/1M input (text), $1.50/1M output
#   voyage-4-lite:         $0.02/1M tokens
# Stored as USD per single token (list price / 1e6 * 1.5). Audio-input tokens from
# the rare transcribe_media fallback are priced at the text-input rate.
_AI_MARGIN = Decimal("1.5")
_AI_TOKEN_RATES_USD: dict[tuple[str, str], Decimal] = {
    ("gemini", "prompt_tokens"): Decimal("0.25") / Decimal(1_000_000) * _AI_MARGIN,
    ("gemini", "candidate_tokens"): Decimal("1.50") / Decimal(1_000_000) * _AI_MARGIN,
    ("voyage", "total_tokens"): Decimal("0.02") / Decimal(1_000_000) * _AI_MARGIN,
}

def _summary_ai_spend_sql(*, workspace_id: str, period_start_at: Any, period_end_at: Any) -> SqlStatement:
    statement = select(usage_events.c.subject, usage_events.c.actual_units_json).where(
        usage_events.c.workspace_id == bindparam("workspace_id", value=workspace_id),
        usage_events.c.status.in_(["succeeded", "released"]),
        usage_events.c.subject.in_(["gemini", "voyage"]),
        usage_events.c.created_at >= bindparam("period_start_at", value=period_start_at),
        usage_events.c.created_at < bindparam("period_end_at", value=period_end_at),
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def _ai_token_count(units: Mapping[str, Any], key: str) -> Decimal:
    value = units.get(key)
    if value is None:
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal(0)


def _compute_ai_spend_usd(rows: Iterable[Mapping[str, Any]]) -> float:
    """Sum the real dollar cost of metered Gemini + Voyage usage for the period."""
    total = Decimal(0)
    for row in rows:
        subject = str(row.get("subject") or "")
        units = _json_mapping(row.get("actual_units_json"))
        for (rate_subject, token_key), rate in _AI_TOKEN_RATES_USD.items():
            if rate_subject == subject:
                total += _ai_token_count(units, token_key) * rate
    return round(float(total), 4)


def _coerce_unit_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _build_entitlements(
    included: Mapping[str, Any],
    used: Mapping[str, Any],
    unlimited: set[str],
) -> list[WorkspaceEntitlement]:
    """Project the raw unit maps onto the curated catalog. `remaining` is computed
    as max(0, included - used) — never trusting a stored negative — and a unit is
    skipped when the plan neither includes nor has used it."""

    entitlements: list[WorkspaceEntitlement] = []
    for key, label, fmt, description in _ENTITLEMENT_CATALOG:
        is_unlimited = key in unlimited
        included_value = _coerce_unit_int(included.get(key))
        used_value = _coerce_unit_int(used.get(key)) or 0
        if included_value is None and used_value == 0 and not is_unlimited:
            continue  # not part of this plan and unused → nothing to show
        remaining = None if included_value is None else max(0, included_value - used_value)
        percent: float | None = None
        if not is_unlimited and included_value and included_value > 0:
            percent = min(1.0, max(0.0, used_value / included_value))
        entitlements.append(
            WorkspaceEntitlement(
                key=key,
                label=label,
                description=description,
                format=fmt,
                included=included_value,
                used=used_value,
                remaining=remaining,
                unlimited=is_unlimited,
                percent=percent,
            )
        )
    return entitlements


def read_workspace_summary(connection: SqlConnection, *, workspace_id: str) -> WorkspaceSummary:
    workspace_statement = _active_workspace_sql(workspace_id=workspace_id)
    workspace_row = _one(connection.execute(workspace_statement.sql, workspace_statement.params))
    workspace = WorkspaceRef(
        id=workspace_id,
        name=_optional_str(workspace_row.get("name")) if workspace_row else None,
    )

    policy_statement = _summary_policy_sql(workspace_id=workspace_id)
    policy_row = _one(connection.execute(policy_statement.sql, policy_statement.params))
    if policy_row is None:
        return WorkspaceSummary(state="no_active_plan", workspace=workspace)

    plan_key = _optional_str(policy_row.get("plan_key"))
    included = _json_mapping(policy_row.get("included_units_jsonb"))

    balance_statement = _summary_balance_sql(
        workspace_id=workspace_id,
        entitlement_policy_id=str(policy_row["id"]),
    )
    balance_row = _one(connection.execute(balance_statement.sql, balance_statement.params))
    if balance_row is None:
        # Policy exists but no balance for the current period: show the plan and
        # its included allowances, but mark the period inactive.
        units = [
            WorkspaceUnit(unit=unit, included=jsonable_exact(value))
            for unit, value in sorted(included.items())
        ]
        return WorkspaceSummary(
            state="no_active_plan",
            plan_key=plan_key,
            workspace=workspace,
            units=units,
            entitlements=_build_entitlements(included, {}, set()),
        )

    used = _json_mapping(balance_row.get("used_units_jsonb"))
    reserved = _json_mapping(balance_row.get("reserved_units_jsonb"))
    remaining = _json_mapping(balance_row.get("remaining_units_jsonb"))
    unlimited = set(_text_array(balance_row.get("unlimited_units")))
    unit_names = sorted(set(included) | set(used) | set(reserved) | set(remaining) | unlimited)
    units = [
        WorkspaceUnit(
            unit=unit,
            included=jsonable_exact(included.get(unit)),
            used=jsonable_exact(used.get(unit)),
            reserved=jsonable_exact(reserved.get(unit)),
            remaining=jsonable_exact(remaining.get(unit)),
            unlimited=unit in unlimited,
        )
        for unit in unit_names
    ]
    ai_spend_statement = _summary_ai_spend_sql(
        workspace_id=workspace_id,
        period_start_at=balance_row["period_start_at"],
        period_end_at=balance_row["period_end_at"],
    )
    ai_spend_rows = _rows_from_result(connection.execute(ai_spend_statement.sql, ai_spend_statement.params))
    return WorkspaceSummary(
        state="active",
        plan_key=plan_key,
        workspace=workspace,
        period=BalancePeriod(start_at=balance_row["period_start_at"], end_at=balance_row["period_end_at"]),
        units=units,
        entitlements=_build_entitlements(included, used, unlimited),
        ai_spend_usd=_compute_ai_spend_usd(ai_spend_rows),
    )


def read_library_overview(connection: SqlConnection, *, workspace_id: str, recent_limit: int = 10) -> LibraryOverview:
    counts_statement = _library_counts_sql(workspace_id=workspace_id)
    counts_row = _one(connection.execute(counts_statement.sql, counts_statement.params)) or {}
    counts = {
        "videos": _int(counts_row.get("videos")),
        "channels": _int(counts_row.get("channels")),
        "sources": _int(counts_row.get("sources")),
    }
    recent_statement = _library_recent_sql(workspace_id=workspace_id, limit=max(0, recent_limit))
    recent_rows = _rows_from_result(connection.execute(recent_statement.sql, recent_statement.params))
    recent = [
        LibraryVideo(
            video_id=str(row.get("youtube_video_id")),
            title=_optional_str(row.get("title")),
            channel_id=_optional_str(row.get("channel_id")),
            published_at=row.get("published_at"),
            duration_seconds=_int_or_none(row.get("duration_seconds")),
        )
        for row in recent_rows
    ]
    return LibraryOverview(counts=counts, recent=recent)


def read_active_account_grants(connection: SqlConnection, *, workspace_id: str) -> list[ConnectedAssistant]:
    statement = _assistants_sql(workspace_id=workspace_id)
    rows = _rows_from_result(connection.execute(statement.sql, statement.params))
    return [
        ConnectedAssistant(
            grant_id=str(row.get("id")),
            client_id=_optional_str(row.get("client_id")),
            scopes=_text_array(row.get("scopes")),
            audience=_optional_str(row.get("audience")),
            status=str(row.get("status") or "active"),
            token_version=_int_or_none(row.get("token_version")),
            created_at=row.get("created_at"),
            last_used_at=row.get("last_used_at"),
            expires_at=row.get("expires_at"),
        )
        for row in rows
    ]


def _one(result: Any) -> dict[str, Any] | None:
    rows = _rows_from_result(result)
    return rows[0] if rows else None


def _rows_from_result(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if hasattr(result, "mappings"):
        return [dict(row) for row in result.mappings()]
    if hasattr(result, "fetchall"):
        rows = result.fetchall()
    elif isinstance(result, (list, tuple)):
        rows = result
    elif isinstance(result, Iterable) and not isinstance(result, (str, bytes, Mapping)):
        rows = list(result)
    else:
        return []
    return [dict(row) for row in rows]


def _json_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        import json

        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _text_array(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return [item.strip().strip('"') for item in stripped[1:-1].split(",") if item.strip()]
        return [item for item in stripped.replace(",", " ").split() if item]
    return [str(value)]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "BalancePeriod",
    "ConnectedAssistant",
    "LibraryOverview",
    "LibraryVideo",
    "WorkspaceRef",
    "WorkspaceSummary",
    "WorkspaceUnit",
    "load_active_workspace",
    "read_active_account_grants",
    "read_library_overview",
    "read_workspace_summary",
]
