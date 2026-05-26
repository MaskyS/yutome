from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from yutome.hosted.allocation_policy import default_search_store_allocation
from yutome.hosted.mcp_query import (
    HostedMcpAuthContext,
    HostedMcpError,
    HostedMcpQueryAdapter,
    HostedMcpUsageContext,
)
from yutome.hosted.models import EntitlementPolicy, ProviderAllocation, UsageEvent, UsageNormalization, WorkspaceBalance
from yutome.hosted.provider_wrappers import ProviderCallContext, execute_provider_call
from yutome.hosted.search_store import SearchStoreUsage


class RecordingSearchStore:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[dict[str, Any]] = []
        self.resources: dict[tuple[str, str, str], dict[str, Any]] = {}

    def lexical_search(self, *, workspace_id: str, query: str, limit: int) -> tuple[list[dict[str, Any]], SearchStoreUsage]:
        self.calls.append({"mode": "lexical", "workspace_id": workspace_id, "query": query, "limit": limit})
        return self.rows, SearchStoreUsage(
            operation="lexical_query",
            backend="postgres_fts_fallback",
            index_profile_ref="sip_default",
            units={"queries": 1, "candidate_limit": limit, "result_count": len(self.rows), "latency_ms": 2.5},
            metadata={"storage_backend": "postgres_vectorchord"},
        )

    def semantic_search(
        self,
        *,
        workspace_id: str,
        query_vector: list[float],
        limit: int,
    ) -> tuple[list[dict[str, Any]], SearchStoreUsage]:
        self.calls.append({"mode": "semantic", "workspace_id": workspace_id, "query_vector": query_vector, "limit": limit})
        return self.rows, SearchStoreUsage(
            operation="semantic_query",
            backend="postgres_vectorchord",
            index_profile_ref="sip_default",
            units={"queries": 1, "candidate_limit": limit, "query_vector_dimensions": len(query_vector), "latency_ms": 3.5},
            metadata={"storage_backend": "postgres_vectorchord"},
        )

    def hybrid_search(
        self,
        *,
        workspace_id: str,
        query: str,
        query_vector: list[float],
        limit: int,
    ) -> tuple[list[dict[str, Any]], SearchStoreUsage]:
        self.calls.append(
            {"mode": "hybrid", "workspace_id": workspace_id, "query": query, "query_vector": query_vector, "limit": limit}
        )
        return self.rows, SearchStoreUsage(
            operation="hybrid_query",
            backend="postgres_vectorchord_fts_fallback",
            index_profile_ref="sip_default",
            units={"queries": 1, "candidate_limit": limit, "query_vector_dimensions": len(query_vector)},
            metadata={"storage_backend": "postgres_vectorchord", "fusion": "rrf"},
        )

    def add_resource(self, workspace_id: str, kind: str, id_: str, payload: dict[str, Any]) -> None:
        self.resources[(workspace_id, kind, id_)] = payload

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
            "still_indexing": 2,
            "needs_attention": 0,
            "channels": 3,
            "videos": 4,
            "chunks": 5,
            "transcript_versions": 6,
            "statuses": {"indexed": 1, "pending": 2},
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
        if self.rows and "video_id" in self.rows[0] and "chunk_id" not in self.rows[0]:
            return self.rows
        return [{"video_id": video_id or "vid_1", "title": "Hosted video"}]

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
        if self.rows and "channel_id" in self.rows[0] and "chunk_id" not in self.rows[0]:
            return self.rows
        return [{"channel_id": channel or "chan_1", "title": "Hosted channel", "selected": selected}]

    def _resource(self, workspace_id: str, kind: str, id_: str) -> dict[str, Any]:
        from yutome.hosted.resources import HostedResourceNotFound

        try:
            return self.resources[(workspace_id, kind, id_)]
        except KeyError as exc:
            raise HostedResourceNotFound(kind=kind, id_=id_) from exc


class RecordingLedger:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    def append(self, event: UsageEvent) -> None:
        self.events.append(event)


@dataclass
class FakeVoyageResponse:
    embeddings: list[list[float]]
    usage: dict[str, int]
    model: str = "voyage-4-lite"


def _allowing_adapter(
    *,
    search_store: RecordingSearchStore,
    ledger: RecordingLedger | None = None,
    usage_context_provider=None,
    voyage_usage_context_provider=None,
    query_embedder=None,
) -> HostedMcpQueryAdapter:
    kwargs: dict[str, Any] = {
        "search_store": search_store,
        "usage_context_provider": usage_context_provider or _usage_context_provider(),
        "voyage_usage_context_provider": voyage_usage_context_provider or _voyage_usage_context_provider(),
    }
    if ledger is not None:
        kwargs["ledger"] = ledger
    if query_embedder is not None:
        kwargs["query_embedder"] = query_embedder
    return HostedMcpQueryAdapter(**kwargs)


