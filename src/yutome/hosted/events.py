from __future__ import annotations

from yutome.hosted.models import EventStatus, UsageEvent, UsageNormalization, UsageReservation


def usage_event_from_normalization(
    normalization: UsageNormalization,
    *,
    reservation: UsageReservation,
    event_type: str,
    status: EventStatus = "succeeded",
    error_code: str | None = None,
) -> UsageEvent:
    return UsageEvent(
        reservation_id=reservation.id,
        workspace_id=reservation.workspace_id,
        subject=normalization.subject,
        operation=normalization.operation,
        event_type=event_type,
        status=status,
        actual_units=normalization.actual_units,
        provider_request_id=normalization.provider_request_id,
        error_code=error_code,
        raw_usage=normalization.raw_usage,
        metadata=normalization.metadata,
    )


def denied_usage_event(reservation: UsageReservation) -> UsageEvent:
    return UsageEvent(
        reservation_id=reservation.id,
        workspace_id=reservation.workspace_id,
        subject=reservation.subject,
        operation=reservation.operation,
        event_type="reservation_created",
        status="denied",
        actual_units={},
        error_code=reservation.decision.reason,
        metadata={"message": reservation.decision.message},
    )
