from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from yutome.hosted.account import (
    DEFAULT_ACCOUNT_SESSION_AUDIENCE,
    AccountSessionError,
    sign_account_session_token,
    verify_account_session_token,
)
from yutome.hosted.http_api import (
    ACCOUNT_SESSION_TOKEN_HEADER,
    WORKSPACE_HEADER,
    build_app,
    error_body,
)
from yutome.hosted.mcp_query import HostedMcpQueryAdapter

MCP_TOKEN = "mcp-test-token"
DASHBOARD_TOKEN = "dashboard-test-token"
HMAC_SECRET = "account-session-secret"


class _NoopSearchStore:
    """Minimal stand-in; the /account/* routes never touch search."""


class RoutingConnection:
    """Fake psycopg connection that returns canned rows per SQL statement.

    Unlike the shared RecordingConnection (same rows for every call), the
    summary endpoint issues three different SELECTs, so the fake must route.
    """

    def __init__(
        self,
        *,
        workspace: list[dict[str, Any]] | None = None,
        policy: list[dict[str, Any]] | None = None,
        balance: list[dict[str, Any]] | None = None,
        counts: list[dict[str, Any]] | None = None,
        recent: list[dict[str, Any]] | None = None,
        grants: list[dict[str, Any]] | None = None,
    ) -> None:
        self.workspace = workspace if workspace is not None else [{"id": "ws_pg", "name": "Demo", "status": "active"}]
        self.policy = policy or []
        self.balance = balance or []
        self.counts = counts or []
        self.recent = recent or []
        self.grants = grants or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((statement, dict(params or {})))
        if "FROM workspaces" in statement:
            return self.workspace
        if "FROM entitlement_policies" in statement:
            return self.policy
        if "FROM workspace_balances" in statement:
            return self.balance
        if "FROM account_grants" in statement:
            return self.grants
        if "AS videos" in statement:  # combined counts subquery
            return self.counts
        if "youtube_video_id" in statement:  # recent videos
            return self.recent
        return []


def build_account_app(connection: RoutingConnection, *, account_session_ttl_seconds: int = 3600) -> TestClient:
    adapter = HostedMcpQueryAdapter(search_store=_NoopSearchStore())
    return TestClient(
        build_app(
            adapter=adapter,
            billing_connection=connection,
            expected_api_token=MCP_TOKEN,
            expected_account_api_token=DASHBOARD_TOKEN,
            account_session_secret=HMAC_SECRET,
            account_session_audience=DEFAULT_ACCOUNT_SESSION_AUDIENCE,
            account_session_ttl_seconds=account_session_ttl_seconds,
        )
    )


def mint_session(
    *,
    workspace_id: str = "ws_pg",
    user_id: str = "usr_pg",
    secret: str = HMAC_SECRET,
    ttl_seconds: int = 3600,
    audience: str = DEFAULT_ACCOUNT_SESSION_AUDIENCE,
    issued_at: datetime | None = None,
) -> str:
    now = issued_at or datetime.now(timezone.utc)
    return sign_account_session_token(
        user_id=user_id,
        workspace_id=workspace_id,
        secret=secret,
        expires_at=now + timedelta(seconds=ttl_seconds),
        issued_at=now,
        audience=audience,
    )


