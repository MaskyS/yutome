from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from yutome.config import default_config
from yutome.db import bootstrap_catalog, connect_catalog
from yutome.embeddings import _embed_voyage_batch, _embed_voyage_query, embed_pending_chunks
from yutome.hosted.gate import UsageGate
from yutome.hosted.models import (
    EntitlementPolicy,
    ProviderAllocation,
    UsageEvent,
    WorkspaceBalance,
)
from yutome.hosted.provider_wrappers import ProviderCallContext, UsageReservationDenied


class RecordingLedger:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    def append(self, event: UsageEvent) -> None:
        self.events.append(event)


class CountingGate(UsageGate):
    def __init__(self) -> None:
        super().__init__()
        self.reserve_calls: list[dict[str, Any]] = []

    def reserve(self, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.reserve_calls.append(dict(kwargs))
        return super().reserve(**kwargs)


@dataclass
class FakeVoyageResponse:
    embeddings: list[list[float]]
    usage: dict[str, int]
    model: str = "voyage-4-lite"


def _context(
    ledger: RecordingLedger,
    *,
    gate: UsageGate | None = None,
    operation: str = "embed_documents",
    estimated_units: dict[str, float] | None = None,
    policy: EntitlementPolicy | None = None,
) -> ProviderCallContext:
    return ProviderCallContext(
        gate=gate or UsageGate(),
        ledger=ledger,
        workspace_id="ws_alice",
        subject="voyage",
        operation=operation,
        estimated_units=estimated_units or {"total_tokens": 100},
        allocation=ProviderAllocation(
            id="alloc_voyage",
            workspace_id="ws_alice",
            provider="voyage",
            operation=operation,
        ),
        policy=policy
        or EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={f"voyage.{operation}"},
        ),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"total_tokens": 500}),
        idempotency_key=f"ws_alice:vid_123:voyage.{operation}:h_test",
        metadata={"job_id": "job_1"},
    )


def _batch() -> list[dict[str, Any]]:
    return [
        {
            "chunk_id": "chunk-a",
            "channel_id": "chan-1",
            "video_id": "vid-1",
            "transcript_version_id": "tx-1",
            "source": "captions",
            "language": "en",
            "is_generated": 1,
            "sequence": 0,
            "start_ms": 0,
            "end_ms": 1000,
            "text": "first chunk",
            "token_count": 2,
            "text_hash": "hash-a",
            "chunker_version": "test-v1",
        },
        {
            "chunk_id": "chunk-b",
            "channel_id": "chan-1",
            "video_id": "vid-1",
            "transcript_version_id": "tx-1",
            "source": "captions",
            "language": "en",
            "is_generated": 1,
            "sequence": 1,
            "start_ms": 1000,
            "end_ms": 2000,
            "text": "second chunk",
            "token_count": 2,
            "text_hash": "hash-b",
            "chunker_version": "test-v1",
        },
    ]


def test_voyage_batch_without_hosted_context_uses_direct_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class FakeClient:
        def embed(self, texts: list[str], **kwargs: Any) -> FakeVoyageResponse:
            calls.append({"texts": texts, **kwargs})
            return FakeVoyageResponse(embeddings=[[0.1, 0.2], [0.3, 0.4]], usage={"total_tokens": 12})

    import voyageai

    monkeypatch.setattr(voyageai, "Client", FakeClient)

    records = _embed_voyage_batch(
        _batch(),
        model="voyage-4-lite",
        dimension=1024,
        max_retries=0,
        retry_base_seconds=0,
    )

    assert calls == [
        {
            "texts": ["first chunk", "second chunk"],
            "model": "voyage-4-lite",
            "input_type": "document",
            "output_dimension": 1024,
        }
    ]
    assert [record["vector"] for record in records] == [[0.1, 0.2], [0.3, 0.4]]


