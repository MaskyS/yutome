from __future__ import annotations

import asyncio

import pytest

from yutome.config import GeminiConfig
from yutome.gemini import _transcribe_gemini_window
from yutome.hosted.gate import UsageGate
from yutome.hosted.models import EntitlementPolicy, ProviderAllocation, UsageEvent, WorkspaceBalance
from yutome.hosted.provider_wrappers import ProviderCallContext, UsageReservationDenied
from yutome.quality_llm import _cleanup_batch_async
from yutome.transcripts import TranscriptSegment


class RecordingLedger:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    def append(self, event: UsageEvent) -> None:
        self.events.append(event)


class FakeGeminiResponse:
    def __init__(self, *, text: str, usage_metadata: dict[str, object], response_id: str = "resp_1") -> None:
        self.text = text
        self.usage_metadata = usage_metadata
        self.response_id = response_id
        self.model_version = "gemini-test"
        self.candidates = [{"finish_reason": "STOP"}]


class FakeTypes:
    class MediaResolution:
        MEDIA_RESOLUTION_LOW = "low"
        MEDIA_RESOLUTION_MEDIUM = "medium"
        MEDIA_RESOLUTION_HIGH = "high"

    class Content:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class Part:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class FileData:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class VideoMetadata:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class GenerateContentConfig:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class ThinkingConfig:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs


class FakeSyncModels:
    def __init__(self, response: FakeGeminiResponse) -> None:
        self.response = response
        self.calls = 0

    def generate_content(self, **kwargs: object) -> FakeGeminiResponse:
        self.calls += 1
        return self.response


class FakeSyncClient:
    def __init__(self, response: FakeGeminiResponse) -> None:
        self.models = FakeSyncModels(response)


class FakeAsyncModels:
    def __init__(self, response: FakeGeminiResponse | list[FakeGeminiResponse]) -> None:
        self.responses = response if isinstance(response, list) else [response]
        self.calls = 0

    async def generate_content(self, **kwargs: object) -> FakeGeminiResponse:
        self.calls += 1
        return self.responses[min(self.calls - 1, len(self.responses) - 1)]


class FakeAioClient:
    def __init__(self, response: FakeGeminiResponse | list[FakeGeminiResponse]) -> None:
        self.models = FakeAsyncModels(response)


class FakeAsyncClient:
    def __init__(self, response: FakeGeminiResponse | list[FakeGeminiResponse]) -> None:
        self.aio = FakeAioClient(response)


def _hosted_context(
    ledger: RecordingLedger,
    *,
    operation: str,
    estimated_units: dict[str, float] | None = None,
    balance_units: dict[str, float] | None = None,
    policy: EntitlementPolicy | None = None,
) -> ProviderCallContext:
    return ProviderCallContext(
        gate=UsageGate(),
        ledger=ledger,
        workspace_id="ws_test",
        subject="gemini",
        operation=operation,
        estimated_units=estimated_units or {"total_tokens": 100},
        allocation=ProviderAllocation(
            id=f"alloc_{operation}",
            workspace_id="ws_test",
            provider="gemini",
            operation=operation,
            model_or_plan="gemini-test",
        ),
        policy=policy
        or EntitlementPolicy(
            id="policy",
            workspace_id="ws_test",
            allowed_operations={f"gemini.{operation}"},
        ),
        balance=WorkspaceBalance(
            workspace_id="ws_test",
            remaining_units=balance_units or {"total_tokens": 10_000},
        ),
        idempotency_key=f"ws_test:video_1:gemini.{operation}:h_test",
        metadata={"job_id": "job_test"},
    )


