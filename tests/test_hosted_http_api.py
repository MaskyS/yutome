from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from datetime import datetime, timedelta, timezone

from yutome import contract
from yutome.hosted.allocation_policy import default_search_store_allocation
from yutome.hosted.account import (
    DEFAULT_ACCOUNT_SESSION_AUDIENCE,
    session_token_hash,
    sign_account_session_token,
)
from yutome.hosted.api_keys import API_KEY_PREFIX, api_key_hash
from yutome.hosted.http_api import (
    ACCOUNT_SESSION_COOKIE_NAME,
    ACCOUNT_SESSION_HMAC_SECRET_ENV_VAR,
    ACCOUNT_SESSION_TOKEN_HEADER,
    ACCOUNT_SESSION_TTL_SECONDS,
    TOKEN_ENV_VAR,
    WORKSPACE_HEADER,
    build_app,
    build_postgres_app,
    error_body,
)
from yutome.hosted.google_signin_service import GoogleSignInSettings
from yutome.hosted.mcp_query import HostedMcpAuthContext, HostedMcpQueryAdapter, HostedMcpUsageContext
from yutome.hosted.models import EntitlementPolicy, UsageEvent, WorkspaceBalance
from yutome.hosted.search_store import SearchStoreUsage
from yutome.youtube_oauth import OAuthClient


TEST_API_TOKEN = "hosted-test-token"


def auth_headers(workspace_id: str, *, token: str = TEST_API_TOKEN) -> dict[str, str]:
    return {WORKSPACE_HEADER: workspace_id, "Authorization": f"Bearer {token}"}


def stripe_signature_header(raw_body: bytes, *, secret: str = "whsec_test") -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = hmac.new(secret.encode(), timestamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return {"stripe-signature": f"t={timestamp},v1={signature}"}


def decode_base64url_json(value: str) -> dict[str, Any]:
    padded = value + ("=" * (-len(value) % 4))
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))


def _allow_search_usage_context(
    auth: HostedMcpAuthContext,
    operation: str,
    estimated_units: dict[str, Any],
) -> HostedMcpUsageContext:
    return HostedMcpUsageContext(
        allocation=default_search_store_allocation(workspace_id=auth.workspace_id, operation=operation),
        policy=EntitlementPolicy(
            id="policy_http",
            workspace_id=auth.workspace_id,
            allowed_operations={f"search_store.{operation}"},
        ),
        balance=WorkspaceBalance(workspace_id=auth.workspace_id, unlimited_units=set(estimated_units)),
    )


class RecordingSearchStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.resources: dict[tuple[str, str, str], dict[str, Any]] = {
            (
                "ws_http",
                "chunk",
                "chunk_http",
            ): {
                "chunk_id": "chunk_http",
                "resource_uri": "yutome://chunk/chunk_http",
                "video_id": "vid_http",
                "youtube_url": "https://youtube.com/watch?v=vid_http&t=3s",
                "start_ms": 3000,
                "end_ms": 8000,
                "text": "Hosted Crohn query result",
            }
        }

    def lexical_search(self, *, workspace_id: str, query: str, limit: int) -> tuple[list[dict[str, Any]], SearchStoreUsage]:
        self.calls.append({"workspace_id": workspace_id, "query": query, "limit": limit})
        return [
            {
                "chunk_id": "chunk_http",
                "video_id": "vid_http",
                "transcript_version_id": "tx_http",
                "start_seconds": 3,
                "end_seconds": 8,
                "text": "Hosted Crohn query result",
                "lexical_score": 0.4,
                "score": 0.4,
                "match_type": "lexical",
            }
        ], SearchStoreUsage(
            operation="lexical_query",
            backend="vectorchord_bm25",
            index_profile_ref="sip_default",
            units={"queries": 1, "candidate_limit": limit, "result_count": 1, "latency_ms": 1.2},
        )

    def resource_chunk(self, *, workspace_id: str, chunk_id: str) -> dict[str, Any]:
        self.calls.append({"resource": "chunk", "workspace_id": workspace_id, "id": chunk_id})
        return self._resource(workspace_id, "chunk", chunk_id)

    def resource_video(self, *, workspace_id: str, video_id: str) -> dict[str, Any]:
        self.calls.append({"resource": "video", "workspace_id": workspace_id, "id": video_id})
        return self._resource(workspace_id, "video", video_id)

    def resource_channel(self, *, workspace_id: str, channel_id: str) -> dict[str, Any]:
        self.calls.append({"resource": "channel", "workspace_id": workspace_id, "id": channel_id})
        return self._resource(workspace_id, "channel", channel_id)

    def resource_transcript(
        self,
        *,
        workspace_id: str,
        transcript_version_id: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "resource": "transcript",
                "workspace_id": workspace_id,
                "id": transcript_version_id,
                "offset": offset,
                "limit": limit,
            }
        )
        return self._resource(workspace_id, "transcript", transcript_version_id)

    def resource_source(self, *, workspace_id: str, source_id: str) -> dict[str, Any]:
        self.calls.append({"resource": "source", "workspace_id": workspace_id, "id": source_id})
        return self._resource(workspace_id, "source", source_id)

    def list_status(self, *, workspace_id: str) -> dict[str, Any]:
        self.calls.append({"list": "status", "workspace_id": workspace_id})
        return {
            "searchable_now": 1,
            "still_indexing": 0,
            "needs_attention": 0,
            "channels": 1,
            "videos": 1,
            "chunks": 1,
            "transcript_versions": 1,
            "statuses": {"indexed": 1},
        }

    def list_videos(
        self,
        *,
        workspace_id: str,
        limit: int,
        offset: int = 0,
        channel: str | None = None,
        video_id: str | None = None,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "list": "videos",
                "workspace_id": workspace_id,
                "limit": limit,
                "offset": offset,
                "channel": channel,
                "video_id": video_id,
                "order_by": order_by,
            }
        )
        return [{"video_id": video_id or "vid_http", "resource_uri": "yutome://video/vid_http", "title": "HTTP video"}]

    def list_channels(
        self,
        *,
        workspace_id: str,
        limit: int,
        offset: int = 0,
        channel: str | None = None,
        selected: bool | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "list": "channels",
                "workspace_id": workspace_id,
                "limit": limit,
                "offset": offset,
                "channel": channel,
                "selected": selected,
            }
        )
        return [{"channel_id": channel or "chan_http", "resource_uri": "yutome://channel/chan_http", "selected": selected}]

    def _resource(self, workspace_id: str, kind: str, id_: str) -> dict[str, Any]:
        from yutome.hosted.resources import HostedResourceNotFound

        try:
            return self.resources[(workspace_id, kind, id_)]
        except KeyError as exc:
            raise HostedResourceNotFound(kind=kind, id_=id_) from exc


class RecordingConnection:
    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        account_session_rows: list[dict[str, Any]] | None = None,
        account_session_update_rows: list[dict[str, Any]] | None = None,
        workspace_row: dict[str, Any] | None = None,
        stripe_customer_rows: list[dict[str, Any]] | None = None,
        workspace_subscription_status: str = "trialing",
        workspace_trial_ends_at: Any = "2999-01-01T00:00:00+00:00",
    ) -> None:
        self.rows = rows or []
        self.account_session_rows = account_session_rows or []
        self.account_session_update_rows = account_session_update_rows or []
        self.workspace_row = workspace_row
        self.stripe_customer_rows = stripe_customer_rows or []
        self.workspace_subscription_status = workspace_subscription_status
        self.workspace_trial_ends_at = workspace_trial_ends_at
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((statement, dict(params or {})))
        if "FROM account_sessions" in statement:
            return list(self.account_session_rows)
        if "UPDATE account_sessions" in statement:
            return list(self.account_session_update_rows)
        if "FROM workspaces" in statement and "owner_user_id" in statement:
            if self.workspace_row is None:
                return []
            return [dict(self.workspace_row)] if params.get("workspace_id") == self.workspace_row["id"] else []
        if "FROM stripe_customers" in statement and "LIMIT" in statement:
            return [
                dict(row)
                for row in self.stripe_customer_rows
                if row.get("workspace_id") == params.get("workspace_id")
            ]
        # The entitlement provider checks trial-expiry before loading policy/balance; model an
        # entitled (trialing) workspace by default so denials fall through to the path under
        # test rather than short-circuiting at the trial gate.
        if "FROM workspaces" in statement and "subscription_status" in statement:
            return [
                {
                    "subscription_status": self.workspace_subscription_status,
                    "trial_ends_at": self.workspace_trial_ends_at,
                }
            ]
        return self.rows