def test_lexical_find_maps_search_rows_to_contract_response_and_records_usage() -> None:
    store = RecordingSearchStore(
        rows=[
            {
                "chunk_id": "chunk_1",
                "video_id": "video_internal_1",
                "youtube_video_id": "dQw4w9WgXcQ",
                "transcript_version_id": "tx_1",
                "chunk_index": 4,
                "start_seconds": 12.2,
                "end_seconds": 19.0,
                "text": "Crohn disease research and probiotics were discussed in this clip.",
                "title": "Gut health update",
                "lexical_score": 0.72,
                "score": 0.72,
                "match_type": "lexical",
            }
        ]
    )
    ledger = RecordingLedger()
    adapter = _allowing_adapter(search_store=store, ledger=ledger)

    payload = adapter.call_tool(
        auth=HostedMcpAuthContext(workspace_id="ws_alice", client_id="mcp_client"),
        name="find",
        arguments={"text": "Crohn probiotics", "mode": "lexical", "limit": 5},
    )

    assert store.calls == [{"mode": "lexical", "workspace_id": "ws_alice", "query": "Crohn probiotics", "limit": 5}]
    assert payload["notes"] == []
    row = payload["rows"][0]
    assert row["chunk_id"] == "chunk_1"
    assert row["resource_uri"] == "yutome://chunk/chunk_1"
    assert row["video_id"] == "video_internal_1"
    assert row["youtube_url"] == "https://youtube.com/watch?v=dQw4w9WgXcQ&t=12s"
    assert row["start_ms"] == 12200
    assert row["end_ms"] == 19000
    assert row["snippet"].startswith("Crohn disease research")
    assert row["scores"]["lexical_score"] == 0.72
    assert "text" not in row

    assert len(ledger.events) == 1
    event = ledger.events[0]
    assert event.subject == "search_store"
    assert event.operation == "lexical_query"
    assert event.event_type == "service_operation_succeeded"
    assert event.status == "succeeded"
    assert event.actual_units["queries"] == 1
    assert event.actual_units["candidate_limit"] == 5
    assert event.actual_units["result_count"] == 1
    assert event.metadata["allocation_id"] == "svc_ws_alice_search_store"
    assert event.metadata["mcp_client_id"] == "mcp_client"


def test_usage_denial_prevents_search_store_execution() -> None:
    store = RecordingSearchStore()
    ledger = RecordingLedger()
    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=ledger,
        usage_context_provider=_usage_context_provider(
            policy=EntitlementPolicy(
                id="policy",
                workspace_id="ws_alice",
                allowed_operations={"search_store.lexical_query"},
            ),
            balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"queries": 0, "candidate_limit": 100}),
        ),
    )

    with pytest.raises(HostedMcpError) as exc_info:
        adapter.call_tool(
            auth=HostedMcpAuthContext(workspace_id="ws_alice"),
            name="find",
            arguments={"text": "Crohn", "mode": "lexical", "limit": 5},
        )

    assert exc_info.value.code == "usage_denied"
    assert exc_info.value.to_dict()["error"]["data"]["operation"] == "search_store.lexical_query"
    assert store.calls == []
    assert len(ledger.events) == 1
    assert ledger.events[0].event_type == "reservation_created"
    assert ledger.events[0].status == "denied"
    assert ledger.events[0].error_code == "insufficient_balance"


def test_semantic_search_denial_prevents_embedding_and_search_store_execution() -> None:
    store = RecordingSearchStore()
    ledger = RecordingLedger()
    embedded = False
    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=ledger,
        usage_context_provider=_usage_context_provider(
            policy=EntitlementPolicy(
                id="policy",
                workspace_id="ws_alice",
                allowed_operations={"search_store.semantic_query"},
            ),
            balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"queries": 0, "candidate_limit": 100}),
        ),
        query_embedder=lambda _query, _context: _mark_embedded(),
    )

    def _mark_embedded() -> list[float]:
        nonlocal embedded
        embedded = True
        return [0.1, 0.2]

    with pytest.raises(HostedMcpError) as exc_info:
        adapter.call_tool(
            auth=HostedMcpAuthContext(workspace_id="ws_alice"),
            name="find",
            arguments={"text": "Crohn probiotics", "mode": "semantic", "limit": 5},
        )

    assert exc_info.value.to_dict()["error"]["data"]["operation"] == "search_store.semantic_query"
    assert embedded is False
    assert store.calls == []
    assert [event.status for event in ledger.events] == ["denied"]
    assert ledger.events[0].subject == "search_store"


def test_voyage_denial_prevents_embedding_and_search_store_execution() -> None:
    store = RecordingSearchStore()
    ledger = RecordingLedger()
    provider_called = False

    def embedder(_query: str, context: ProviderCallContext) -> list[float]:
        def call() -> FakeVoyageResponse:
            nonlocal provider_called
            provider_called = True
            return FakeVoyageResponse(embeddings=[[0.1, 0.2]], usage={"total_tokens": 6})

        result = execute_provider_call(
            context,
            call,
            normalize_usage=lambda response: UsageNormalization(
                subject="voyage",
                operation="embed_query",
                actual_units={"total_tokens": response.usage["total_tokens"], "vectors": len(response.embeddings)},
                metadata={"input_type": "query", "output_dimension": 2},
            ),
        )
        return result.embeddings[0]

    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=ledger,
        usage_context_provider=_usage_context_provider(),
        voyage_usage_context_provider=_voyage_usage_context_provider(
            policy=EntitlementPolicy(
                id="policy",
                workspace_id="ws_alice",
                allowed_operations={"voyage.embed_query"},
                max_units_by_operation={"voyage.embed_query": {"total_tokens": 1}},
            ),
        ),
        query_embedder=embedder,
    )

    with pytest.raises(HostedMcpError) as exc_info:
        adapter.call_tool(
            auth=HostedMcpAuthContext(workspace_id="ws_alice"),
            name="find",
            arguments={"text": "Crohn probiotics", "mode": "semantic", "limit": 5},
        )

    assert exc_info.value.code == "usage_denied"
    assert exc_info.value.to_dict()["error"]["data"]["operation"] == "voyage.embed_query"
    assert provider_called is False
    assert store.calls == []
    assert [event.subject for event in ledger.events] == ["voyage", "search_store"]
    assert ledger.events[0].status == "denied"
    release = ledger.events[1]
    assert release.event_type == "usage_reservation_released"
    assert release.status == "released"
    assert release.operation == "semantic_query"
    assert release.metadata["release_reason"] == "provider_usage_denied"
    assert release.metadata["provider_operation"] == "voyage.embed_query"