def account_headers(session_token: str, *, token: str = DASHBOARD_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", ACCOUNT_SESSION_TOKEN_HEADER: session_token}


def _period_rows() -> list[dict[str, Any]]:
    start = datetime.now(timezone.utc) - timedelta(days=1)
    end = start + timedelta(days=31)
    return [
        {
            "period_start_at": start,
            "period_end_at": end,
            "used_units_jsonb": {"queries": 12},
            "reserved_units_jsonb": {"queries": 3},
            "remaining_units_jsonb": {"queries": 9985, "vectors": 10000},
            "unlimited_units": ["vectors"],
        }
    ]


def test_account_summary_returns_plan_and_units() -> None:
    connection = RoutingConnection(
        policy=[{"id": "pol_pg", "plan_key": "starter", "included_units_jsonb": {"queries": 10000, "vectors": 10000}}],
        balance=_period_rows(),
    )
    client = build_account_app(connection)

    response = client.get("/account/summary", headers=account_headers(mint_session()))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["state"] == "active"
    assert body["plan_key"] == "starter"
    assert body["workspace"] == {"id": "ws_pg", "name": "Demo"}
    assert body["period"]["start_at"] and body["period"]["end_at"]
    units = {unit["unit"]: unit for unit in body["units"]}
    assert units["queries"] == {
        "unit": "queries",
        "included": 10000,
        "used": 12,
        "reserved": 3,
        "remaining": 9985,
        "unlimited": False,
    }
    assert units["vectors"]["unlimited"] is True
    # Derived from the verified session, not a client header.
    statements = [statement for statement, _ in connection.calls]
    assert any("FROM entitlement_policies" in s for s in statements)
    assert any("FROM workspace_balances" in s for s in statements)
    assert all(params.get("workspace_id") == "ws_pg" for _, params in connection.calls)


def test_account_summary_fail_soft_without_balance() -> None:
    connection = RoutingConnection(
        policy=[{"id": "pol_pg", "plan_key": "starter", "included_units_jsonb": {"queries": 10000}}],
        balance=[],
    )
    client = build_account_app(connection)

    response = client.get("/account/summary", headers=account_headers(mint_session()))

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "no_active_plan"
    assert body["plan_key"] == "starter"
    assert body["period"] is None
    units = {unit["unit"]: unit for unit in body["units"]}
    assert units["queries"]["included"] == 10000
    assert units["queries"]["remaining"] is None


def test_account_summary_no_policy_is_no_active_plan() -> None:
    connection = RoutingConnection(policy=[])
    client = build_account_app(connection)

    response = client.get("/account/summary", headers=account_headers(mint_session()))

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "no_active_plan"
    assert body["plan_key"] is None
    assert body["units"] == []


def test_account_summary_decimal_units_serialize_exactly() -> None:
    connection = RoutingConnection(
        policy=[{"id": "pol_pg", "plan_key": "starter", "included_units_jsonb": {"bytes": 1024}}],
        balance=[
            {
                "period_start_at": datetime.now(timezone.utc),
                "period_end_at": datetime.now(timezone.utc) + timedelta(days=1),
                "used_units_jsonb": {},
                "reserved_units_jsonb": {},
                "remaining_units_jsonb": {"bytes": Decimal("9985.5")},
                "unlimited_units": [],
            }
        ],
    )
    client = build_account_app(connection)

    response = client.get("/account/summary", headers=account_headers(mint_session()))

    assert response.status_code == 200
    units = {unit["unit"]: unit for unit in response.json()["units"]}
    assert units["bytes"]["remaining"] == "9985.5"  # exact string, never a lossy float


def test_account_workspace_not_found_returns_404() -> None:
    connection = RoutingConnection(workspace=[])  # missing/inactive workspace
    client = build_account_app(connection)

    response = client.get("/account/summary", headers=account_headers(mint_session()))

    assert response.status_code == 404
    assert error_body(response.json())["code"] == "workspace_not_found"


def test_account_library_counts_and_recent() -> None:
    published = datetime.now(timezone.utc)
    connection = RoutingConnection(
        counts=[{"videos": 42, "channels": 3, "sources": 5}],
        recent=[
            {
                "youtube_video_id": "vidA",
                "title": "Hello",
                "channel_id": "chan1",
                "published_at": published,
                "duration_seconds": 600,
            }
        ],
    )
    client = build_account_app(connection)

    response = client.get("/account/library", headers=account_headers(mint_session()))

    assert response.status_code == 200
    body = response.json()
    assert body["counts"] == {"videos": 42, "channels": 3, "sources": 5}
    assert body["recent"][0]["video_id"] == "vidA"
    assert body["recent"][0]["duration_seconds"] == 600


def test_account_assistants_lists_active_grants_without_secrets() -> None:
    connection = RoutingConnection(
        grants=[
            {
                "id": "grant_1",
                "client_id": "claude",
                "scopes": ["yutome.search.read"],
                "audience": "https://mcp.yutome.com/mcp",
                "status": "active",
                "token_version": 1,
                "created_at": datetime.now(timezone.utc),
                "last_used_at": None,
                "expires_at": None,
            }
        ]
    )
    client = build_account_app(connection)

    response = client.get("/account/assistants", headers=account_headers(mint_session()))

    assert response.status_code == 200
    body = response.json()
    assert body["assistants"][0]["grant_id"] == "grant_1"
    assert body["assistants"][0]["client_id"] == "claude"
    assert body["assistants"][0]["scopes"] == ["yutome.search.read"]
    # token_version is legitimate metadata; ensure no secret material leaks.
    assert "token_version" in body["assistants"][0]
    for forbidden in ("session_hash", "secret", "hmac", "password"):
        assert forbidden not in response.text.lower()


def test_account_endpoints_reject_mcp_token() -> None:
    connection = RoutingConnection()
    client = build_account_app(connection)

    # The MCP query token must not authorize dashboard reads.
    response = client.get("/account/summary", headers=account_headers(mint_session(), token=MCP_TOKEN))
    assert response.status_code == 401
    assert error_body(response.json())["code"] == "api_token_invalid"


def test_tools_call_rejects_dashboard_token() -> None:
    connection = RoutingConnection()
    client = build_account_app(connection)

    # The dashboard token must not authorize the MCP query plane.
    response = client.post(
        "/tools/call",
        json={"name": "find", "arguments": {"text": "x"}},
        headers={WORKSPACE_HEADER: "ws_pg", "Authorization": f"Bearer {DASHBOARD_TOKEN}"},
    )
    assert response.status_code == 401
    assert error_body(response.json())["code"] == "api_token_invalid"


def test_account_session_required_when_header_absent() -> None:
    connection = RoutingConnection()
    client = build_account_app(connection)

    response = client.get("/account/summary", headers={"Authorization": f"Bearer {DASHBOARD_TOKEN}"})

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "account_session_required"


def test_account_session_invalid_signature_rejected() -> None:
    connection = RoutingConnection()
    client = build_account_app(connection)
    tampered = mint_session()[:-3] + ("aaa" if not mint_session().endswith("aaa") else "bbb")

    response = client.get("/account/summary", headers=account_headers(tampered))

    assert response.status_code == 401
    assert error_body(response.json())["code"] in {"account_session_invalid", "account_session_malformed"}


def test_account_session_wrong_audience_rejected() -> None:
    connection = RoutingConnection()
    client = build_account_app(connection)
    foreign = mint_session(audience="someone-elses-audience")

    response = client.get("/account/summary", headers=account_headers(foreign))

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "account_session_audience_mismatch"


def test_account_session_max_age_is_enforced_for_account_reads() -> None:
    connection = RoutingConnection()
    client = build_account_app(connection, account_session_ttl_seconds=60)
    old_but_unexpired = mint_session(
        ttl_seconds=3600,
        issued_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )

    response = client.get("/account/summary", headers=account_headers(old_but_unexpired))

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "account_session_expired"


def test_verify_account_session_token_roundtrip_and_rejections() -> None:
    now = datetime.now(timezone.utc)
    token = sign_account_session_token(
        user_id="usr_a",
        workspace_id="ws_a",
        secret=HMAC_SECRET,
        expires_at=now + timedelta(hours=1),
        issued_at=now,
        workspace_ids=["ws_a", "ws_b"],
        session_id="sess_1",
    )
    claims = verify_account_session_token(token, secret=HMAC_SECRET, now=now)
    assert claims.user_id == "usr_a"
    assert claims.workspace_id == "ws_a"
    assert claims.workspace_ids == ("ws_a", "ws_b")
    assert claims.session_id == "sess_1"

    with pytest.raises(AccountSessionError):
        verify_account_session_token(token, secret="wrong", now=now)
    with pytest.raises(AccountSessionError):
        verify_account_session_token(token, secret=HMAC_SECRET, now=now + timedelta(hours=2))
    with pytest.raises(AccountSessionError):
        verify_account_session_token("not.a.token", secret=HMAC_SECRET, now=now)
