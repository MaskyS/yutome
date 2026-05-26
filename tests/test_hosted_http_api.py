from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from yutome.hosted.http_api import TOKEN_ENV_VAR, WORKSPACE_HEADER, build_app, build_postgres_app, error_body
from yutome.hosted.mcp_query import HostedMcpQueryAdapter
from yutome.hosted.models import UsageEvent
from yutome.hosted.search_store import SearchStoreUsage


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
    adapter = HostedMcpQueryAdapter(search_store=store, ledger=ledger)
    return TestClient(build_app(adapter=adapter)), store, ledger


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
    app = build_postgres_app(connection=connection)
    client = TestClient(app)

    response = client.post(
        "/mcp/tools/call",
        json={"name": "find", "arguments": {"text": "Postgres", "mode": "lexical", "limit": 2}},
        headers={WORKSPACE_HEADER: "ws_pg"},
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


def test_tool_call_endpoint_uses_workspace_from_auth_header(
    hosted_http_client: tuple[TestClient, RecordingSearchStore, RecordingLedger],
) -> None:
    client, store, ledger = hosted_http_client

    response = client.post(
        "/tools/call",
        json={"name": "find", "arguments": {"text": "Crohn", "mode": "lexical", "limit": 4}},
        headers={WORKSPACE_HEADER: "ws_http"},
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


def test_configured_api_token_rejects_missing_authorization_before_workspace_dispatch() -> None:
    store = RecordingSearchStore()
    ledger = RecordingLedger()
    adapter = HostedMcpQueryAdapter(search_store=store, ledger=ledger)
    client = TestClient(build_app(adapter=adapter, expected_api_token="hosted-secret"))

    response = client.post(
        "/tools/call",
        json={"name": "find", "arguments": {"text": "Crohn", "mode": "lexical"}},
    )

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "api_token_required"
    assert store.calls == []
    assert ledger.events == []


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
    adapter = HostedMcpQueryAdapter(search_store=store, ledger=ledger)
    client = TestClient(build_app(adapter=adapter, expected_api_token="hosted-secret"))

    response = client.post(
        "/tools/call",
        json={"name": "find", "arguments": {"text": "Crohn", "mode": "lexical", "limit": 2}},
        headers={WORKSPACE_HEADER: "ws_http", "Authorization": "Bearer hosted-secret"},
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
    )
    invalid = client.post(
        "/tools/call",
        json={"name": "find", "arguments": {"text": "Crohn", "mode": "lexical"}},
        headers={WORKSPACE_HEADER: "not a workspace"},
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
        headers={WORKSPACE_HEADER: "ws_http"},
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
        headers={WORKSPACE_HEADER: "ws_bob"},
    )

    assert response.status_code == 404
    detail = error_body(response.json())
    assert detail["code"] == "resource_not_found"
    assert detail["data"]["kind"] == "chunk"
    assert store.calls == [{"resource": "chunk", "workspace_id": "ws_bob", "id": "chunk_http"}]
