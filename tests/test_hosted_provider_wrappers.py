from __future__ import annotations

from dataclasses import replace

import pytest

from yutome.hosted.gate import UsageGate
from yutome.hosted.ledger import PostgresUsageGate, PostgresUsageLedger
from yutome.hosted.models import (
    EntitlementPolicy,
    ProviderAllocation,
    UsageEvent,
    UsageNormalization,
    WorkspaceBalance,
)
from yutome.hosted.provider_wrappers import (
    ProviderCallContext,
    UsageReservationDenied,
    execute_provider_call,
)


class RecordingLedger:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    def append(self, event: UsageEvent) -> None:
        self.events.append(event)


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, statement: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        self.calls.append((statement, dict(params or {})))
        return []


class FakeProviderError(RuntimeError):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


def _context(
    ledger: RecordingLedger,
    *,
    estimated_units: dict[str, float] | None = None,
    balance_units: dict[str, float] | None = None,
    policy: EntitlementPolicy | None = None,
) -> ProviderCallContext:
    operation = "cleanup_transcript"
    return ProviderCallContext(
        gate=UsageGate(),
        ledger=ledger,
        workspace_id="ws_alice",
        subject="gemini",
        operation=operation,
        estimated_units=estimated_units or {"total_tokens": 100},
        allocation=ProviderAllocation(
            id="alloc_gemini",
            workspace_id="ws_alice",
            provider="gemini",
            operation=operation,
        ),
        policy=policy
        or EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={f"gemini.{operation}"},
        ),
        balance=WorkspaceBalance(
            workspace_id="ws_alice",
            remaining_units={"total_tokens": 500} if balance_units is None else balance_units,
        ),
        idempotency_key="ws_alice:vid_123:gemini.cleanup_transcript:h_fake",
        metadata={"job_id": "job_1"},
    )


def test_provider_wrapper_records_started_and_succeeded_events() -> None:
    ledger = RecordingLedger()
    response = {"request_id": "resp_1", "tokens": 91}

    def call() -> dict[str, int | str]:
        return response

    def normalize(result: dict[str, int | str]) -> UsageNormalization:
        return UsageNormalization(
            subject="gemini",
            operation="cleanup_transcript",
            actual_units={"total_tokens": result["tokens"]},
            provider_request_id=str(result["request_id"]),
            raw_usage={"provider_payload": result},
        )

    result = execute_provider_call(_context(ledger), call, normalize_usage=normalize)

    assert result is response
    assert [event.status for event in ledger.events] == ["started", "succeeded"]
    assert [event.event_type for event in ledger.events] == [
        "provider_attempt_started",
        "provider_attempt_succeeded",
    ]
    assert ledger.events[0].reservation_id == ledger.events[1].reservation_id
    assert ledger.events[0].metadata["estimated_units"] == {"total_tokens": 100}
    assert ledger.events[1].actual_units["total_tokens"] == 91
    assert ledger.events[1].provider_request_id == "resp_1"
    assert ledger.events[1].metadata["job_id"] == "job_1"


def test_provider_wrapper_denies_before_calling_provider() -> None:
    ledger = RecordingLedger()
    called = False

    def call() -> object:
        nonlocal called
        called = True
        return object()

    context = _context(
        ledger,
        estimated_units={"total_tokens": 600},
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={"gemini.cleanup_transcript"},
            max_units_by_operation={"gemini.cleanup_transcript": {"total_tokens": 500}},
        ),
    )

    with pytest.raises(UsageReservationDenied) as exc_info:
        execute_provider_call(context, call)

    assert called is False
    assert len(ledger.events) == 1
    assert ledger.events[0].status == "denied"
    assert ledger.events[0].event_type == "reservation_created"
    assert ledger.events[0].error_code == "usage_limit_exceeded"
    assert exc_info.value.reservation.status == "denied"


def test_provider_wrapper_denies_missing_policy_before_calling_provider() -> None:
    ledger = RecordingLedger()
    called = False

    def call() -> object:
        nonlocal called
        called = True
        return object()

    context = _context(
        ledger,
        policy=EntitlementPolicy(id="policy", workspace_id="ws_alice"),
    )

    with pytest.raises(UsageReservationDenied) as exc_info:
        execute_provider_call(context, call)

    assert called is False
    assert len(ledger.events) == 1
    assert ledger.events[0].status == "denied"
    assert ledger.events[0].event_type == "reservation_created"
    assert ledger.events[0].error_code == "operation_not_allowed"
    assert exc_info.value.reservation.decision.reason == "operation_not_allowed"


def test_provider_wrapper_records_retryable_failed_event() -> None:
    ledger = RecordingLedger()

    def call() -> object:
        raise FakeProviderError(503, "Service Unavailable")

    with pytest.raises(FakeProviderError):
        execute_provider_call(_context(ledger), call)

    assert [event.status for event in ledger.events] == ["started", "failed"]
    failed = ledger.events[1]
    assert failed.event_type == "provider_attempt_failed"
    assert failed.error_code == "http_503"
    assert failed.metadata["failure_kind"] == "transient"
    assert failed.metadata["retryable"] is True
    assert failed.metadata["exception_type"] == "FakeProviderError"


def test_provider_wrapper_redacts_sensitive_failure_metadata() -> None:
    ledger = RecordingLedger()

    def call() -> object:
        raise FakeProviderError(
            407,
            "Proxy failed http://webshare-user:SuperSecretPass@proxy.example:80 "
            "api_key=pa-1234567890abcdef token=sk-providersecret12345 "
            "Authorization: Bearer eyJsecretsecret.abc123456.def789012",
        )

    with pytest.raises(FakeProviderError):
        execute_provider_call(_context(ledger), call)

    failed = ledger.events[1]
    message = failed.metadata["message"]
    assert "SuperSecretPass" not in message
    assert "pa-1234567890abcdef" not in message
    assert "sk-providersecret12345" not in message
    assert "eyJsecretsecret" not in message
    assert "http://***:***@proxy.example:80" in message
    assert "api_key=***" in message
    assert "token=***" in message
    assert "Bearer ***" in message


def test_provider_wrapper_can_use_durable_postgres_usage_adapters_without_double_charge() -> None:
    connection = RecordingConnection()
    response = {"request_id": "resp_1", "tokens": 91}

    def call() -> dict[str, int | str]:
        return response

    def normalize(result: dict[str, int | str]) -> UsageNormalization:
        return UsageNormalization(
            subject="gemini",
            operation="cleanup_transcript",
            actual_units={"total_tokens": result["tokens"]},
            provider_request_id=str(result["request_id"]),
            raw_usage={"provider_payload": result},
        )

    for _ in range(2):
        context = _context(PostgresUsageLedger(connection))
        context = replace(context, gate=PostgresUsageGate(connection))
        assert execute_provider_call(context, call, normalize_usage=normalize) is response

    reservation_inserts = [params for sql, params in connection.calls if "INSERT INTO usage_reservations" in sql]
    event_inserts = [params for sql, params in connection.calls if "INSERT INTO usage_events" in sql]
    started_events = [params for params in event_inserts if params["event_type"] == "provider_attempt_started"]
    succeeded_events = [params for params in event_inserts if params["event_type"] == "provider_attempt_succeeded"]

    assert len(reservation_inserts) == 2
    assert len({params["id"] for params in reservation_inserts}) == 1
    assert len(started_events) == 2
    assert len({params["id"] for params in started_events}) == 1
    assert len(succeeded_events) == 2
    assert len({params["id"] for params in succeeded_events}) == 1
    assert all(params["provider_request_id"] == "resp_1" for params in succeeded_events)