def test_hybrid_search_store_soft_denial_falls_back_to_lexical_without_embedding() -> None:
    store = RecordingSearchStore(
        rows=[
            {
                "chunk_id": "chunk_soft_search",
                "video_id": "vid_1",
                "youtube_video_id": "dQw4w9WgXcQ",
                "transcript_version_id": "tx_1",
                "start_seconds": 1,
                "end_seconds": 2,
                "text": "Lexical fallback after search-store soft denial.",
                "score": 0.4,
                "match_type": "lexical",
            }
        ]
    )
    ledger = RecordingLedger()
    embedded = False

    def usage_provider(
        auth: HostedMcpAuthContext,
        operation: str,
        estimated_units: dict[str, float],
    ) -> HostedMcpUsageContext:
        policy = EntitlementPolicy(
            id="policy",
            workspace_id=auth.workspace_id,
            allowed_operations={"search_store.hybrid_query", "search_store.lexical_query"},
        )
        balance = (
            WorkspaceBalance(workspace_id=auth.workspace_id)
            if operation == "hybrid_query"
            else WorkspaceBalance(workspace_id=auth.workspace_id, unlimited_units=set(estimated_units))
        )
        return HostedMcpUsageContext(
            allocation=default_search_store_allocation(workspace_id=auth.workspace_id, operation=operation),
            policy=policy,
            balance=balance,
        )

    def embedder(_query: str, _context: ProviderCallContext) -> list[float]:
        nonlocal embedded
        embedded = True
        return [0.1, 0.2]

    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=ledger,
        usage_context_provider=usage_provider,
        query_embedder=embedder,
    )

    result = adapter.call_tool(
        auth=HostedMcpAuthContext(workspace_id="ws_alice"),
        name="find",
        arguments={"text": "Crohn probiotics", "mode": "hybrid", "limit": 5},
    )

    assert embedded is False
    assert [call["mode"] for call in store.calls] == ["lexical"]
    assert result["rows"][0]["chunk_id"] == "chunk_soft_search"
    assert "hosted_find_fallback_to_lexical" in result["notes"]
    assert [event.status for event in ledger.events] == ["denied", "succeeded"]
    assert ledger.events[0].error_code == "insufficient_balance"
    assert ledger.events[0].metadata["mcp_search_mode"] == "hybrid"


def test_hybrid_voyage_soft_denial_falls_back_to_lexical() -> None:
    store = RecordingSearchStore(
        rows=[
            {
                "chunk_id": "chunk_soft_voyage",
                "video_id": "vid_1",
                "youtube_video_id": "dQw4w9WgXcQ",
                "transcript_version_id": "tx_1",
                "start_seconds": 1,
                "end_seconds": 2,
                "text": "Lexical fallback after Voyage soft denial.",
                "score": 0.4,
                "match_type": "lexical",
            }
        ]
    )
    ledger = RecordingLedger()
    provider_called = False

    def embedder(_query: str, context: ProviderCallContext) -> list[float]:
        def call() -> FakeVoyageResponse:
            nonlocal provider_called
            provider_called = True
            return FakeVoyageResponse(embeddings=[[0.1, 0.2]], usage={"total_tokens": 6})

        result = execute_provider_call(
            context,
            call,
            normalize_usage=lambda response: UsageNormalization(
                subject="voyage",
                operation="embed_query",
                actual_units={"total_tokens": response.usage["total_tokens"], "vectors": len(response.embeddings)},
                metadata={"input_type": "query", "output_dimension": 2},
            ),
        )
        return result.embeddings[0]

    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=ledger,
        usage_context_provider=_usage_context_provider(
            policy=EntitlementPolicy(
                id="policy_search",
                workspace_id="ws_alice",
                allowed_operations={"search_store.hybrid_query", "search_store.lexical_query"},
            ),
        ),
        voyage_usage_context_provider=_voyage_usage_context_provider(
            policy=EntitlementPolicy(
                id="policy_voyage",
                workspace_id="ws_alice",
                allowed_operations={"voyage.embed_query"},
            ),
            balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"total_tokens": 0, "vectors": 1}),
        ),
        query_embedder=embedder,
    )

    result = adapter.call_tool(
        auth=HostedMcpAuthContext(workspace_id="ws_alice"),
        name="find",
        arguments={"text": "Crohn probiotics", "mode": "hybrid", "limit": 5},
    )

    assert provider_called is False
    assert [call["mode"] for call in store.calls] == ["lexical"]
    assert result["rows"][0]["chunk_id"] == "chunk_soft_voyage"
    assert "hosted_find_fallback_to_lexical" in result["notes"]
    assert [event.subject for event in ledger.events] == ["voyage", "search_store", "search_store"]
    assert ledger.events[0].status == "denied"
    assert ledger.events[0].error_code == "insufficient_balance"
    assert ledger.events[1].event_type == "usage_reservation_released"
    assert ledger.events[1].metadata["release_reason"] == "provider_usage_denied"