class RecordingLedger:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    def append(self, event: UsageEvent) -> None:
        self.events.append(event)


@pytest.fixture
def hosted_http_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, RecordingSearchStore, RecordingLedger]:
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    store = RecordingSearchStore()
    ledger = RecordingLedger()
    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=ledger,
        usage_context_provider=_allow_search_usage_context,
    )
    return TestClient(build_app(adapter=adapter, expected_api_token=TEST_API_TOKEN)), store, ledger


def _tool_call_payload() -> dict[str, Any]:
    return {"name": "find", "arguments": {"text": "Crohn", "mode": "lexical", "limit": 4}}


def _rate_limited_client(*, requests_per_minute: int) -> TestClient:
    store = RecordingSearchStore()
    ledger = RecordingLedger()
    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=ledger,
        usage_context_provider=_allow_search_usage_context,
    )
    return TestClient(
        build_app(
            adapter=adapter,
            expected_api_token=TEST_API_TOKEN,
            requests_per_minute=requests_per_minute,
        )
    )


def test_health_endpoint_shape(hosted_http_client: tuple[TestClient, RecordingSearchStore, RecordingLedger]) -> None:
    client, _, _ = hosted_http_client

    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["service"] == "yutome-hosted-mcp"
    assert body["contract"]["auth_scope"] == "yutome.search.read"
    assert "find" in body["contract"]["tools"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_rate_gate_returns_429_with_retry_after_and_ratelimit_headers() -> None:
    client = _rate_limited_client(requests_per_minute=2)

    first = client.post("/tools/call", json=_tool_call_payload(), headers=auth_headers("ws_http"))
    second = client.post("/tools/call", json=_tool_call_payload(), headers=auth_headers("ws_http"))
    limited = client.post("/tools/call", json=_tool_call_payload(), headers=auth_headers("ws_http"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert limited.status_code == 429
    assert limited.headers["Retry-After"]
    assert limited.headers["RateLimit-Limit"] == "2"
    assert limited.headers["RateLimit-Remaining"] == "0"
    assert limited.headers["RateLimit-Reset"]
    assert error_body(limited.json())["code"] == "rate_limited"


def test_rate_gate_exempts_healthz_and_readyz() -> None:
    client = _rate_limited_client(requests_per_minute=1)

    responses = [client.get(path) for _ in range(4) for path in ("/healthz", "/readyz")]

    assert [response.status_code for response in responses] == [200] * len(responses)


def test_rate_gate_exempts_stripe_webhook() -> None:
    connection = RecordingConnection()
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store)
    client = TestClient(
        build_app(
            adapter=adapter,
            billing_connection=connection,
            stripe_webhook_secret="whsec_test",
            requests_per_minute=1,
        )
    )

    responses = []
    for index in range(4):
        raw_body = json.dumps(
            {
                "id": f"evt_rate_{index}",
                "type": "checkout.session.completed",
                "created": 1779753600,
                "data": {
                    "object": {
                        "customer": "cus_123",
                        "subscription": "sub_123",
                        "status": "complete",
                        "metadata": {"workspace_id": "ws_http"},
                    }
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        responses.append(client.post("/webhooks/stripe", content=raw_body, headers=stripe_signature_header(raw_body)))

    assert all(response.status_code != 429 for response in responses)
    assert all(error_body(response.json()).get("code") != "rate_limited" for response in responses)


def test_rate_gate_uses_proxy_appended_client_ip_for_unauthenticated_requests() -> None:
    client = _rate_limited_client(requests_per_minute=1)

    first_real_ip = client.post(
        "/tools/call",
        json=_tool_call_payload(),
        headers={"X-Forwarded-For": "198.51.100.10, 203.0.113.10"},
    )
    second_real_ip = client.post(
        "/tools/call",
        json=_tool_call_payload(),
        headers={"X-Forwarded-For": "198.51.100.10, 203.0.113.11"},
    )
    spoofed_leftmost = client.post(
        "/tools/call",
        json=_tool_call_payload(),
        headers={"X-Forwarded-For": "198.51.100.99, 203.0.113.10"},
    )

    assert first_real_ip.status_code == 401
    assert error_body(first_real_ip.json())["code"] == "api_token_required"
    assert second_real_ip.status_code == 401
    assert error_body(second_real_ip.json())["code"] == "api_token_required"
    assert spoofed_leftmost.status_code == 429
    assert error_body(spoofed_leftmost.json())["code"] == "rate_limited"


def test_rate_gate_does_not_key_on_unvalidated_workspace_header() -> None:
    client = _rate_limited_client(requests_per_minute=1)

    allowed_a = client.post("/tools/call", json=_tool_call_payload(), headers=auth_headers("ws_rate_a"))
    limited_b = client.post("/tools/call", json=_tool_call_payload(), headers=auth_headers("ws_rate_b"))

    assert allowed_a.status_code == 200
    assert limited_b.status_code == 429
    assert error_body(limited_b.json())["code"] == "rate_limited"


def test_successful_response_carries_ratelimit_headers() -> None:
    client = _rate_limited_client(requests_per_minute=2)

    response = client.post("/tools/call", json=_tool_call_payload(), headers=auth_headers("ws_http"))

    assert response.status_code == 200
    assert response.headers["RateLimit-Limit"] == "2"
    assert response.headers["RateLimit-Remaining"] == "1"
    assert response.headers["RateLimit-Reset"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_readyz_uses_injected_readiness_check_without_live_db(
    hosted_http_client: tuple[TestClient, RecordingSearchStore, RecordingLedger],
) -> None:
    _client, store, _ledger = hosted_http_client
    adapter = HostedMcpQueryAdapter(search_store=store)
    client = TestClient(
        build_app(
            adapter=adapter,
            readiness_check=lambda: {"ok": True, "database_reachable": False, "extensions": {}},
        )
    )

    response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["checks"]["database_reachable"] is False
    assert store.calls == []


def test_readyz_returns_503_when_readiness_check_reports_not_ready(
    hosted_http_client: tuple[TestClient, RecordingSearchStore, RecordingLedger],
) -> None:
    _client, store, _ledger = hosted_http_client
    adapter = HostedMcpQueryAdapter(search_store=store)
    client = TestClient(
        build_app(
            adapter=adapter,
            readiness_check=lambda: {"ok": False, "error": "postgres_url_missing"},
        )
    )

    response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["checks"]["error"] == "postgres_url_missing"
    assert store.calls == []


def test_readyz_sanitizes_readiness_exception() -> None:
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store)

    def leaking_readiness_check() -> dict[str, Any]:
        raise RuntimeError("psycopg failed for postgresql://user:secret@db.internal/yutome")

    client = TestClient(build_app(adapter=adapter, readiness_check=leaking_readiness_check))

    response = client.get("/readyz")

    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["checks"] == {"ok": False, "error": "readiness_check_failed"}
    assert "secret" not in response.text
    assert "postgresql://" not in response.text
    assert "psycopg" not in response.text


def test_postgres_app_builder_wires_connection_search_store_and_adapter() -> None:
    connection = RecordingConnection(
        rows=[
            {
                "chunk_id": "chunk_pg",
                "video_id": "vid_pg",
                "transcript_version_id": "tx_pg",
                "start_seconds": 10,
                "end_seconds": 12,
                "text": "Postgres-backed hosted MCP result",
                "score": 0.8,
                "match_type": "lexical",
            }
        ]
    )
    app = build_postgres_app(
        connection=connection,
        expected_api_token=TEST_API_TOKEN,
        usage_context_provider=_allow_search_usage_context,
    )
    client = TestClient(app)

    response = client.post(
        "/mcp/tools/call",
        json={"name": "find", "arguments": {"text": "Postgres", "mode": "lexical", "limit": 2}},
        headers=auth_headers("ws_pg"),
    )

    assert response.status_code == 200
    assert response.json()["result"]["rows"][0]["chunk_id"] == "chunk_pg"
    assert app.state.hosted_connection is connection
    assert app.state.hosted_search_store.connection is connection
    assert app.state.hosted_adapter.search_store is app.state.hosted_search_store
    assert len(connection.calls) == 1
    assert "to_bm25query" in connection.calls[0][0]
    assert "bm25_catalog.bm25_limit" in connection.calls[0][0]
    assert connection.calls[0][1]["workspace_id"] == "ws_pg"
    assert connection.calls[0][1]["limit"] == 2


def test_postgres_app_builder_default_usage_context_denies_without_entitlement() -> None:
    connection = RecordingConnection()
    app = build_postgres_app(connection=connection, expected_api_token=TEST_API_TOKEN)
    client = TestClient(app)

    response = client.post(
        "/mcp/tools/call",
        json={"name": "find", "arguments": {"text": "Postgres", "mode": "lexical", "limit": 2}},
        headers=auth_headers("ws_pg"),
    )

    assert response.status_code == 403
    body = error_body(response.json())
    assert body["code"] == "usage_denied"
    assert body["data"]["operation"] == "search_store.lexical_query"
    assert all("websearch_to_tsquery" not in statement for statement, _params in connection.calls)
    assert any("FROM entitlement_policies" in statement for statement, _params in connection.calls)


def test_stripe_webhook_rejects_invalid_signature_before_db_write() -> None:
    connection = RecordingConnection()
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store)
    client = TestClient(
        build_app(
            adapter=adapter,
            billing_connection=connection,
            stripe_webhook_secret="whsec_test",
        )
    )
    raw_body = b'{"id":"evt_1","type":"checkout.session.completed","data":{"object":{}}}'
    headers = stripe_signature_header(raw_body)
    headers["stripe-signature"] = headers["stripe-signature"].rsplit("=", 1)[0] + "=deadbeef"

    response = client.post("/webhooks/stripe", content=raw_body, headers=headers)

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "webhook_signature_invalid"
    assert connection.calls == []


def test_stripe_webhook_accepts_signature_and_upserts_customer_exactly_once() -> None:
    connection = RecordingConnection()
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store)
    client = TestClient(
        build_app(
            adapter=adapter,
            billing_connection=connection,
            stripe_webhook_secret="whsec_test",
        )
    )
    raw_body = json.dumps(
        {
            "id": "evt_123",
            "type": "checkout.session.completed",
            "created": 1779753600,
            "data": {
                "object": {
                    "customer": "cus_123",
                    "subscription": "sub_123",
                    "status": "complete",
                    "metadata": {"workspace_id": "ws_http"},
                }
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    response = client.post("/webhooks/stripe", content=raw_body, headers=stripe_signature_header(raw_body))

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["event_id"] == "evt_123"
    assert body["event_type"] == "checkout.session.completed"
    assert body["stripe_customer"].startswith("sc_")
    inserts = [call[0].split()[2] for call in connection.calls if call[0].startswith("INSERT INTO")]
    # snapshot insert (exactly-once via PK), customer upsert, then — because `complete` normalizes
    # to the entitled `active` status — provision the starter price book, EntitlementPolicy, and
    # WorkspaceBalance, and finally finalize the snapshot.
    assert inserts == [
        "stripe_webhook_events",
        "stripe_customers",
        "price_books",
        "entitlement_policies",
        "workspace_balances",
        "stripe_webhook_events",
    ]
    # The webhook snapshot is keyed by the Stripe event id, deduping replays via PK conflict.
    assert "evt_123" in connection.calls[0][1].values()


def _billing_client_with_customer(
    monkeypatch: pytest.MonkeyPatch, *, customer_row: dict[str, Any] | None
) -> tuple[TestClient, list[tuple[str, dict[str, str]]]]:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setenv("STRIPE_PRICE_ID", "price_metered_123")
    monkeypatch.setenv("STRIPE_SEAT_PRICE_ID", "price_seat_4usd")
    monkeypatch.setenv("STRIPE_CHECKOUT_SUCCESS_URL", "https://app.example.test/billing/success")
    monkeypatch.setenv("STRIPE_CHECKOUT_CANCEL_URL", "https://app.example.test/billing/cancel")
    monkeypatch.setenv("STRIPE_PORTAL_RETURN_URL", "https://app.example.test/billing")
    posted: list[tuple[str, dict[str, str]]] = []

    def fake_stripe_post(path: str, form: dict[str, str]) -> dict[str, Any]:
        posted.append((path, dict(form)))
        if path == "/v1/customers":
            return {"id": "cus_created_1"}
        if path == "/v1/checkout/sessions":
            return {"id": "cs_1", "url": "https://checkout.stripe.test/cs_1"}
        if path == "/v1/billing_portal/sessions":
            return {"id": "bps_1", "url": "https://billing.stripe.test/bps_1"}
        return {}

    monkeypatch.setattr("yutome.hosted.http_api._stripe_post", fake_stripe_post)

    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store)
    rows = [{"id": "ws_http", "name": "Workspace", "status": "active"}]
    if customer_row is not None:
        rows = [customer_row]
    connection = RecordingConnection(rows=rows)
    app = build_app(
        adapter=adapter,
        billing_connection=connection,
        expected_account_api_token=ACCOUNT_DASHBOARD_TOKEN,
        account_session_secret=ACCOUNT_SESSION_SECRET,
    )
    return TestClient(app), posted


def test_billing_checkout_requires_auth() -> None:
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store)
    connection = RecordingConnection(rows=[{"id": "ws_http", "name": "Workspace", "status": "active"}])
    client = TestClient(
        build_app(
            adapter=adapter,
            billing_connection=connection,
            expected_account_api_token=ACCOUNT_DASHBOARD_TOKEN,
            account_session_secret=ACCOUNT_SESSION_SECRET,
        )
    )

    response = client.post("/billing/checkout", headers={"Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}"})

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "account_session_required"


def test_billing_checkout_creates_customer_and_returns_url(monkeypatch: pytest.MonkeyPatch) -> None:
    client, posted = _billing_client_with_customer(monkeypatch, customer_row=None)

    response = client.post(
        "/billing/checkout",
        headers=_account_headers(_account_session_token("ws_http")),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["url"] == "https://checkout.stripe.test/cs_1"
    paths = [path for path, _form in posted]
    assert paths == ["/v1/customers", "/v1/checkout/sessions"]
    checkout_form = posted[1][1]
    assert checkout_form["mode"] == "subscription"
    assert checkout_form["customer"] == "cus_created_1"
    # Personal plan subscribes to BOTH line items: flat $4 seat (quantity 1) + metered overage.
    assert checkout_form["line_items[0][price]"] == "price_seat_4usd"
    assert checkout_form["line_items[0][quantity]"] == "1"
    assert checkout_form["line_items[1][price]"] == "price_metered_123"
    # The metered overage price must omit quantity (only the seat carries a quantity).
    assert "line_items[1][quantity]" not in checkout_form
    # 14-day card-gated trial.
    assert checkout_form["subscription_data[trial_period_days]"] == "14"
    assert checkout_form["client_reference_id"] == "ws_http"
    assert checkout_form["subscription_data[metadata][workspace_id]"] == "ws_http"


def test_billing_portal_returns_url_for_existing_customer(monkeypatch: pytest.MonkeyPatch) -> None:
    customer_row = {
        "id": "sc_1",
        "workspace_id": "ws_http",
        "stripe_customer_id": "cus_existing",
        "stripe_subscription_id": "sub_1",
        "subscription_status": "active",
        "status": "active",
    }
    client, posted = _billing_client_with_customer(monkeypatch, customer_row=customer_row)

    response = client.post(
        "/billing/portal",
        headers=_account_headers(_account_session_token("ws_http")),
    )

    assert response.status_code == 200
    assert response.json()["url"] == "https://billing.stripe.test/bps_1"
    assert posted == [
        (
            "/v1/billing_portal/sessions",
            {"customer": "cus_existing", "return_url": "https://app.example.test/billing"},
        )
    ]


def test_account_bootstrap_creates_signed_session_and_persists_hash_only() -> None:
    connection = RecordingConnection()
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store)
    client = TestClient(
        build_app(
            adapter=adapter,
            billing_connection=connection,
            expected_api_token=TEST_API_TOKEN,
            account_session_secret="account-session-secret",
        )
    )

    response = client.post(
        "/account/bootstrap",
        json={"email": " ALICE@YUTOME.COM ", "name": "Alice", "workspace_name": "Alice Research"},
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["principal"]["normalized_email"] == "alice@yutome.com"
    assert body["principal"]["workspace_id"].startswith("ws_")
    assert body["session"]["audience"] == DEFAULT_ACCOUNT_SESSION_AUDIENCE
    assert body["session"]["cookie_name"] == ACCOUNT_SESSION_COOKIE_NAME
    assert body["session"]["max_age_seconds"] == ACCOUNT_SESSION_TTL_SECONDS

    token = body["session"]["token"]
    version, encoded_payload, encoded_signature = token.split(".")
    assert version == "v1"
    payload = decode_base64url_json(encoded_payload)
    assert payload["aud"] == DEFAULT_ACCOUNT_SESSION_AUDIENCE
    assert isinstance(payload["iat"], int)
    assert isinstance(payload["jti"], str)
    assert payload["exp"] - payload["iat"] == ACCOUNT_SESSION_TTL_SECONDS
    assert payload["user_id"] == body["principal"]["user_id"]
    assert payload["workspace_id"] == body["principal"]["workspace_id"]
    assert payload["workspace_ids"] == [body["principal"]["workspace_id"]]
    expected_signature = hmac.new(
        b"account-session-secret",
        f"v1.{encoded_payload}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    assert base64.urlsafe_b64decode(encoded_signature + ("=" * (-len(encoded_signature) % 4))) == expected_signature

    account_session_calls = [params for sql, params in connection.calls if "INSERT INTO account_sessions" in sql]
    assert len(account_session_calls) == 1
    assert account_session_calls[0]["session_hash"] == session_token_hash(token)
    assert token not in json.dumps(connection.calls, default=str)


def test_account_bootstrap_requires_api_token_before_db_write() -> None:
    connection = RecordingConnection()
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store)
    client = TestClient(
        build_app(
            adapter=adapter,
            billing_connection=connection,
            expected_api_token=TEST_API_TOKEN,
            account_session_secret="account-session-secret",
        )
    )

    response = client.post(
        "/account/bootstrap",
        json={"email": "alice@yutome.com", "name": "Alice", "workspace_name": "Alice Research"},
    )

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "api_token_required"
    assert connection.calls == []


def test_account_bootstrap_requires_connection_and_signing_secret() -> None:
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store)
    missing_connection = TestClient(
        build_app(
            adapter=adapter,
            expected_api_token=TEST_API_TOKEN,
            account_session_secret="account-session-secret",
        )
    )
    missing_secret = TestClient(
        build_app(
            adapter=adapter,
            billing_connection=RecordingConnection(),
            expected_api_token=TEST_API_TOKEN,
        )
    )
    headers = {"Authorization": f"Bearer {TEST_API_TOKEN}"}
    payload = {"email": "alice@yutome.com", "name": "Alice", "workspace_name": "Alice Research"}

    connection_response = missing_connection.post("/account/bootstrap", json=payload, headers=headers)
    secret_response = missing_secret.post("/account/bootstrap", json=payload, headers=headers)

    assert connection_response.status_code == 503
    assert error_body(connection_response.json())["code"] == "account_bootstrap_connection_unconfigured"
    assert secret_response.status_code == 503
    assert error_body(secret_response.json())["code"] == "account_session_signing_unconfigured"
    assert ACCOUNT_SESSION_HMAC_SECRET_ENV_VAR in error_body(secret_response.json())["message"]


def test_account_delete_requires_admin_token() -> None:
    connection = RecordingConnection(
        workspace_row={
            "id": "ws_test",
            "name": "Synthetic",
            "status": "active",
            "subscription_status": "trialing",
            "owner_user_id": "user_test",
        }
    )
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store)
    client = TestClient(
        build_app(adapter=adapter, billing_connection=connection, expected_api_token=TEST_API_TOKEN)
    )

    response = client.delete("/account/ws_test")

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "api_token_required"
    assert connection.calls == []


def test_account_delete_rejects_dashboard_token() -> None:
    connection = RecordingConnection(
        workspace_row={
            "id": "ws_test",
            "name": "Synthetic",
            "status": "active",
            "subscription_status": "trialing",
            "owner_user_id": "user_test",
        }
    )
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store)
    client = TestClient(
        build_app(
            adapter=adapter,
            billing_connection=connection,
            expected_account_api_token=ACCOUNT_DASHBOARD_TOKEN,
        )
    )

    response = client.delete("/account/ws_test", headers={"Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}"})

    assert response.status_code == 503
    assert error_body(response.json())["code"] == "api_token_unconfigured"
    assert connection.calls == []


def test_account_delete_requires_connection() -> None:
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store)
    client = TestClient(build_app(adapter=adapter, expected_api_token=TEST_API_TOKEN))

    response = client.delete("/account/ws_test", headers={"Authorization": f"Bearer {TEST_API_TOKEN}"})

    assert response.status_code == 503
    assert error_body(response.json())["code"] == "account_delete_connection_unconfigured"


def test_account_delete_refuses_active_subscription() -> None:
    connection = RecordingConnection(
        workspace_row={
            "id": "ws_real",
            "name": "Real Workspace",
            "status": "active",
            "subscription_status": "active",
            "owner_user_id": "user_real",
        }
    )
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store)
    client = TestClient(
        build_app(adapter=adapter, billing_connection=connection, expected_api_token=TEST_API_TOKEN)
    )

    response = client.delete("/account/ws_real", headers={"Authorization": f"Bearer {TEST_API_TOKEN}"})

    assert response.status_code == 409
    assert error_body(response.json())["code"] == "workspace_not_synthetic"
    assert not any(sql.startswith("DELETE FROM workspaces") for sql, _params in connection.calls)


