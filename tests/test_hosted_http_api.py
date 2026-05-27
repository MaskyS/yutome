from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from datetime import datetime, timedelta, timezone

from yutome.hosted.allocation_policy import default_search_store_allocation
from yutome.hosted.account import (
    DEFAULT_ACCOUNT_SESSION_AUDIENCE,
    session_token_hash,
    sign_account_session_token,
)
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
from yutome.hosted.mcp_query import HostedMcpAuthContext, HostedMcpQueryAdapter, HostedMcpUsageContext
from yutome.hosted.models import EntitlementPolicy, UsageEvent, WorkspaceBalance
from yutome.hosted.search_store import SearchStoreUsage


TEST_API_TOKEN = "hosted-test-token"


def auth_headers(workspace_id: str, *, token: str = TEST_API_TOKEN) -> dict[str, str]:
    return {WORKSPACE_HEADER: workspace_id, "Authorization": f"Bearer {token}"}


def polar_headers(raw_body: bytes, *, secret: str = "polar-secret", webhook_id: str = "wh_msg_123") -> dict[str, str]:
    timestamp = str(int(time.time()))
    signed = webhook_id.encode() + b"." + timestamp.encode() + b"." + raw_body
    signature = base64.b64encode(hmac.new(secret.encode(), signed, hashlib.sha256).digest()).decode("ascii")
    return {
        "webhook-id": webhook_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": f"v1,{signature}",
    }


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
            backend="postgres_fts_fallback",
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
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((statement, dict(params or {})))
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


def test_polar_webhook_rejects_invalid_signature_before_db_write() -> None:
    connection = RecordingConnection()
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store)
    client = TestClient(
        build_app(
            adapter=adapter,
            billing_connection=connection,
            polar_webhook_secret="polar-secret",
        )
    )
    raw_body = b'{"type":"customer.state_changed","timestamp":"2026-05-26T03:00:00Z","data":{"id":"cus_123"}}'
    headers = polar_headers(raw_body)
    headers["webhook-signature"] = "v1,wrong"

    response = client.post("/billing/polar/webhook", content=raw_body, headers=headers)

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "webhook_signature_invalid"
    assert connection.calls == []


def test_polar_webhook_accepts_signature_and_processes_order_credit() -> None:
    connection = RecordingConnection()
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store)
    client = TestClient(
        build_app(
            adapter=adapter,
            billing_connection=connection,
            polar_webhook_secret="polar-secret",
        )
    )
    raw_body = json.dumps(
        {
            "type": "order.paid",
            "timestamp": "2026-05-26T03:00:00Z",
            "data": {
                "id": "ord_123",
                "customer_id": "cus_123",
                "customer": {"id": "cus_123", "external_id": "ws_http"},
                "metadata": {"yutome_credit_grants": [{"unit": "credits", "quantity": "5"}]},
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    response = client.post("/billing/polar/webhook", content=raw_body, headers=polar_headers(raw_body))

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["event_id"] == "wh_msg_123"
    assert body["credit_entries"] == 1
    assert body["billing_customer"].startswith("bc_")
    assert [call[0].split()[2] for call in connection.calls if call[0].startswith("INSERT INTO")] == [
        "polar_webhook_snapshots",
        "billing_customers",
        "credit_ledger_entries",
        "polar_webhook_snapshots",
    ]
    credit_params = connection.calls[2][1]
    assert credit_params["workspace_id"] == "ws_http"
    assert credit_params["external_order_id"] == "ord_123"
    assert credit_params["quantity_text"] == "5"


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