@pytest.mark.parametrize(("status_code", "failure_kind"), [(429, "rate_limit"), (503, "transient")])
def test_hybrid_voyage_provider_availability_failure_falls_back_to_lexical(
    status_code: int,
    failure_kind: str,
) -> None:
    store = RecordingSearchStore(
        rows=[
            {
                "chunk_id": "chunk_lexical",
                "video_id": "vid_1",
                "start_ms": 0,
                "end_ms": 1000,
                "text": "Lexical fallback result.",
                "lexical_score": 0.5,
                "score": 0.5,
                "match_type": "lexical",
            }
        ]
    )
    ledger = RecordingLedger()

    class FakeProviderError(RuntimeError):
        def __init__(self) -> None:
            self.status_code = status_code
            super().__init__("provider unavailable")

    def embedder(_query: str, context: ProviderCallContext) -> list[float]:
        def call() -> FakeVoyageResponse:
            raise FakeProviderError()

        execute_provider_call(context, call)
        raise AssertionError("unreachable")

    adapter = _allowing_adapter(search_store=store, ledger=ledger, query_embedder=embedder)

    payload = adapter.call_tool(
        auth=HostedMcpAuthContext(workspace_id="ws_alice"),
        name="find",
        arguments={"text": "Crohn probiotics", "mode": "hybrid", "limit": 5},
    )

    assert store.calls == [{"mode": "lexical", "workspace_id": "ws_alice", "query": "Crohn probiotics", "limit": 5}]
    assert payload["rows"][0]["chunk_id"] == "chunk_lexical"
    assert payload["notes"][0] == "hosted_find_fallback_to_lexical"
    note_metadata = json.loads(payload["notes"][1].removeprefix("hosted_find_fallback_metadata:"))
    assert note_metadata["fallback_from"] == "hybrid"
    assert note_metadata["fallback_reason"] == "provider_availability"
    assert note_metadata["fallback_failure_kind"] == failure_kind
    assert [event.event_type for event in ledger.events] == [
        "provider_attempt_started",
        "provider_attempt_failed",
        "usage_reservation_released",
        "service_operation_succeeded",
    ]
    failed = ledger.events[1]
    assert failed.metadata["failure_kind"] == failure_kind
    assert failed.metadata["retryable"] is True
    assert failed.metadata["mcp_search_mode"] == "hybrid"
    release = ledger.events[2]
    assert release.status == "released"
    assert release.operation == "hybrid_query"
    assert release.metadata["fallback_reason"] == "provider_availability"
    fallback_event = ledger.events[3]
    assert fallback_event.operation == "lexical_query"
    assert fallback_event.metadata["fallback"] is True
    assert fallback_event.metadata["fallback_from"] == "hybrid"
    assert fallback_event.metadata["fallback_to"] == "lexical"


@pytest.mark.parametrize(("status_code", "failure_kind"), [(429, "rate_limit"), (503, "transient")])
def test_semantic_voyage_provider_availability_failure_does_not_fall_back_to_lexical(
    status_code: int,
    failure_kind: str,
) -> None:
    store = RecordingSearchStore()
    ledger = RecordingLedger()

    class FakeProviderError(RuntimeError):
        def __init__(self) -> None:
            self.status_code = status_code
            super().__init__("provider unavailable")

    def embedder(_query: str, context: ProviderCallContext) -> list[float]:
        def call() -> FakeVoyageResponse:
            raise FakeProviderError()

        execute_provider_call(context, call)
        raise AssertionError("unreachable")

    adapter = _allowing_adapter(search_store=store, ledger=ledger, query_embedder=embedder)

    with pytest.raises(HostedMcpError) as exc_info:
        adapter.call_tool(
            auth=HostedMcpAuthContext(workspace_id="ws_alice"),
            name="find",
            arguments={"text": "Crohn probiotics", "mode": "semantic", "limit": 5},
        )

    assert exc_info.value.code == "provider_call_failed"
    assert exc_info.value.to_dict()["error"]["data"]["failure_kind"] == failure_kind
    assert store.calls == []
    assert [event.event_type for event in ledger.events] == [
        "provider_attempt_started",
        "provider_attempt_failed",
        "usage_reservation_released",
    ]
    release = ledger.events[2]
    assert release.operation == "semantic_query"
    assert release.status == "released"
    assert release.metadata["release_reason"] == "provider_failure"
    assert release.metadata["failure_kind"] == failure_kind


def test_voyage_auth_failure_does_not_fall_back_to_lexical() -> None:
    store = RecordingSearchStore()
    ledger = RecordingLedger()

    class FakeProviderAuthError(RuntimeError):
        status_code = 401

    def embedder(_query: str, context: ProviderCallContext) -> list[float]:
        def call() -> object:
            raise FakeProviderAuthError("invalid api key")

        execute_provider_call(context, call)
        raise AssertionError("unreachable")

    adapter = _allowing_adapter(search_store=store, ledger=ledger, query_embedder=embedder)

    with pytest.raises(HostedMcpError) as exc_info:
        adapter.call_tool(
            auth=HostedMcpAuthContext(workspace_id="ws_alice"),
            name="find",
            arguments={"text": "Crohn probiotics", "mode": "semantic", "limit": 5},
        )

    assert exc_info.value.code == "provider_call_failed"
    assert exc_info.value.to_dict()["error"]["data"]["failure_kind"] == "auth"
    assert store.calls == []
    assert [event.event_type for event in ledger.events] == [
        "provider_attempt_started",
        "provider_attempt_failed",
        "usage_reservation_released",
    ]
    release = ledger.events[2]
    assert release.operation == "semantic_query"
    assert release.status == "released"
    assert release.metadata["release_reason"] == "provider_failure"
    assert release.metadata["failure_kind"] == "auth"