def test_account_delete_purges_synthetic_workspace() -> None:
    workspace_id = "ws_0123456789abcdef01234567"
    connection = RecordingConnection(
        workspace_row={
            "id": workspace_id,
            "name": "Synthetic",
            "status": "active",
            "subscription_status": "trialing",
            "owner_user_id": "user_synth",
        }
    )
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store)
    client = TestClient(
        build_app(adapter=adapter, billing_connection=connection, expected_api_token=TEST_API_TOKEN)
    )

    response = client.delete(f"/account/{workspace_id}", headers={"Authorization": f"Bearer {TEST_API_TOKEN}"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["workspace_id"] == workspace_id
    delete_calls = [(sql, params) for sql, params in connection.calls if sql.startswith("DELETE FROM")]
    delete_sql = [sql for sql, _params in delete_calls]
    assert any(sql.startswith("DELETE FROM sources") for sql in delete_sql)
    assert any(sql.startswith("DELETE FROM jobs") for sql in delete_sql)
    assert delete_sql[-1].startswith("DELETE FROM workspaces")
    assert all(params == {"workspace_id": workspace_id} for _sql, params in delete_calls)


def test_tool_call_endpoint_uses_workspace_from_auth_header(
    hosted_http_client: tuple[TestClient, RecordingSearchStore, RecordingLedger],
) -> None:
    client, store, ledger = hosted_http_client

    response = client.post(
        "/tools/call",
        json={"name": "find", "arguments": {"text": "Crohn", "mode": "lexical", "limit": 4}},
        headers=auth_headers("ws_http"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    rows = body["result"]["rows"]
    assert rows[0]["chunk_id"] == "chunk_http"
    assert rows[0]["resource_uri"] == "yutome://chunk/chunk_http"
    assert store.calls == [{"workspace_id": "ws_http", "query": "Crohn", "limit": 4}]
    assert ledger.events[0].workspace_id == "ws_http"
    assert ledger.events[0].operation == "lexical_query"


def test_tool_call_endpoint_accepts_contract_list_and_q_tools(
    hosted_http_client: tuple[TestClient, RecordingSearchStore, RecordingLedger],
) -> None:
    client, store, _ledger = hosted_http_client

    list_response = client.post(
        "/tools/call",
        json={"name": "list", "arguments": {"entity": "videos", "order_by": "newest", "limit": 1}},
        headers=auth_headers("ws_http"),
    )
    q_response = client.post(
        "/tools/call",
        json={"name": "q", "arguments": {"request": {"project": "status_breakdown"}}},
        headers=auth_headers("ws_http"),
    )

    assert list_response.status_code == 200
    assert list_response.json()["result"]["rows"][0]["video_id"] == "vid_http"
    assert q_response.status_code == 200
    assert q_response.json()["result"]["rows"][0]["videos"] == 1
    assert store.calls == [
        {
            "list": "videos",
            "workspace_id": "ws_http",
            "limit": 1,
            "offset": 0,
            "channel": None,
            "video_id": None,
            "order_by": "newest",
        },
        {"list": "status", "workspace_id": "ws_http"},
    ]


def test_configured_api_token_rejects_missing_authorization_before_workspace_dispatch() -> None:
    store = RecordingSearchStore()
    ledger = RecordingLedger()
    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=ledger,
        usage_context_provider=_allow_search_usage_context,
    )
    client = TestClient(build_app(adapter=adapter, expected_api_token="hosted-secret"))

    response = client.post(
        "/tools/call",
        json={"name": "find", "arguments": {"text": "Crohn", "mode": "lexical"}},
    )

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "api_token_required"
    assert store.calls == []
    assert ledger.events == []


def test_unconfigured_api_token_rejects_tool_and_resource_before_adapter_dispatch() -> None:
    store = RecordingSearchStore()
    ledger = RecordingLedger()
    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=ledger,
        usage_context_provider=_allow_search_usage_context,
    )
    client = TestClient(build_app(adapter=adapter))

    tool_response = client.post(
        "/tools/call",
        json={"name": "find", "arguments": {"text": "Crohn", "mode": "lexical"}},
        headers={WORKSPACE_HEADER: "ws_http", "Authorization": "Bearer any-token"},
    )
    resource_response = client.post(
        "/resources/read",
        json={"uri": "yutome://chunk/chunk_http"},
        headers={WORKSPACE_HEADER: "ws_http", "Authorization": "Bearer any-token"},
    )

    assert tool_response.status_code == 503
    assert error_body(tool_response.json())["code"] == "api_token_unconfigured"
    assert resource_response.status_code == 503
    assert error_body(resource_response.json())["code"] == "api_token_unconfigured"
    assert store.calls == []
    assert ledger.events == []


def test_explicit_auth_dependency_can_be_used_for_tests_without_token() -> None:
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store, usage_context_provider=_allow_search_usage_context)

    def test_auth() -> Any:
        from yutome.hosted.mcp_query import HostedMcpAuthContext

        return HostedMcpAuthContext(workspace_id="ws_http").validated()

    client = TestClient(build_app(adapter=adapter, auth_dependency=test_auth))

    response = client.post(
        "/resources/read",
        json={"uri": "yutome://chunk/chunk_http"},
    )

    assert response.status_code == 200
    assert response.json()["result"]["chunk_id"] == "chunk_http"
    assert store.calls == [{"resource": "chunk", "workspace_id": "ws_http", "id": "chunk_http"}]


def test_configured_api_token_rejects_invalid_authorization_before_store_or_ledger() -> None:
    store = RecordingSearchStore()
    ledger = RecordingLedger()
    adapter = HostedMcpQueryAdapter(search_store=store, ledger=ledger)
    client = TestClient(build_app(adapter=adapter, expected_api_token="hosted-secret"))

    response = client.post(
        "/tools/call",
        json={"name": "find", "arguments": {"text": "Crohn", "mode": "lexical"}},
        headers={WORKSPACE_HEADER: "ws_http", "Authorization": "Bearer wrong-secret"},
    )

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "api_token_invalid"
    assert store.calls == []
    assert ledger.events == []


def test_configured_api_token_allows_valid_tool_call() -> None:
    store = RecordingSearchStore()
    ledger = RecordingLedger()
    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=ledger,
        usage_context_provider=_allow_search_usage_context,
    )
    client = TestClient(build_app(adapter=adapter, expected_api_token="hosted-secret"))

    response = client.post(
        "/tools/call",
        json={"name": "find", "arguments": {"text": "Crohn", "mode": "lexical", "limit": 2}},
        headers=auth_headers("ws_http", token="hosted-secret"),
    )

    assert response.status_code == 200
    assert response.json()["result"]["rows"][0]["chunk_id"] == "chunk_http"
    assert store.calls == [{"workspace_id": "ws_http", "query": "Crohn", "limit": 2}]
    assert len(ledger.events) == 1


