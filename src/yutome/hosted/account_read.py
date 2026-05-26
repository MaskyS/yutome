"""Read-only dashboard projections for the hosted web frontend.

This module backs the authenticated dashboard's read endpoints
(`/account/summary`, `/account/library`, `/account/assistants`). It is
deliberately separate from `entitlements.py`: the gate path there loads the
minimal `UsageGate` inputs and **fails closed** (missing policy/balance → deny),
whereas a dashboard read of an unprovisioned or out-of-period workspace should
**fail soft** — return a clear "no active plan" projection, never a denial.

SQL is raw, parameterized psycopg (`%(name)s`) to match the existing read paths
(`entitlements.py`); these are SELECTs, not the ordinary upserts being moved to a
SQLAlchemy Core metadata module. None of these reads is metered — they never go
through `UsageGate`, so rendering the dashboard cannot spend a workspace's
`queries` budget.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from yutome.hosted.models import jsonable_exact


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


class WorkspaceSummary(BaseModel):
    state: WorkspaceState
    plan_key: str | None = None
    workspace: WorkspaceRef
    period: BalancePeriod | None = None
    units: list[WorkspaceUnit] = Field(default_factory=list)


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


_ACTIVE_WORKSPACE_SQL = """
SELECT id, name, status
FROM workspaces
WHERE id = %(workspace_id)s
LIMIT 1;
""".strip()

_SUMMARY_POLICY_SQL = """
SELECT id, plan_key, included_units_jsonb
FROM entitlement_policies
WHERE workspace_id = %(workspace_id)s
  AND status = 'active'
ORDER BY updated_at DESC, created_at DESC, id
LIMIT 1;
""".strip()

_SUMMARY_BALANCE_SQL = """
SELECT period_start_at, period_end_at, used_units_jsonb, reserved_units_jsonb,
       remaining_units_jsonb, unlimited_units
FROM workspace_balances
WHERE workspace_id = %(workspace_id)s
  AND entitlement_policy_id = %(entitlement_policy_id)s
  AND period_start_at <= now()
  AND period_end_at > now()
LIMIT 1;
""".strip()

_LIBRARY_COUNTS_SQL = """
SELECT
    (SELECT count(*) FROM videos WHERE workspace_id = %(workspace_id)s) AS videos,
    (SELECT count(DISTINCT channel_id) FROM videos
        WHERE workspace_id = %(workspace_id)s AND channel_id IS NOT NULL) AS channels,
    (SELECT count(*) FROM sources
        WHERE workspace_id = %(workspace_id)s AND status = 'active') AS sources;
""".strip()

_LIBRARY_RECENT_SQL = """
SELECT youtube_video_id, title, channel_id, published_at, duration_seconds
FROM videos
WHERE workspace_id = %(workspace_id)s
ORDER BY published_at DESC NULLS LAST, created_at DESC
LIMIT %(limit)s;
""".strip()

_ASSISTANTS_SQL = """
SELECT id, client_id, scopes, audience, status, token_version,
       created_at, last_used_at, expires_at
FROM account_grants
WHERE workspace_id = %(workspace_id)s
  AND status = 'active'
  AND kind = 'mcp_client'
ORDER BY created_at DESC;
""".strip()


def load_active_workspace(connection: SqlConnection, *, workspace_id: str) -> dict[str, Any] | None:
    """Return the workspace row when it exists and is active, else None.

    Used by the API auth layer to 404 a verified session whose workspace has
    been removed or disabled, rather than serving an empty dashboard.
    """

    row = _one(connection.execute(_ACTIVE_WORKSPACE_SQL, {"workspace_id": workspace_id}))
    if row is None or str(row.get("status")) != "active":
        return None
    return row


def read_workspace_summary(connection: SqlConnection, *, workspace_id: str) -> WorkspaceSummary:
    workspace_row = _one(connection.execute(_ACTIVE_WORKSPACE_SQL, {"workspace_id": workspace_id}))
    workspace = WorkspaceRef(
        id=workspace_id,
        name=_optional_str(workspace_row.get("name")) if workspace_row else None,
    )

    policy_row = _one(connection.execute(_SUMMARY_POLICY_SQL, {"workspace_id": workspace_id}))
    if policy_row is None:
        return WorkspaceSummary(state="no_active_plan", workspace=workspace)

    plan_key = _optional_str(policy_row.get("plan_key"))
    included = _json_mapping(policy_row.get("included_units_jsonb"))

    balance_row = _one(
        connection.execute(
            _SUMMARY_BALANCE_SQL,
            {"workspace_id": workspace_id, "entitlement_policy_id": str(policy_row["id"])},
        )
    )
    if balance_row is None:
        # Policy exists but no balance for the current period: show the plan and
        # its included allowances, but mark the period inactive.
        units = [
            WorkspaceUnit(unit=unit, included=jsonable_exact(value))
            for unit, value in sorted(included.items())
        ]
        return WorkspaceSummary(state="no_active_plan", plan_key=plan_key, workspace=workspace, units=units)

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
    return WorkspaceSummary(
        state="active",
        plan_key=plan_key,
        workspace=workspace,
        period=BalancePeriod(start_at=balance_row["period_start_at"], end_at=balance_row["period_end_at"]),
        units=units,
    )


def read_library_overview(connection: SqlConnection, *, workspace_id: str, recent_limit: int = 10) -> LibraryOverview:
    counts_row = _one(connection.execute(_LIBRARY_COUNTS_SQL, {"workspace_id": workspace_id})) or {}
    counts = {
        "videos": _int(counts_row.get("videos")),
        "channels": _int(counts_row.get("channels")),
        "sources": _int(counts_row.get("sources")),
    }
    recent_rows = _rows_from_result(
        connection.execute(_LIBRARY_RECENT_SQL, {"workspace_id": workspace_id, "limit": max(0, recent_limit)})
    )
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
    rows = _rows_from_result(connection.execute(_ASSISTANTS_SQL, {"workspace_id": workspace_id}))
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
