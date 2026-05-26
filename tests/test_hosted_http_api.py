from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from yutome.hosted.allocation_policy import default_search_store_allocation
from yutome.hosted.http_api import TOKEN_ENV_VAR, WORKSPACE_HEADER, build_app, build_postgres_app, error_body
from yutome.hosted.mcp_query import HostedMcpAuthContext, HostedMcpQueryAdapter, HostedMcpUsageContext
from yutome.hosted.models import EntitlementPolicy, UsageEvent, WorkspaceBalance
from yutome.hosted.search_store import SearchStoreUsage


TEST_API_TOKEN = "hosted-test-token"


def auth_headers(workspace_id: str, *, token: str = TEST_API_TOKEN) -> dict[str, str]:
    return {WORKSPACE_HEADER: workspace_id, "Authorization": f"Bearer {token}"}


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
    assert "websearch_to_tsquery" in connection.calls[0][0]
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
    adapter = HostedMcpQueryAdapter(search_store=store)

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
    adapter = HostedMcpQueryAdapter(search_store=store, ledger=ledger)
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
    assert ledger.events == []


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