def test_missing_or_invalid_workspace_header_is_rejected(
    hosted_http_client: tuple[TestClient, RecordingSearchStore, RecordingLedger],
) -> None:
    client, store, _ = hosted_http_client

    missing = client.post(
        "/tools/call",
        json={"name": "find", "arguments": {"text": "Crohn", "mode": "lexical"}},
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
    )
    invalid = client.post(
        "/tools/call",
        json={"name": "find", "arguments": {"text": "Crohn", "mode": "lexical"}},
        headers=auth_headers("not a workspace"),
    )

    assert missing.status_code == 401
    assert error_body(missing.json())["code"] == "workspace_required"
    assert invalid.status_code == 401
    assert error_body(invalid.json())["code"] == "workspace_invalid"
    assert store.calls == []


def test_configured_api_token_protects_resource_read_before_adapter_dispatch() -> None:
    store = RecordingSearchStore()
    ledger = RecordingLedger()
    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=ledger,
        usage_context_provider=_allow_search_usage_context,
    )
    client = TestClient(build_app(adapter=adapter, expected_api_token="hosted-secret"))

    missing = client.post(
        "/resources/read",
        json={"uri": "yutome://chunk/chunk_http"},
        headers={WORKSPACE_HEADER: "ws_http"},
    )
    invalid = client.post(
        "/resources/read",
        json={"uri": "yutome://chunk/chunk_http"},
        headers={WORKSPACE_HEADER: "ws_http", "Authorization": "Bearer wrong-secret"},
    )
    valid = client.post(
        "/resources/read",
        json={"uri": "yutome://chunk/chunk_http"},
        headers={WORKSPACE_HEADER: "ws_http", "Authorization": "Bearer hosted-secret"},
    )

    assert missing.status_code == 401
    assert error_body(missing.json())["code"] == "api_token_required"
    assert invalid.status_code == 401
    assert error_body(invalid.json())["code"] == "api_token_invalid"
    assert valid.status_code == 200
    assert valid.json()["result"]["chunk_id"] == "chunk_http"
    assert store.calls == [{"resource": "chunk", "workspace_id": "ws_http", "id": "chunk_http"}]
    assert len(ledger.events) == 1
    assert ledger.events[0].operation == "resource_read"