def test_hybrid_vector_store_availability_failure_falls_back_to_lexical() -> None:
    class VectorUnavailableStore(RecordingSearchStore):
        def hybrid_search(
            self,
            *,
            workspace_id: str,
            query: str,
            query_vector: list[float],
            limit: int,
        ) -> tuple[list[dict[str, Any]], SearchStoreUsage]:
            self.calls.append(
                {"mode": "hybrid", "workspace_id": workspace_id, "query": query, "query_vector": query_vector, "limit": limit}
            )
            raise RuntimeError("vector extension unavailable")

    store = VectorUnavailableStore(
        rows=[
            {
                "chunk_id": "chunk_lexical",
                "video_id": "vid_1",
                "start_ms": 0,
                "end_ms": 1000,
                "text": "Lexical fallback result.",
                "lexical_score": 0.5,
                "score": 0.5,
                "match_type": "lexical",
            }
        ]
    )
    ledger = RecordingLedger()
    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=ledger,
        usage_context_provider=_usage_context_provider(),
        voyage_usage_context_provider=_voyage_usage_context_provider(),
        query_embedder=_recording_embedder(vector=[0.1, 0.2], total_tokens=7),
    )

    payload = adapter.call_tool(
        auth=HostedMcpAuthContext(workspace_id="ws_alice"),
        name="find",
        arguments={"text": "Crohn probiotics", "mode": "hybrid", "limit": 5},
    )

    assert store.calls == [
        {
            "mode": "hybrid",
            "workspace_id": "ws_alice",
            "query": "Crohn probiotics",
            "query_vector": [0.1, 0.2],
            "limit": 5,
        },
        {"mode": "lexical", "workspace_id": "ws_alice", "query": "Crohn probiotics", "limit": 5},
    ]
    assert payload["rows"][0]["chunk_id"] == "chunk_lexical"
    assert payload["notes"][0] == "hosted_find_fallback_to_lexical"
    note_metadata = json.loads(payload["notes"][1].removeprefix("hosted_find_fallback_metadata:"))
    assert note_metadata["fallback_reason"] == "vector_store_availability"
    assert note_metadata["fallback_operation"] == "search_store.hybrid_query"
    assert [event.event_type for event in ledger.events] == [
        "provider_attempt_started",
        "provider_attempt_succeeded",
        "service_operation_failed",
        "service_operation_succeeded",
    ]
    failed = ledger.events[2]
    assert failed.operation == "hybrid_query"
    assert failed.status == "failed"
    assert failed.metadata["fallback_reason"] == "vector_store_availability"
    fallback_event = ledger.events[3]
    assert fallback_event.operation == "lexical_query"
    assert fallback_event.metadata["fallback_reason"] == "vector_store_availability"


def test_semantic_vector_store_availability_failure_does_not_fall_back_to_lexical() -> None:
    class VectorUnavailableStore(RecordingSearchStore):
        def semantic_search(
            self,
            *,
            workspace_id: str,
            query_vector: list[float],
            limit: int,
        ) -> tuple[list[dict[str, Any]], SearchStoreUsage]:
            self.calls.append({"mode": "semantic", "workspace_id": workspace_id, "query_vector": query_vector, "limit": limit})
            raise RuntimeError("vector extension unavailable")

    store = VectorUnavailableStore()
    ledger = RecordingLedger()
    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=ledger,
        usage_context_provider=_usage_context_provider(),
        voyage_usage_context_provider=_voyage_usage_context_provider(),
        query_embedder=_recording_embedder(vector=[0.1, 0.2], total_tokens=7),
    )

    with pytest.raises(RuntimeError, match="vector extension unavailable"):
        adapter.call_tool(
            auth=HostedMcpAuthContext(workspace_id="ws_alice"),
            name="find",
            arguments={"text": "Crohn probiotics", "mode": "semantic", "limit": 5},
        )

    assert store.calls == [{"mode": "semantic", "workspace_id": "ws_alice", "query_vector": [0.1, 0.2], "limit": 5}]
    assert [event.event_type for event in ledger.events] == [
        "provider_attempt_started",
        "provider_attempt_succeeded",
        "service_operation_failed",
    ]
    failed = ledger.events[2]
    assert failed.operation == "semantic_query"
    assert failed.status == "failed"
    assert failed.metadata["failure_kind"] == "unknown"
    assert failed.metadata["message"] == "vector extension unavailable"


def test_vector_store_tenant_error_does_not_fall_back_to_lexical() -> None:
    class TenantDeniedStore(RecordingSearchStore):
        def semantic_search(
            self,
            *,
            workspace_id: str,
            query_vector: list[float],
            limit: int,
        ) -> tuple[list[dict[str, Any]], SearchStoreUsage]:
            self.calls.append({"mode": "semantic", "workspace_id": workspace_id, "query_vector": query_vector, "limit": limit})
            raise PermissionError("permission denied by tenant policy")

    store = TenantDeniedStore()
    ledger = RecordingLedger()
    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=ledger,
        usage_context_provider=_usage_context_provider(),
        voyage_usage_context_provider=_voyage_usage_context_provider(),
        query_embedder=_recording_embedder(vector=[0.1, 0.2], total_tokens=7),
    )

    with pytest.raises(PermissionError):
        adapter.call_tool(
            auth=HostedMcpAuthContext(workspace_id="ws_alice"),
            name="find",
            arguments={"text": "Crohn probiotics", "mode": "semantic", "limit": 5},
        )

    assert store.calls == [{"mode": "semantic", "workspace_id": "ws_alice", "query_vector": [0.1, 0.2], "limit": 5}]
    assert [event.event_type for event in ledger.events] == [
        "provider_attempt_started",
        "provider_attempt_succeeded",
        "service_operation_failed",
    ]
    failed = ledger.events[2]
    assert failed.operation == "semantic_query"
    assert failed.status == "failed"
    assert failed.metadata["failure_kind"] == "unknown"
    assert failed.metadata["message"] == "permission denied by tenant policy"