def test_hosted_voyage_batch_records_normalized_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = RecordingLedger()

    class FakeClient:
        def embed(self, texts: list[str], **kwargs: Any) -> FakeVoyageResponse:
            assert texts == ["first chunk", "second chunk"]
            assert kwargs["input_type"] == "document"
            return FakeVoyageResponse(embeddings=[[0.1, 0.2], [0.3, 0.4]], usage={"total_tokens": 91})

    import voyageai

    monkeypatch.setattr(voyageai, "Client", FakeClient)

    records = _embed_voyage_batch(
        _batch(),
        model="voyage-4-lite",
        dimension=1024,
        max_retries=0,
        retry_base_seconds=0,
        hosted_context=_context(ledger),
    )

    assert [record["chunk_id"] for record in records] == ["chunk-a", "chunk-b"]
    assert [event.status for event in ledger.events] == ["started", "succeeded"]
    succeeded = ledger.events[1]
    assert succeeded.subject == "voyage"
    assert succeeded.operation == "embed_documents"
    assert succeeded.actual_units["total_tokens"] == 91
    assert succeeded.actual_units["vectors"] == 2
    assert succeeded.metadata["input_type"] == "document"
    assert succeeded.metadata["output_dimension"] == 1024
    assert succeeded.metadata["job_id"] == "job_1"


def test_hosted_voyage_batch_retry_uses_one_reservation_and_one_success_charge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = RecordingLedger()
    gate = CountingGate()
    calls = 0

    class FakeTransientError(RuntimeError):
        status_code = 503

    class FakeClient:
        def embed(self, texts: list[str], **kwargs: Any) -> FakeVoyageResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise FakeTransientError("Service Unavailable")
            return FakeVoyageResponse(embeddings=[[0.1, 0.2], [0.3, 0.4]], usage={"total_tokens": 91})

    import voyageai

    monkeypatch.setattr(voyageai, "Client", FakeClient)

    records = _embed_voyage_batch(
        _batch(),
        model="voyage-4-lite",
        dimension=1024,
        max_retries=1,
        retry_base_seconds=0,
        hosted_context=_context(ledger, gate=gate),
    )

    assert calls == 2
    assert [record["chunk_id"] for record in records] == ["chunk-a", "chunk-b"]
    assert len(gate.reserve_calls) == 1
    assert [event.status for event in ledger.events] == ["started", "succeeded"]
    assert len({event.reservation_id for event in ledger.events}) == 1
    assert ledger.events[1].actual_units["total_tokens"] == 91
    assert ledger.events[1].actual_units["vectors"] == 2


def test_hosted_voyage_denial_prevents_client_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = RecordingLedger()
    constructed = False

    class FakeClient:
        def __init__(self) -> None:
            nonlocal constructed
            constructed = True

        def embed(self, texts: list[str], **kwargs: Any) -> FakeVoyageResponse:
            raise AssertionError("denied hosted reservations must not call Voyage")

    import voyageai

    monkeypatch.setattr(voyageai, "Client", FakeClient)
    context = _context(
        ledger,
        estimated_units={"total_tokens": 600},
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={"voyage.embed_documents"},
            max_units_by_operation={"voyage.embed_documents": {"total_tokens": 500}},
        ),
    )

    with pytest.raises(UsageReservationDenied):
        _embed_voyage_batch(
            _batch(),
            model="voyage-4-lite",
            dimension=1024,
            max_retries=0,
            retry_base_seconds=0,
            hosted_context=context,
        )

    assert constructed is False
    assert [event.status for event in ledger.events] == ["denied"]
    assert ledger.events[0].error_code == "usage_limit_exceeded"