def test_resource_read_endpoint_returns_payload(
    hosted_http_client: tuple[TestClient, RecordingSearchStore, RecordingLedger],
) -> None:
    client, store, _ = hosted_http_client

    response = client.post(
        "/resources/read",
        json={"uri": "yutome://chunk/chunk_http"},
        headers=auth_headers("ws_http"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["result"]["resource_uri"] == "yutome://chunk/chunk_http"
    assert body["result"]["text"] == "Hosted Crohn query result"
    assert store.calls == [{"resource": "chunk", "workspace_id": "ws_http", "id": "chunk_http"}]


def test_resource_read_endpoint_hides_cross_workspace_resources_as_missing(
    hosted_http_client: tuple[TestClient, RecordingSearchStore, RecordingLedger],
) -> None:
    client, store, _ = hosted_http_client

    response = client.post(
        "/resources/read",
        json={"uri": "yutome://chunk/chunk_http"},
        headers=auth_headers("ws_bob"),
    )

    assert response.status_code == 404
    detail = error_body(response.json())
    assert detail["code"] == "resource_not_found"
    assert detail["data"]["kind"] == "chunk"
    assert store.calls == [{"resource": "chunk", "workspace_id": "ws_bob", "id": "chunk_http"}]


# --- Dashboard (session-authenticated) retrieval ---------------------------

ACCOUNT_DASHBOARD_TOKEN = "dashboard-test-token"
ACCOUNT_SESSION_SECRET = "account-session-secret"


def _account_session_token(
    workspace_id: str = "ws_http",
    user_id: str = "user_http",
    *,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    return sign_account_session_token(
        user_id=user_id,
        workspace_id=workspace_id,
        secret=ACCOUNT_SESSION_SECRET,
        expires_at=expires_at or now + timedelta(seconds=ACCOUNT_SESSION_TTL_SECONDS),
        issued_at=issued_at or now,
        audience=DEFAULT_ACCOUNT_SESSION_AUDIENCE,
    )


def _account_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}",
        ACCOUNT_SESSION_TOKEN_HEADER: token,
    }


def _account_search_client() -> tuple[TestClient, RecordingSearchStore]:
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=RecordingLedger(),
        usage_context_provider=_allow_search_usage_context,
    )
    connection = RecordingConnection(rows=[{"id": "ws_http", "name": "Workspace", "status": "active"}])
    app = build_app(
        adapter=adapter,
        billing_connection=connection,
        expected_account_api_token=ACCOUNT_DASHBOARD_TOKEN,
        account_session_secret=ACCOUNT_SESSION_SECRET,
    )
    return TestClient(app), store


def _account_client_with_connection(connection: Any) -> tuple[TestClient, RecordingSearchStore]:
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=RecordingLedger(),
        usage_context_provider=_allow_search_usage_context,
    )
    app = build_app(
        adapter=adapter,
        billing_connection=connection,
        expected_account_api_token=ACCOUNT_DASHBOARD_TOKEN,
        account_session_secret=ACCOUNT_SESSION_SECRET,
    )
    return TestClient(app), store


def _jsonb_obj(value: object) -> dict[str, Any]:
    obj = getattr(value, "obj", value)
    assert isinstance(obj, Mapping)
    return dict(obj)