def test_semantic_success_records_voyage_then_search_store_events_and_metadata() -> None:
    store = RecordingSearchStore(
        rows=[
            {
                "chunk_id": "chunk_1",
                "video_id": "vid_1",
                "start_ms": 0,
                "end_ms": 1000,
                "text": "Crohn probiotic trial summary.",
                "vector_score": 0.8,
                "score": 0.8,
                "match_type": "semantic",
            }
        ]
    )
    ledger = RecordingLedger()
    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=ledger,
        usage_context_provider=_usage_context_provider(),
        voyage_usage_context_provider=_voyage_usage_context_provider(),
        query_embedder=_recording_embedder(vector=[0.1, 0.2], total_tokens=7),
    )

    payload = adapter.call_tool(
        auth=HostedMcpAuthContext(workspace_id="ws_alice", client_id="mcp_client"),
        name="find",
        arguments={"text": "Crohn probiotics", "mode": "semantic", "limit": 3},
    )

    assert store.calls == [{"mode": "semantic", "workspace_id": "ws_alice", "query_vector": [0.1, 0.2], "limit": 3}]
    assert payload["rows"][0]["match_type"] == "semantic"
    assert payload["rows"][0]["scores"]["vector_score"] == 0.8
    assert [event.event_type for event in ledger.events] == [
        "provider_attempt_started",
        "provider_attempt_succeeded",
        "service_operation_succeeded",
    ]
    voyage_event = ledger.events[1]
    assert voyage_event.subject == "voyage"
    assert voyage_event.operation == "embed_query"
    assert voyage_event.actual_units["total_tokens"] == 7
    assert voyage_event.actual_units["vectors"] == 1
    assert voyage_event.metadata["input_type"] == "query"
    assert voyage_event.metadata["output_dimension"] == 2
    search_event = ledger.events[2]
    assert search_event.subject == "search_store"
    assert search_event.operation == "semantic_query"
    assert search_event.actual_units["query_vector_dimensions"] == 2
    assert search_event.actual_units["result_count"] == 1
    assert search_event.metadata["mcp_client_id"] == "mcp_client"


def test_hybrid_success_calls_hybrid_search_and_records_search_store_metadata() -> None:
    store = RecordingSearchStore(
        rows=[
            {
                "chunk_id": "chunk_1",
                "video_id": "vid_1",
                "start_ms": 0,
                "end_ms": 1000,
                "text": "Crohn probiotic trial summary.",
                "lexical_score": 0.4,
                "vector_score": 0.8,
                "hybrid_score": 0.9,
                "score": 0.9,
                "match_type": "hybrid",
            }
        ]
    )
    ledger = RecordingLedger()
    adapter = HostedMcpQueryAdapter(
        search_store=store,
        ledger=ledger,
        usage_context_provider=_usage_context_provider(),
        voyage_usage_context_provider=_voyage_usage_context_provider(),
        query_embedder=_recording_embedder(vector=[0.1, 0.2, 0.3], total_tokens=8),
    )

    payload = adapter.call_tool(
        auth=HostedMcpAuthContext(workspace_id="ws_alice"),
        name="find",
        arguments={"text": "Crohn probiotics", "mode": "hybrid", "limit": 4},
    )

    assert store.calls == [
        {
            "mode": "hybrid",
            "workspace_id": "ws_alice",
            "query": "Crohn probiotics",
            "query_vector": [0.1, 0.2, 0.3],
            "limit": 4,
        }
    ]
    assert payload["rows"][0]["scores"]["hybrid_score"] == 0.9
    search_event = ledger.events[2]
    assert search_event.operation == "hybrid_query"
    assert search_event.actual_units["candidate_limit"] == 4
    assert search_event.actual_units["query_vector_dimensions"] == 3
    assert search_event.metadata["fusion"] == "rrf"


def test_lexical_mode_does_not_construct_provider_context_or_call_embedder() -> None:
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(
        search_store=store,
        usage_context_provider=_usage_context_provider(),
        voyage_usage_context_provider=lambda _auth, _operation, _estimated: pytest.fail("lexical must not need Voyage"),
        query_embedder=lambda _query, _context: pytest.fail("lexical must not embed"),
    )

    payload = adapter.call_tool(
        auth=HostedMcpAuthContext(workspace_id="ws_alice"),
        name="find",
        arguments={"text": "Crohn", "limit": 2},
    )

    assert payload["notes"] == ["hosted_find_defaulted_to_lexical"]
    assert store.calls == [{"mode": "lexical", "workspace_id": "ws_alice", "query": "Crohn", "limit": 2}]


def test_workspace_arg_injection_is_rejected_before_search() -> None:
    store = RecordingSearchStore()
    adapter = _allowing_adapter(search_store=store)

    with pytest.raises(HostedMcpError) as exc_info:
        adapter.call_tool(
            auth=HostedMcpAuthContext(workspace_id="ws_real"),
            name="find",
            arguments={"text": "Crohn", "mode": "lexical", "workspace_id": "ws_evil"},
        )

    assert exc_info.value.code == "workspace_argument_not_allowed"
    assert store.calls == []


def test_unsupported_tool_and_resource_return_clear_errors() -> None:
    store = RecordingSearchStore()
    adapter = _allowing_adapter(search_store=store)
    auth = HostedMcpAuthContext(workspace_id="ws_alice")

    with pytest.raises(HostedMcpError) as tool_exc:
        adapter.call_tool(auth=auth, name="unknown", arguments={})

    assert tool_exc.value.code == "unsupported_tool"
    assert tool_exc.value.to_dict()["error"]["data"]["supported"] == ["find", "list", "q", "show"]

    with pytest.raises(HostedMcpError) as resource_exc:
        adapter.read_resource(auth=auth, uri="yutome://unknown/chunk_1")

    assert resource_exc.value.code == "unsupported_resource"
    assert resource_exc.value.status_code == 404
    assert store.calls == []