def test_hosted_voyage_query_records_normalized_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = RecordingLedger()

    class FakeClient:
        def embed(self, texts: list[str], **kwargs: Any) -> FakeVoyageResponse:
            assert texts == ["crohn probiotics"]
            assert kwargs["input_type"] == "query"
            return FakeVoyageResponse(embeddings=[[0.9, 0.8]], usage={"total_tokens": 7})

    import voyageai

    monkeypatch.setattr(voyageai, "Client", FakeClient)

    vector = _embed_voyage_query(
        query="crohn probiotics",
        model="voyage-4-lite",
        dimension=1024,
        hosted_context=_context(ledger, operation="embed_query", estimated_units={"total_tokens": 10}),
    )

    assert vector == [0.9, 0.8]
    assert [event.status for event in ledger.events] == ["started", "succeeded"]
    succeeded = ledger.events[1]
    assert succeeded.operation == "embed_query"
    assert succeeded.actual_units["total_tokens"] == 7
    assert succeeded.actual_units["vectors"] == 1
    assert succeeded.metadata["input_type"] == "query"


def test_embed_pending_chunks_uses_opt_in_hosted_context_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger = RecordingLedger()
    batches_seen: list[list[str]] = []

    class FakeClient:
        def embed(self, texts: list[str], **kwargs: Any) -> FakeVoyageResponse:
            assert texts == ["first chunk", "second chunk"]
            return FakeVoyageResponse(embeddings=[[0.1, 0.2], [0.3, 0.4]], usage={"total_tokens": 11})

    import voyageai

    monkeypatch.setattr(voyageai, "Client", FakeClient)
    db_path = tmp_path / "catalog.db"
    bootstrap_catalog(db_path)
    config = default_config()
    config = config.model_copy(
        update={
            "embeddings": config.embeddings.model_copy(
                update={
                    "enabled": True,
                    "batch_size": 2,
                    "concurrency": 1,
                    "max_retries": 0,
                    "retry_base_seconds": 0.0,
                }
            )
        }
    )

    with connect_catalog(db_path) as connection:
        _insert_pending_chunks(connection)

        def context_factory(batch: list[dict[str, Any]]) -> ProviderCallContext:
            batches_seen.append([row["chunk_id"] for row in batch])
            return _context(
                ledger,
                estimated_units={"total_tokens": sum(float(row["token_count"] or 0) for row in batch)},
            )

        stats = embed_pending_chunks(
            connection=connection,
            config=config,
            lancedb_dir=tmp_path / "lancedb",
            hosted_context_factory=context_factory,
        )

        embedded_count = connection.execute(
            "SELECT COUNT(*) FROM embeddings WHERE index_status = 'indexed'"
        ).fetchone()[0]

    assert stats.embedded_chunks == 2
    assert batches_seen == [["chunk-a", "chunk-b"]]
    assert embedded_count == 2
    assert [event.status for event in ledger.events] == ["started", "succeeded"]
    assert ledger.events[1].actual_units["total_tokens"] == 11
    assert ledger.events[1].actual_units["vectors"] == 2


def _insert_pending_chunks(connection: Any) -> None:
    connection.execute("INSERT INTO channels(channel_id, title) VALUES ('chan-1', 'Test Channel')")
    connection.execute(
        """
        INSERT INTO videos(video_id, channel_id, title, description, ingest_status)
        VALUES ('vid-1', 'chan-1', 'Video', 'Description', 'indexed')
        """
    )
    connection.execute(
        """
        INSERT INTO transcript_versions(
            transcript_version_id, video_id, source, language, is_generated,
            raw_path, normalized_path, text_hash, segment_count, active
        )
        VALUES ('tx-1', 'vid-1', 'captions', 'en', 1, 'raw.json', 'normalized.jsonl', 'hash', 2, 1)
        """
    )
    connection.executemany(
        """
        INSERT INTO chunks(
            chunk_id, transcript_version_id, video_id, channel_id, sequence,
            start_ms, end_ms, text, token_count, text_hash, chunker_version
        )
        VALUES (?, 'tx-1', 'vid-1', 'chan-1', ?, ?, ?, ?, ?, ?, 'test-v1')
        """,
        [
            ("chunk-a", 0, 0, 1000, "first chunk", 2, "hash-a"),
            ("chunk-b", 1, 1000, 2000, "second chunk", 3, "hash-b"),
        ],
    )
    connection.commit()