def _contains_raw_key(value: object, raw_key: str) -> bool:
    if isinstance(value, str):
        return raw_key in value
    obj = getattr(value, "obj", value)
    if isinstance(obj, Mapping):
        return any(_contains_raw_key(item, raw_key) for item in obj.values())
    if isinstance(obj, (list, tuple, set)):
        return any(_contains_raw_key(item, raw_key) for item in obj)
    return False


class StatefulApiKeyConnection:
    def __init__(self) -> None:
        self.workspace = {"id": "ws_http", "name": "Workspace", "status": "active"}
        self.api_keys: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params = dict(params or {})
        self.calls.append((statement, params))
        if "FROM account_sessions" in statement:
            return []
        if "UPDATE account_sessions" in statement:
            return []
        if "FROM workspaces" in statement:
            return [dict(self.workspace)] if params.get("workspace_id") == self.workspace["id"] else []
        if statement.startswith("INSERT INTO api_keys"):
            row = {
                "id": params["id"],
                "workspace_id": params["workspace_id"],
                "user_id": params["user_id"],
                "key_hash": params["key_hash"],
                "name": params["name"],
                "scopes": params["scopes"],
                "status": "active",
                "metadata_json": _jsonb_obj(params["metadata_json"]),
                "created_at": datetime.now(timezone.utc),
                "last_used_at": None,
                "expires_at": params["expires_at"],
                "revoked_at": None,
            }
            self.api_keys[row["id"]] = row
            return [dict(row)]
        if "FROM api_keys" in statement and "key_hash" in params:
            rows = [
                row
                for row in self.api_keys.values()
                if row["key_hash"] == params["key_hash"] and row["status"] == "active" and row["revoked_at"] is None
            ]
            return [dict(rows[0])] if rows else []
        if "FROM api_keys" in statement and "workspace_id" in params:
            rows = [row for row in self.api_keys.values() if row["workspace_id"] == params["workspace_id"]]
            return [dict(row) for row in sorted(rows, key=lambda row: row["created_at"], reverse=True)]
        if statement.startswith("UPDATE api_keys") and "revoked_at" in statement:
            row = self.api_keys.get(params["key_id"])
            if row is None or row["workspace_id"] != params["workspace_id"] or row["revoked_at"] is not None:
                return []
            row["status"] = "revoked"
            row["revoked_at"] = params["now"]
            return [{"id": row["id"]}]
        if statement.startswith("UPDATE api_keys") and "last_used_at" in statement:
            row = self.api_keys.get(params["key_id"])
            if row is not None:
                row["last_used_at"] = params["now"]
            return []
        return []


def _api_key_test_client() -> tuple[TestClient, RecordingSearchStore, StatefulApiKeyConnection]:
    connection = StatefulApiKeyConnection()
    client, store = _account_client_with_connection(connection)
    return client, store, connection