@pytest.mark.parametrize(
    ("uri", "kind", "id_"),
    [
        ("yutome://chunk/chunk_1", "chunk", "chunk_1"),
        ("yutome://video/vid_1", "video", "vid_1"),
        ("yutome://channel/chan_1", "channel", "chan_1"),
        ("yutome://transcript/tx_1", "transcript", "tx_1"),
    ],
)
def test_read_resource_dispatches_supported_contract_resources_with_workspace_scope(
    uri: str,
    kind: str,
    id_: str,
) -> None:
    store = RecordingSearchStore()
    payload = {"resource_uri": uri, f"{kind}_id" if kind != "transcript" else "transcript_version_id": id_}
    store.add_resource("ws_alice", kind, id_, payload)
    adapter = HostedMcpQueryAdapter(search_store=store)

    result = adapter.read_resource(auth=HostedMcpAuthContext(workspace_id="ws_alice"), uri=uri)

    assert result == payload
    assert store.calls[0]["workspace_id"] == "ws_alice"
    assert store.calls[0]["resource"] == kind


def test_read_resource_missing_and_cross_workspace_are_both_404() -> None:
    store = RecordingSearchStore()
    store.add_resource("ws_alice", "chunk", "chunk_1", {"chunk_id": "chunk_1"})
    adapter = HostedMcpQueryAdapter(search_store=store)

    with pytest.raises(HostedMcpError) as missing_exc:
        adapter.read_resource(auth=HostedMcpAuthContext(workspace_id="ws_alice"), uri="yutome://chunk/missing")
    with pytest.raises(HostedMcpError) as cross_workspace_exc:
        adapter.read_resource(auth=HostedMcpAuthContext(workspace_id="ws_bob"), uri="yutome://chunk/chunk_1")

    assert missing_exc.value.code == "resource_not_found"
    assert missing_exc.value.status_code == 404
    assert cross_workspace_exc.value.code == "resource_not_found"
    assert cross_workspace_exc.value.status_code == 404


@pytest.mark.parametrize(
    ("kind", "id_"),
    [
        ("chunk", "chunk_1"),
        ("video", "vid_1"),
        ("channel", "chan_1"),
        ("transcript", "tx_1"),
        ("source", "src_1"),
    ],
)
def test_show_maps_supported_kinds_to_resource_helpers(kind: str, id_: str) -> None:
    store = RecordingSearchStore()
    payload = {"resource_uri": f"yutome://{kind}/{id_}", "id": id_}
    store.add_resource("ws_alice", kind, id_, payload)
    adapter = HostedMcpQueryAdapter(search_store=store)

    result = adapter.call_tool(
        auth=HostedMcpAuthContext(workspace_id="ws_alice"),
        name="show",
        arguments={"kind": kind, "id_": id_, "transcript_offset": 2, "transcript_limit": 3},
    )

    assert result == payload
    assert store.calls[0]["resource"] == kind
    assert store.calls[0]["workspace_id"] == "ws_alice"
    if kind == "transcript":
        assert store.calls[0]["offset"] == 2
        assert store.calls[0]["limit"] == 3


def test_show_context_returns_structured_unsupported_error() -> None:
    adapter = HostedMcpQueryAdapter(search_store=RecordingSearchStore())

    with pytest.raises(HostedMcpError) as exc_info:
        adapter.call_tool(
            auth=HostedMcpAuthContext(workspace_id="ws_alice"),
            name="show",
            arguments={"kind": "context", "id_": "chunk_1"},
        )

    assert exc_info.value.code == "unsupported_show_context"
    assert exc_info.value.status_code == 501


def test_list_status_videos_and_channels_are_workspace_scoped() -> None:
    store = RecordingSearchStore()
    adapter = HostedMcpQueryAdapter(search_store=store)
    auth = HostedMcpAuthContext(workspace_id="ws_alice")

    status = adapter.call_tool(auth=auth, name="list", arguments={"entity": "status"})
    videos = adapter.call_tool(
        auth=auth,
        name="list",
        arguments={"entity": "videos", "channel": "chan_1", "order_by": "newest", "limit": 2, "offset": 4},
    )
    channels = adapter.call_tool(
        auth=auth,
        name="list",
        arguments={"entity": "channels", "selected": True, "limit": 3},
    )

    assert status["rows"][0]["videos"] == 4
    assert videos["rows"][0]["video_id"] == "vid_1"
    assert channels["rows"][0]["channel_id"] == "chan_1"
    assert store.calls == [
        {"list": "status", "workspace_id": "ws_alice"},
        {
            "list": "videos",
            "workspace_id": "ws_alice",
            "limit": 2,
            "offset": 4,
            "channel": "chan_1",
            "video_id": None,
            "order_by": "newest",
        },
        {
            "list": "channels",
            "workspace_id": "ws_alice",
            "limit": 3,
            "offset": 0,
            "channel": None,
            "selected": True,
        },
    ]


def test_list_attention_and_advanced_filters_return_structured_unsupported() -> None:
    adapter = HostedMcpQueryAdapter(search_store=RecordingSearchStore())

    with pytest.raises(HostedMcpError) as attention_exc:
        adapter.call_tool(
            auth=HostedMcpAuthContext(workspace_id="ws_alice"),
            name="list",
            arguments={"entity": "attention"},
        )
    with pytest.raises(HostedMcpError) as filter_exc:
        adapter.call_tool(
            auth=HostedMcpAuthContext(workspace_id="ws_alice"),
            name="list",
            arguments={"entity": "videos", "since": "2026-01-01"},
        )

    assert attention_exc.value.code == "unsupported_list_entity"
    assert attention_exc.value.status_code == 501
    assert filter_exc.value.code == "unsupported_list_filter"
    assert filter_exc.value.status_code == 501