def test_transcribe_window_records_hosted_usage_from_gemini_response() -> None:
    ledger = RecordingLedger()
    response = FakeGeminiResponse(
        text='{"segments":[{"start":1.0,"duration":2.0,"text":"hello world"}]}',
        usage_metadata={
            "prompt_token_count": 11,
            "candidates_token_count": 7,
            "total_token_count": 18,
        },
    )
    client = FakeSyncClient(response)

    segments = _transcribe_gemini_window(
        client=client,
        types=FakeTypes,
        video_url="https://www.youtube.com/watch?v=video_1",
        config=GeminiConfig(model="gemini-test"),
        window_start=0,
        window_end=None,
        hosted_context=_hosted_context(ledger, operation="transcribe_media"),
    )

    assert client.models.calls == 1
    assert segments == [{"text": "hello world", "start": 1.0, "duration": 2.0}]
    assert [event.status for event in ledger.events] == ["started", "succeeded"]
    assert ledger.events[1].operation == "transcribe_media"
    assert ledger.events[1].actual_units["prompt_tokens"] == 11
    assert ledger.events[1].actual_units["candidate_tokens"] == 7
    assert ledger.events[1].actual_units["total_tokens"] == 18
    assert ledger.events[1].provider_request_id == "resp_1"
    assert ledger.events[1].metadata["job_id"] == "job_test"


def test_denied_transcribe_reservation_prevents_gemini_call() -> None:
    ledger = RecordingLedger()
    client = FakeSyncClient(
        FakeGeminiResponse(
            text='{"segments":[]}',
            usage_metadata={"total_token_count": 0},
        )
    )
    context = _hosted_context(
        ledger,
        operation="transcribe_media",
        estimated_units={"total_tokens": 600},
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_test",
            allowed_operations={"gemini.transcribe_media"},
            hard_limits_by_operation={"gemini.transcribe_media": {"total_tokens": 500}},
        ),
    )

    with pytest.raises(UsageReservationDenied):
        _transcribe_gemini_window(
            client=client,
            types=FakeTypes,
            video_url="https://www.youtube.com/watch?v=video_1",
            config=GeminiConfig(model="gemini-test"),
            window_start=0,
            window_end=None,
            hosted_context=context,
        )

    assert client.models.calls == 0
    assert len(ledger.events) == 1
    assert ledger.events[0].status == "denied"
    assert ledger.events[0].error_code == "usage_limit_exceeded"


def test_transcribe_windows_derive_distinct_hosted_idempotency_values() -> None:
    ledger = RecordingLedger()
    response = FakeGeminiResponse(
        text='{"segments":[{"start":1.0,"duration":2.0,"text":"hello world"}]}',
        usage_metadata={"total_token_count": 18},
    )
    client = FakeSyncClient(response)
    context = _hosted_context(
        ledger,
        operation="transcribe_media",
        estimated_units={"media_seconds": 120},
        balance_units={"media_seconds": 120},
    )

    for window_index, (window_start, window_end) in enumerate([(0, 60), (60, 120)]):
        _transcribe_gemini_window(
            client=client,
            types=FakeTypes,
            video_url="https://www.youtube.com/watch?v=video_1",
            config=GeminiConfig(model="gemini-test"),
            window_start=window_start,
            window_end=window_end,
            hosted_context=context,
            window_index=window_index,
            window_count=2,
        )

    assert client.models.calls == 2
    started_events = [event for event in ledger.events if event.status == "started"]
    idempotency_keys = [event.metadata["idempotency_key"] for event in started_events]
    assert idempotency_keys == [
        "ws_test:video_1:gemini.transcribe_media:h_test:window:0:0:60",
        "ws_test:video_1:gemini.transcribe_media:h_test:window:1:60:120",
    ]
    assert [event.metadata["estimated_units"] for event in started_events] == [
        {"media_seconds": 60.0},
        {"media_seconds": 60.0},
    ]
    assert [event.metadata["window_index"] for event in started_events] == [0, 1]
    assert all(event.metadata["parent_idempotency_key"] == context.idempotency_key for event in started_events)


