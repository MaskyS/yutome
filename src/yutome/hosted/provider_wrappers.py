from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from yutome.hosted.errors import ProviderFailure, classify_provider_http_error
from yutome.hosted.events import denied_usage_event, usage_event_from_normalization
from yutome.hosted.gate import Allocation, UsageGate
from yutome.hosted.models import (
    EntitlementPolicy,
    UnitQuantity,
    UsageEvent,
    UsageNormalization,
    UsageReservation,
    UsageSubject,
    WorkspaceBalance,
)


T = TypeVar("T")


class UsageLedgerWriter(Protocol):
    def append(self, event: UsageEvent) -> None:
        ...


UsageNormalizer = Callable[[T], UsageNormalization]


@dataclass(frozen=True)
class ProviderCallContext:
    gate: UsageGate
    ledger: UsageLedgerWriter
    workspace_id: str
    subject: UsageSubject
    operation: str
    estimated_units: Mapping[str, UnitQuantity]
    allocation: Allocation | None
    policy: EntitlementPolicy
    balance: WorkspaceBalance
    idempotency_key: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


class UsageReservationDenied(RuntimeError):
    def __init__(self, reservation: UsageReservation, event: UsageEvent) -> None:
        self.reservation = reservation
        self.event = event
        message = reservation.decision.message or reservation.decision.reason
        super().__init__(message)


class HostedProviderWrapper:
    def __init__(self, context: ProviderCallContext) -> None:
        self.context = context

    def run(self, call: Callable[[], T], *, normalize_usage: UsageNormalizer[T] | None = None) -> T:
        reservation = self._reserve()
        if not reservation.decision.allowed:
            event = _with_context_metadata(denied_usage_event(reservation), self.context, reservation)
            self.context.ledger.append(event)
            raise UsageReservationDenied(reservation, event)

        self.context.ledger.append(_started_event(self.context, reservation))
        try:
            result = call()
        except Exception as exc:
            failure = classify_provider_exception(provider=self.context.subject, exc=exc)
            self.context.ledger.append(_failed_event(self.context, reservation, failure, exc))
            raise

        normalization = _normalize_success(self.context, result, normalize_usage)
        event = usage_event_from_normalization(
            normalization,
            reservation=reservation,
            event_type="provider_attempt_succeeded",
        )
        self.context.ledger.append(_with_context_metadata(event, self.context, reservation))
        return result

    async def arun(
        self,
        call: Callable[[], Awaitable[T]],
        *,
        normalize_usage: UsageNormalizer[T] | None = None,
    ) -> T:
        reservation = self._reserve()
        if not reservation.decision.allowed:
            event = _with_context_metadata(denied_usage_event(reservation), self.context, reservation)
            self.context.ledger.append(event)
            raise UsageReservationDenied(reservation, event)

        self.context.ledger.append(_started_event(self.context, reservation))
        try:
            result = await call()
        except Exception as exc:
            failure = classify_provider_exception(provider=self.context.subject, exc=exc)
            self.context.ledger.append(_failed_event(self.context, reservation, failure, exc))
            raise

        normalization = _normalize_success(self.context, result, normalize_usage)
        event = usage_event_from_normalization(
            normalization,
            reservation=reservation,
            event_type="provider_attempt_succeeded",
        )
        self.context.ledger.append(_with_context_metadata(event, self.context, reservation))
        return result

    def _reserve(self) -> UsageReservation:
        return self.context.gate.reserve(
            workspace_id=self.context.workspace_id,
            subject=self.context.subject,
            operation=self.context.operation,
            estimated_units=dict(self.context.estimated_units),
            allocation=self.context.allocation,
            policy=self.context.policy,
            balance=self.context.balance,
            idempotency_key=self.context.idempotency_key,
        )


def execute_provider_call(
    context: ProviderCallContext,
    call: Callable[[], T],
    *,
    normalize_usage: UsageNormalizer[T] | None = None,
) -> T:
    return HostedProviderWrapper(context).run(call, normalize_usage=normalize_usage)


async def execute_provider_call_async(
    context: ProviderCallContext,
    call: Callable[[], Awaitable[T]],
    *,
    normalize_usage: UsageNormalizer[T] | None = None,
) -> T:
    return await HostedProviderWrapper(context).arun(call, normalize_usage=normalize_usage)


def classify_provider_exception(*, provider: str, exc: BaseException) -> ProviderFailure:
    return classify_provider_http_error(
        provider=provider,
        status_code=_status_code_from_exception(exc),
        message=str(exc),
    )


def _normalize_success(
    context: ProviderCallContext,
    result: T,
    normalize_usage: UsageNormalizer[T] | None,
) -> UsageNormalization:
    if normalize_usage is not None:
        return _for_context(context, normalize_usage(result))
    if isinstance(result, UsageNormalization):
        return _for_context(context, result)
    return UsageNormalization(subject=context.subject, operation=context.operation)


def _for_context(context: ProviderCallContext, normalization: UsageNormalization) -> UsageNormalization:
    if normalization.subject == context.subject and normalization.operation == context.operation:
        return normalization
    return UsageNormalization(
        subject=context.subject,
        operation=context.operation,
        actual_units=normalization.actual_units,
        provider_request_id=normalization.provider_request_id,
        raw_usage=normalization.raw_usage,
        metadata=normalization.metadata,
    )


def _started_event(context: ProviderCallContext, reservation: UsageReservation) -> UsageEvent:
    event = UsageEvent(
        reservation_id=reservation.id,
        workspace_id=reservation.workspace_id,
        subject=reservation.subject,
        operation=reservation.operation,
        event_type="provider_attempt_started",
        status="started",
        metadata={"estimated_units": dict(reservation.estimated_units)},
    )
    return _with_context_metadata(event, context, reservation)


def _failed_event(
    context: ProviderCallContext,
    reservation: UsageReservation,
    failure: ProviderFailure,
    exc: BaseException,
) -> UsageEvent:
    event = UsageEvent(
        reservation_id=reservation.id,
        workspace_id=reservation.workspace_id,
        subject=reservation.subject,
        operation=reservation.operation,
        event_type="provider_attempt_failed",
        status="failed",
        error_code=failure.code,
        metadata={
            "failure_kind": failure.kind,
            "retryable": failure.retryable,
            "message": failure.message,
            "exception_type": type(exc).__name__,
        },
    )
    return _with_context_metadata(event, context, reservation)


def _with_context_metadata(
    event: UsageEvent,
    context: ProviderCallContext,
    reservation: UsageReservation,
) -> UsageEvent:
    event.metadata = {
        **event.metadata,
        "idempotency_key": reservation.idempotency_key,
        "allocation_id": reservation.allocation_id,
        **dict(context.metadata),
    }
    return event


def _status_code_from_exception(exc: BaseException) -> int | None:
    for name in ("status_code", "status", "code"):
        value = getattr(exc, name, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        for name in ("status_code", "status"):
            value = getattr(response, name, None)
            if isinstance(value, int):
                return value
    return None