def _create_api_key(client: TestClient, *, name: str = "Dev key") -> dict[str, Any]:
    response = client.post(
        "/account/api-keys",
        json={"name": name},
        headers=_account_headers(_account_session_token("ws_http")),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_account_search_uses_workspace_from_session_not_arguments() -> None:
    client, store = _account_search_client()

    response = client.post(
        "/account/search",
        json={"text": "Crohn", "mode": "lexical", "limit": 4},
        headers=_account_headers(_account_session_token("ws_http")),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["result"]["rows"][0]["chunk_id"] == "chunk_http"
    assert store.calls == [{"workspace_id": "ws_http", "query": "Crohn", "limit": 4}]


def test_account_search_scopes_to_session_workspace() -> None:
    client, store = _account_search_client()

    response = client.post(
        "/account/search",
        json={"text": "Crohn", "mode": "lexical", "limit": 2},
        headers=_account_headers(_account_session_token("ws_bob")),
    )

    assert response.status_code == 200
    assert store.calls == [{"workspace_id": "ws_bob", "query": "Crohn", "limit": 2}]


def test_account_search_rejects_workspace_argument_injection() -> None:
    client, store = _account_search_client()

    response = client.post(
        "/account/search",
        json={"text": "Crohn", "mode": "lexical", "workspace_id": "ws_admin"},
        headers=_account_headers(_account_session_token("ws_http")),
    )

    assert response.status_code == 422
    assert store.calls == []


def test_account_search_requires_session_token() -> None:
    client, store = _account_search_client()

    response = client.post(
        "/account/search",
        json={"text": "Crohn", "mode": "lexical"},
        headers={"Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}"},
    )

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "account_session_required"
    assert store.calls == []


def test_account_search_requires_dashboard_token_before_search_store() -> None:
    client, store = _account_search_client()
    session = _account_session_token("ws_http")

    missing = client.post(
        "/account/search",
        json={"text": "Crohn", "mode": "lexical"},
        headers={ACCOUNT_SESSION_TOKEN_HEADER: session},
    )
    invalid = client.post(
        "/account/search",
        json={"text": "Crohn", "mode": "lexical"},
        headers={
            "Authorization": "Bearer wrong-dashboard-token",
            ACCOUNT_SESSION_TOKEN_HEADER: session,
        },
    )

    assert missing.status_code == 401
    assert error_body(missing.json())["code"] == "api_token_required"
    assert invalid.status_code == 401
    assert error_body(invalid.json())["code"] == "api_token_invalid"
    assert store.calls == []


def test_account_search_rejects_invalid_session_before_search_store() -> None:
    client, store = _account_search_client()
    issued_at = datetime.now(timezone.utc) - timedelta(seconds=ACCOUNT_SESSION_TTL_SECONDS + 120)
    expired_session = _account_session_token(
        "ws_http",
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=1),
    )

    malformed = client.post(
        "/account/search",
        json={"text": "Crohn", "mode": "lexical"},
        headers=_account_headers("not-a-session-token"),
    )
    expired = client.post(
        "/account/search",
        json={"text": "Crohn", "mode": "lexical"},
        headers=_account_headers(expired_session),
    )

    assert malformed.status_code == 401
    assert error_body(malformed.json())["code"] == "account_session_malformed"
    assert expired.status_code == 401
    assert error_body(expired.json())["code"] == "account_session_expired"
    assert store.calls == []


def test_account_session_revoke_marks_session_revoked() -> None:
    raw_token = _account_session_token("ws_http")
    expected_hash = session_token_hash(raw_token)
    connection = RecordingConnection(
        rows=[{"id": "ws_http", "name": "Workspace", "status": "active"}],
        account_session_update_rows=[{"id": "sess_x"}],
    )
    client, _store = _account_client_with_connection(connection)

    response = client.post("/account/session/revoke", headers=_account_headers(raw_token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["revoked"] is True
    assert body["session_id"] == "sess_x"
    update_calls = [(sql, params) for sql, params in connection.calls if "UPDATE account_sessions" in sql]
    assert len(update_calls) == 1
    update_sql, update_params = update_calls[0]
    assert "status" in update_sql
    assert "revoked_at" in update_sql
    assert update_params["status"] == "revoked"
    assert update_params["revoked_at"] is not None
    assert expected_hash in update_params.values()


def test_account_session_revoke_requires_session_token() -> None:
    connection = RecordingConnection(rows=[{"id": "ws_http", "name": "Workspace", "status": "active"}])
    client, _store = _account_client_with_connection(connection)

    response = client.post(
        "/account/session/revoke",
        headers={"Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}"},
    )

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "account_session_required"


def test_account_auth_dependency_denies_revoked_session() -> None:
    connection = RecordingConnection(
        rows=[{"id": "ws_http", "name": "Workspace", "status": "active"}],
        account_session_rows=[
            {"id": "sess_x", "status": "revoked", "revoked_at": "2026-01-01T00:00:00+00:00"}
        ],
    )
    client, store = _account_client_with_connection(connection)

    response = client.post(
        "/account/search",
        json={"text": "Crohn", "mode": "lexical"},
        headers=_account_headers(_account_session_token("ws_http")),
    )

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "account_session_revoked"
    assert store.calls == []


def test_account_auth_dependency_allows_session_with_no_persisted_row() -> None:
    connection = RecordingConnection(
        rows=[{"id": "ws_http", "name": "Workspace", "status": "active"}],
        account_session_rows=[],
    )
    client, store = _account_client_with_connection(connection)

    response = client.post(
        "/account/search",
        json={"text": "Crohn", "mode": "lexical", "limit": 4},
        headers=_account_headers(_account_session_token("ws_http")),
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert store.calls == [{"workspace_id": "ws_http", "query": "Crohn", "limit": 4}]


def test_create_api_key_returns_raw_once_and_persists_hash_only() -> None:
    client, _store, connection = _api_key_test_client()

    body = _create_api_key(client, name="Local dev")

    raw_key = body["key"]
    assert raw_key.startswith(API_KEY_PREFIX)
    stored = connection.api_keys[body["id"]]
    assert stored["key_hash"] == api_key_hash(raw_key)
    assert stored["name"] == "Local dev"
    assert raw_key not in json.dumps(stored, default=str)
    assert all(
        not _contains_raw_key(value, raw_key)
        for _sql, params in connection.calls
        for value in params.values()
    )


def test_list_api_keys_never_leaks_hash_or_raw() -> None:
    client, _store, _connection = _api_key_test_client()
    first = _create_api_key(client, name="First key")
    second = _create_api_key(client, name="Second key")

    response = client.get("/account/api-keys", headers=_account_headers(_account_session_token("ws_http")))

    assert response.status_code == 200, response.text
    body = response.json()
    payload = json.dumps(body)
    assert body["ok"] is True
    assert {item["name"] for item in body["api_keys"]} == {"First key", "Second key"}
    assert all({"id", "name", "scopes", "status", "created_at"} <= set(item) for item in body["api_keys"])
    assert "key_hash" not in payload
    assert first["key"] not in payload
    assert second["key"] not in payload


def test_revoke_api_key_is_idempotent_and_tenant_scoped() -> None:
    client, _store, connection = _api_key_test_client()
    created = _create_api_key(client)

    revoked = client.delete(
        f"/account/api-keys/{created['id']}",
        headers=_account_headers(_account_session_token("ws_http")),
    )
    repeated = client.delete(
        f"/account/api-keys/{created['id']}",
        headers=_account_headers(_account_session_token("ws_http")),
    )

    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked"] is True
    assert repeated.status_code == 404
    assert error_body(repeated.json())["code"] == "api_key_not_found"
    stored = connection.api_keys[created["id"]]
    assert stored["status"] == "revoked"
    assert stored["revoked_at"] is not None


def test_api_key_bearer_can_call_find() -> None:
    client, store, connection = _api_key_test_client()
    created = _create_api_key(client)

    response = client.post(
        "/account/keys/find",
        json={"text": "Crohn", "mode": "lexical", "limit": 4},
        headers={"Authorization": f"Bearer {created['key']}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert store.calls == [{"workspace_id": "ws_http", "query": "Crohn", "limit": 4}]
    assert connection.api_keys[created["id"]]["last_used_at"] is not None


def test_api_key_bearer_rejects_unknown_revoked_and_expired() -> None:
    client, _store, connection = _api_key_test_client()

    unknown = client.post(
        "/account/keys/find",
        json={"text": "Crohn", "mode": "lexical"},
        headers={"Authorization": "Bearer yk_unknown"},
    )
    revoked_key = _create_api_key(client, name="Revoked key")
    connection.api_keys[revoked_key["id"]]["status"] = "revoked"
    connection.api_keys[revoked_key["id"]]["revoked_at"] = datetime.now(timezone.utc)
    revoked = client.post(
        "/account/keys/find",
        json={"text": "Crohn", "mode": "lexical"},
        headers={"Authorization": f"Bearer {revoked_key['key']}"},
    )
    expired_key = _create_api_key(client, name="Expired key")
    connection.api_keys[expired_key["id"]]["expires_at"] = datetime.now(timezone.utc) - timedelta(minutes=1)
    expired = client.post(
        "/account/keys/find",
        json={"text": "Crohn", "mode": "lexical"},
        headers={"Authorization": f"Bearer {expired_key['key']}"},
    )

    assert unknown.status_code == 401
    assert error_body(unknown.json())["code"] == "api_key_invalid"
    assert revoked.status_code == 401
    assert error_body(revoked.json())["code"] == "api_key_invalid"
    assert expired.status_code == 401
    assert error_body(expired.json())["code"] == "api_key_expired"


def test_api_key_bearer_cannot_use_infra_or_dashboard_token() -> None:
    client, store, _connection = _api_key_test_client()

    infra = client.post(
        "/account/keys/find",
        json={"text": "Crohn", "mode": "lexical"},
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
    )
    dashboard = client.post(
        "/account/keys/find",
        json={"text": "Crohn", "mode": "lexical"},
        headers={"Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}"},
    )

    assert infra.status_code == 401
    assert error_body(infra.json())["code"] == "api_key_invalid"
    assert dashboard.status_code == 401
    assert error_body(dashboard.json())["code"] == "api_key_invalid"
    assert store.calls == []


def test_create_api_key_rejects_out_of_allowlist_scope() -> None:
    client, _store, connection = _api_key_test_client()

    response = client.post(
        "/account/api-keys",
        json={"scopes": [contract.SOURCE_WRITE_SCOPE]},
        headers=_account_headers(_account_session_token("ws_http")),
    )

    assert response.status_code == 400
    assert error_body(response.json())["code"] == "api_key_scope_invalid"
    assert connection.api_keys == {}


class RecordingEmailSender:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    def send(self, message: Any) -> None:
        self.messages.append(message)


class _LoginVerifyConnection:
    """Returns the token row only for the consume UPDATE; empty otherwise so the
    bootstrap-in-transaction falls back to deterministic ids (as in the empty
    bootstrap test)."""

    def __init__(self, token_rows: list[dict[str, Any]]) -> None:
        self.token_rows = token_rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((statement, dict(params or {})))
        if "email_login_tokens" in statement and statement.strip().upper().startswith("UPDATE"):
            return list(self.token_rows)
        return []


def _login_app(
    connection: Any,
    sender: RecordingEmailSender,
    *,
    dev_link: bool = True,
    google_signin_settings: GoogleSignInSettings | None = None,
) -> TestClient:
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store, usage_context_provider=_allow_search_usage_context)
    app = build_app(
        adapter=adapter,
        billing_connection=connection,
        expected_account_api_token=ACCOUNT_DASHBOARD_TOKEN,
        account_session_secret=ACCOUNT_SESSION_SECRET,
        email_sender=sender,
        app_base_url="https://app.example.test",
        dev_return_login_link=dev_link,
        google_signin_settings=google_signin_settings,
    )
    return TestClient(app)


def test_account_login_start_issues_token_and_emails_link_without_session() -> None:
    connection = RecordingConnection()
    sender = RecordingEmailSender()
    client = _login_app(connection, sender)

    response = client.post(
        "/account/login/start",
        json={"email": " Alice@Example.com ", "name": "Alice", "workspace_name": "Alice WS"},
        headers={"Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["email"] == "alice@example.com"
    assert "session" not in body  # no session minted before email is proven
    verify_link = body["verify_link"]
    assert verify_link.startswith("https://app.example.test/auth/verify?token=")
    assert len(sender.messages) == 1
    assert sender.messages[0].to == "alice@example.com"
    assert verify_link in sender.messages[0].text

    inserts = [params for sql, params in connection.calls if "INSERT INTO email_login_tokens" in sql]
    assert len(inserts) == 1
    raw_token = verify_link.split("token=", 1)[1]
    assert inserts[0]["token_hash"].startswith("sha256:")
    assert raw_token not in json.dumps(connection.calls, default=str)  # only the hash is stored


def test_account_login_start_does_not_return_link_without_dev_flag() -> None:
    connection = RecordingConnection()
    sender = RecordingEmailSender()
    client = _login_app(connection, sender, dev_link=False)

    response = client.post(
        "/account/login/start",
        json={"email": "alice@example.com"},
        headers={"Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "verify_link" not in body
    assert len(sender.messages) == 1
    assert "https://app.example.test/auth/verify?token=" in sender.messages[0].text


@pytest.mark.parametrize(
    "redirect_path",
    ["https://evil.example/", "//evil.example/", "/\\evil.example/", "/%2fevil.example/"],
)
def test_account_login_start_drops_unsafe_redirect_paths(redirect_path: str) -> None:
    connection = RecordingConnection()
    client = _login_app(connection, RecordingEmailSender())

    response = client.post(
        "/account/login/start",
        json={"email": "alice@example.com", "redirect_path": redirect_path},
        headers={"Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}"},
    )

    assert response.status_code == 200
    inserts = [params for sql, params in connection.calls if "INSERT INTO email_login_tokens" in sql]
    assert inserts[0]["redirect_path"] is None


def test_account_login_start_keeps_safe_redirect_path() -> None:
    connection = RecordingConnection()
    client = _login_app(connection, RecordingEmailSender())

    response = client.post(
        "/account/login/start",
        json={"email": "alice@example.com", "redirect_path": "/dashboard/search?q=crohn#top"},
        headers={"Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}"},
    )

    assert response.status_code == 200
    inserts = [params for sql, params in connection.calls if "INSERT INTO email_login_tokens" in sql]
    assert inserts[0]["redirect_path"] == "/dashboard/search?q=crohn#top"


def test_account_login_start_requires_bearer_token() -> None:
    connection = RecordingConnection()
    sender = RecordingEmailSender()
    client = _login_app(connection, sender)

    response = client.post("/account/login/start", json={"email": "alice@example.com"})

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "api_token_required"
    assert sender.messages == []
    assert connection.calls == []


def test_account_google_authorize_requests_identity_scopes_only() -> None:
    connection = RecordingConnection()
    client = _login_app(
        connection,
        RecordingEmailSender(),
        google_signin_settings=GoogleSignInSettings(
            client=OAuthClient(client_id="google-client", client_secret="google-secret")
        ),
    )

    response = client.post(
        "/account/google/authorize",
        json={"redirect_uri": "https://app.example.test/auth/google/callback", "redirect_path": "/dashboard"},
        headers={"Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    params = parse_qs(urlsplit(body["authorization_url"]).query)
    assert params["client_id"] == ["google-client"]
    assert params["scope"] == ["openid email profile"]
    assert "youtube" not in params["scope"][0]
    assert "include_granted_scopes" not in params
    assert "access_type" not in params
    assert connection.calls == []


def test_account_google_callback_mints_session_without_youtube_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = RecordingConnection()
    client = _login_app(
        connection,
        RecordingEmailSender(),
        google_signin_settings=GoogleSignInSettings(
            client=OAuthClient(client_id="google-client", client_secret="google-secret")
        ),
    )

    monkeypatch.setattr(
        "yutome.hosted.google_signin_service.exchange_code",
        lambda **_: {"access_token": "google-access-token", "expires_at": time.time() + 3600},
    )
    monkeypatch.setattr(
        "yutome.hosted.google_signin_service.fetch_google_userinfo",
        lambda access_token: {
            "sub": "google-subject",
            "email": "Alice@Example.com",
            "email_verified": True,
            "name": "Alice",
        },
    )

    redirect_uri = "https://app.example.test/auth/google/callback"
    authorize = client.post(
        "/account/google/authorize",
        json={"redirect_uri": redirect_uri, "redirect_path": "/dashboard/search?q=crohn"},
        headers={"Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}"},
    )
    state = parse_qs(urlsplit(authorize.json()["authorization_url"]).query)["state"][0]

    response = client.post(
        "/account/google/callback",
        json={"code": "google-code", "state": state, "redirect_uri": redirect_uri},
        headers={"Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["session"]["token"]
    assert body["principal"]["normalized_email"] == "alice@example.com"
    assert body["redirect_path"] == "/dashboard/search?q=crohn"
    assert any("INSERT INTO account_sessions" in sql for sql, _ in connection.calls)
    assert not any("youtube_grants" in sql for sql, _ in connection.calls)


def test_account_google_callback_rejects_invalid_state() -> None:
    client = _login_app(
        RecordingConnection(),
        RecordingEmailSender(),
        google_signin_settings=GoogleSignInSettings(
            client=OAuthClient(client_id="google-client", client_secret="google-secret")
        ),
    )

    response = client.post(
        "/account/google/callback",
        json={
            "code": "google-code",
            "state": "not-a-state",
            "redirect_uri": "https://app.example.test/auth/google/callback",
        },
        headers={"Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}"},
    )

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "google_signin_state_invalid"


def test_account_login_verify_consumes_token_and_mints_session() -> None:
    connection = _LoginVerifyConnection(
        [{"normalized_email": "alice@example.com", "name": "Alice", "workspace_name": "Alice WS", "redirect_path": "/dashboard"}]
    )
    client = _login_app(connection, RecordingEmailSender())

    response = client.post(
        "/account/login/verify",
        json={"token": "raw-login-token"},
        headers={"Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["session"]["token"]
    assert body["principal"]["workspace_id"].startswith("ws_")
    assert body["redirect_path"] == "/dashboard"
    consume_calls = [params for sql, params in connection.calls if "UPDATE email_login_tokens" in sql]
    assert len(consume_calls) == 1
    assert "now" in consume_calls[0]


def test_account_login_verify_rechecks_stored_redirect_path() -> None:
    connection = _LoginVerifyConnection(
        [{"normalized_email": "alice@example.com", "name": "Alice", "workspace_name": "Alice WS", "redirect_path": "/%5cevil"}]
    )
    client = _login_app(connection, RecordingEmailSender())

    response = client.post(
        "/account/login/verify",
        json={"token": "raw-login-token"},
        headers={"Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}"},
    )

    assert response.status_code == 200
    assert response.json()["redirect_path"] is None


def test_account_login_verify_rejects_unknown_or_expired_token() -> None:
    connection = _LoginVerifyConnection([])  # consume matches no row (unknown/used/expired)
    client = _login_app(connection, RecordingEmailSender())

    response = client.post(
        "/account/login/verify",
        json={"token": "stale"},
        headers={"Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}"},
    )

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "account_login_token_invalid"
    assert not any("INSERT INTO account_sessions" in sql for sql, _ in connection.calls)


def test_account_login_verify_requires_bearer_token() -> None:
    connection = _LoginVerifyConnection([{"normalized_email": "alice@example.com"}])
    client = _login_app(connection, RecordingEmailSender())

    response = client.post("/account/login/verify", json={"token": "raw-login-token"})

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "api_token_required"
    assert connection.calls == []


def test_account_show_reads_resource_for_session_workspace() -> None:
    client, store = _account_search_client()

    response = client.post(
        "/account/show",
        json={"kind": "chunk", "id": "chunk_http"},
        headers=_account_headers(_account_session_token("ws_http")),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["result"]["resource_uri"] == "yutome://chunk/chunk_http"
    assert store.calls == [{"resource": "chunk", "workspace_id": "ws_http", "id": "chunk_http"}]


def test_account_list_videos_scoped_to_session_workspace() -> None:
    client, store = _account_search_client()

    response = client.post(
        "/account/list",
        json={"entity": "videos", "order_by": "newest", "limit": 5},
        headers=_account_headers(_account_session_token("ws_http")),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["result"]["rows"][0]["video_id"] == "vid_http"
    assert store.calls == [
        {
            "list": "videos",
            "workspace_id": "ws_http",
            "limit": 5,
            "offset": 0,
            "channel": None,
            "video_id": None,
            "order_by": "newest",
        }
    ]


def test_account_list_channels_and_status_scoped_to_session_workspace() -> None:
    client, store = _account_search_client()
    headers = _account_headers(_account_session_token("ws_bob"))

    channels = client.post("/account/list", json={"entity": "channels"}, headers=headers)
    status = client.post("/account/list", json={"entity": "status"}, headers=headers)

    assert channels.status_code == 200
    assert channels.json()["result"]["rows"][0]["channel_id"] == "chan_http"
    assert status.status_code == 200
    assert status.json()["result"]["rows"][0]["videos"] == 1
    assert {call.get("list") for call in store.calls} == {"channels", "status"}
    assert all(call["workspace_id"] == "ws_bob" for call in store.calls)


def test_account_list_requires_session_token() -> None:
    client, store = _account_search_client()

    response = client.post(
        "/account/list",
        json={"entity": "videos"},
        headers={"Authorization": f"Bearer {ACCOUNT_DASHBOARD_TOKEN}"},
    )

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "account_session_required"
    assert store.calls == []


def test_account_list_rejects_workspace_argument_injection() -> None:
    client, store = _account_search_client()

    response = client.post(
        "/account/list",
        json={"entity": "videos", "workspace_id": "ws_admin"},
        headers=_account_headers(_account_session_token("ws_http")),
    )

    assert response.status_code == 422
    assert store.calls == []


def test_account_list_surfaces_adapter_rejected_combinations() -> None:
    client, store = _account_search_client()
    headers = _account_headers(_account_session_token("ws_http"))

    videos_selected = client.post("/account/list", json={"entity": "videos", "selected": True}, headers=headers)
    channels_order = client.post("/account/list", json={"entity": "channels", "order_by": "newest"}, headers=headers)
    status_filtered = client.post("/account/list", json={"entity": "status", "channel": "chan_http"}, headers=headers)

    assert videos_selected.status_code == 400
    assert error_body(videos_selected.json())["code"] == "unsupported_list_filter"
    assert channels_order.status_code == 400
    assert error_body(channels_order.json())["code"] == "unsupported_list_order"
    assert status_filtered.status_code == 400
    assert error_body(status_filtered.json())["code"] == "unsupported_list_filter"
    assert store.calls == []  # rejected during argument validation, before any store read