def test_cleanup_batch_records_hosted_usage_from_async_gemini_response() -> None:
    ledger = RecordingLedger()
    response = FakeGeminiResponse(
        text='{"corrections":[{"sequence":1,"text":"I use Gemini daily."}]}',
        usage_metadata={
            "promptTokenCount": 33,
            "candidatesTokenCount": 9,
            "totalTokenCount": 42,
        },
    )
    client = FakeAsyncClient(response)
    batch = [
        TranscriptSegment(
            segment_id="seg_1",
            sequence=1,
            start_ms=0,
            end_ms=1000,
            text="I use Gemeni daily.",
        )
    ]

    result = asyncio.run(
        _cleanup_batch_async(
            client=client,
            types=FakeTypes,
            batch=batch,
            config=GeminiConfig(model="gemini-test"),
            context=None,
            cache_name=None,
            max_change_ratio=0.5,
            max_patch_retries=0,
            hosted_context=_hosted_context(ledger, operation="cleanup_transcript"),
        )
    )

    assert client.aio.models.calls == 1
    assert result.corrections[0].text == "I use Gemini daily."
    assert [event.status for event in ledger.events] == ["started", "succeeded"]
    assert ledger.events[1].operation == "cleanup_transcript"
    assert ledger.events[1].actual_units["prompt_tokens"] == 33
    assert ledger.events[1].actual_units["candidate_tokens"] == 9
    assert ledger.events[1].actual_units["total_tokens"] == 42


def test_cleanup_batches_derive_distinct_batch_hosted_idempotency_values() -> None:
    ledger = RecordingLedger()
    client = FakeAsyncClient(
        FakeGeminiResponse(
            text='{"corrections":[]}',
            usage_metadata={"totalTokenCount": 42},
        )
    )
    context = _hosted_context(ledger, operation="cleanup_transcript")
    batches = [
        [
            TranscriptSegment(
                segment_id="seg_1",
                sequence=1,
                start_ms=0,
                end_ms=1000,
                text="First batch.",
            )
        ],
        [
            TranscriptSegment(
                segment_id="seg_2",
                sequence=2,
                start_ms=1000,
                end_ms=2000,
                text="Second batch.",
            )
        ],
    ]

    for batch_index, batch in enumerate(batches):
        asyncio.run(
            _cleanup_batch_async(
                client=client,
                types=FakeTypes,
                batch=batch,
                config=GeminiConfig(model="gemini-test"),
                context=None,
                cache_name=None,
                max_change_ratio=0.5,
                max_patch_retries=0,
                hosted_context=context,
                batch_index=batch_index,
                batch_count=2,
            )
        )

    started_events = [event for event in ledger.events if event.status == "started"]
    assert [event.metadata["idempotency_key"] for event in started_events] == [
        "ws_test:video_1:gemini.cleanup_transcript:h_test:batch:0:seq:1-1:attempt:0",
        "ws_test:video_1:gemini.cleanup_transcript:h_test:batch:1:seq:2-2:attempt:0",
    ]
    assert [event.metadata["batch_index"] for event in started_events] == [0, 1]
    assert [event.metadata["batch_count"] for event in started_events] == [2, 2]


def test_cleanup_validation_retries_derive_distinct_attempt_idempotency_values() -> None:
    ledger = RecordingLedger()
    client = FakeAsyncClient(
        [
            FakeGeminiResponse(
                text='{"corrections":[{"sequence":99,"text":"wrong segment"}]}',
                usage_metadata={"totalTokenCount": 21},
            ),
            FakeGeminiResponse(
                text='{"corrections":[{"sequence":1,"text":"I use Gemini daily."}]}',
                usage_metadata={"totalTokenCount": 42},
            ),
        ]
    )
    batch = [
        TranscriptSegment(
            segment_id="seg_1",
            sequence=1,
            start_ms=0,
            end_ms=1000,
            text="I use Gemeni daily.",
        )
    ]

    result = asyncio.run(
        _cleanup_batch_async(
            client=client,
            types=FakeTypes,
            batch=batch,
            config=GeminiConfig(model="gemini-test"),
            context=None,
            cache_name=None,
            max_change_ratio=0.5,
            max_patch_retries=1,
            hosted_context=_hosted_context(ledger, operation="cleanup_transcript"),
            batch_index=0,
            batch_count=1,
        )
    )

    assert result.corrections[0].text == "I use Gemini daily."
    assert client.aio.models.calls == 2
    started_events = [event for event in ledger.events if event.status == "started"]
    assert [event.metadata["idempotency_key"] for event in started_events] == [
        "ws_test:video_1:gemini.cleanup_transcript:h_test:batch:0:seq:1-1:attempt:0",
        "ws_test:video_1:gemini.cleanup_transcript:h_test:batch:0:seq:1-1:attempt:1",
    ]