def test_q_accepts_safe_status_video_channel_and_chunk_shapes() -> None:
    store = RecordingSearchStore(
        rows=[
            {
                "chunk_id": "chunk_1",
                "video_id": "vid_1",
                "start_seconds": 1,
                "end_seconds": 2,
                "text": "Hosted chunk search.",
                "lexical_score": 0.5,
                "score": 0.5,
            }
        ]
    )
    adapter = _allowing_adapter(search_store=store)
    auth = HostedMcpAuthContext(workspace_id="ws_alice")

    status = adapter.call_tool(auth=auth, name="q", arguments={"request": {"project": "status_breakdown"}})
    videos = adapter.call_tool(
        auth=auth,
        name="q",
        arguments={
            "request": {
                "entity": "video",
                "project": "video_card",
                "filter": {"video_id": {"eq": "vid_1"}, "channel_id": {"eq": "chan_1"}},
                "order_by": [{"field": "published_at", "direction": "desc"}],
                "limit": 2,
                "offset": 1,
            }
        },
    )
    channels = adapter.call_tool(
        auth=auth,
        name="q",
        arguments={"request": {"entity": "channel", "filter": {"channel_selected": {"eq": True}}, "limit": 2}},
    )
    chunks = adapter.call_tool(
        auth=auth,
        name="q",
        arguments={
            "request": {
                "entity": "chunk",
                "search": {"over": "chunk_text", "mode": "lexical", "text": "Crohn"},
                "project": "chunk",
                "limit": 1,
            }
        },
    )

    assert status["rows"][0]["searchable_now"] == 1
    assert videos["rows"][0]["video_id"] == "vid_1"
    assert channels["rows"][0]["channel_id"] == "chan_1"
    assert chunks["rows"][0]["chunk_id"] == "chunk_1"
    assert store.calls[:3] == [
        {"list": "status", "workspace_id": "ws_alice"},
        {
            "list": "videos",
            "workspace_id": "ws_alice",
            "limit": 2,
            "offset": 1,
            "channel": "chan_1",
            "video_id": "vid_1",
            "order_by": "newest",
        },
        {
            "list": "channels",
            "workspace_id": "ws_alice",
            "limit": 2,
            "offset": 0,
            "channel": None,
            "selected": True,
        },
    ]
    assert store.calls[3] == {"mode": "lexical", "workspace_id": "ws_alice", "query": "Crohn", "limit": 1}


def test_q_rejects_unsupported_shapes_and_nested_workspace_injection() -> None:
    adapter = HostedMcpQueryAdapter(search_store=RecordingSearchStore())

    with pytest.raises(HostedMcpError) as unsupported_exc:
        adapter.call_tool(
            auth=HostedMcpAuthContext(workspace_id="ws_alice"),
            name="q",
            arguments={"request": {"entity": "chunk", "search": {"mode": "semantic", "text": "Crohn"}}},
        )
    with pytest.raises(HostedMcpError) as workspace_exc:
        adapter.call_tool(
            auth=HostedMcpAuthContext(workspace_id="ws_alice"),
            name="q",
            arguments={"request": {"entity": "video", "workspace_id": "ws_evil"}},
        )

    assert unsupported_exc.value.code == "unsupported_q_shape"
    assert unsupported_exc.value.status_code == 501
    assert workspace_exc.value.code == "workspace_argument_not_allowed"
    assert workspace_exc.value.to_dict()["error"]["data"]["arguments"] == ["request.workspace_id"]


def _usage_context_provider(
    *,
    policy: EntitlementPolicy | None = None,
    balance: WorkspaceBalance | None = None,
):
    def _provider(auth: HostedMcpAuthContext, operation: str, estimated_units: dict[str, float]) -> HostedMcpUsageContext:
        return HostedMcpUsageContext(
            allocation=default_search_store_allocation(workspace_id=auth.workspace_id, operation=operation),
            policy=policy
            or EntitlementPolicy(
                id="policy",
                workspace_id=auth.workspace_id,
                allowed_operations={f"search_store.{operation}"},
            ),
            balance=balance
            or WorkspaceBalance(
                workspace_id=auth.workspace_id,
                unlimited_units=set(estimated_units),
            ),
        )

    return _provider


def _voyage_usage_context_provider(
    *,
    policy: EntitlementPolicy | None = None,
    balance: WorkspaceBalance | None = None,
):
    def _provider(auth: HostedMcpAuthContext, operation: str, estimated_units: dict[str, float]) -> HostedMcpUsageContext:
        return HostedMcpUsageContext(
            allocation=ProviderAllocation(
                id=f"alloc_{auth.workspace_id}_voyage",
                workspace_id=auth.workspace_id,
                provider="voyage",
                operation=operation,
            ),
            policy=policy
            or EntitlementPolicy(
                id="policy",
                workspace_id=auth.workspace_id,
                allowed_operations={f"voyage.{operation}"},
            ),
            balance=balance
            or WorkspaceBalance(
                workspace_id=auth.workspace_id,
                unlimited_units=set(estimated_units),
            ),
        )

    return _provider


def _recording_embedder(*, vector: list[float], total_tokens: int):
    def _embedder(_query: str, context: ProviderCallContext) -> list[float]:
        response = execute_provider_call(
            context,
            lambda: FakeVoyageResponse(embeddings=[vector], usage={"total_tokens": total_tokens}),
            normalize_usage=lambda result: UsageNormalization(
                subject="voyage",
                operation="embed_query",
                actual_units={"total_tokens": result.usage["total_tokens"], "vectors": len(result.embeddings)},
                metadata={"input_type": "query", "output_dimension": len(vector)},
            ),
        )
        return response.embeddings[0]

    return _embedder
