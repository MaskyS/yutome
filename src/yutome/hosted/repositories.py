from __future__ import annotations

import json
from datetime import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from yutome.hosted.models import ReservationStatus, UsageDecision, UsageEvent, UsageReservation


JsonParam = str
SqlParams = dict[str, Any]
UsageEventIdempotency = Literal["event_id", "provider_request"]


@dataclass(frozen=True)
class SqlStatement:
    """Parameterized SQL plus DB-API style named parameters."""

    sql: str
    params: SqlParams


USAGE_RESERVATION_IDEMPOTENCY_CONSTRAINT_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_reservations_workspace_idempotency_key
    ON usage_reservations(workspace_id, idempotency_key);
""".strip()

USAGE_EVENT_PROVIDER_REQUEST_IDEMPOTENCY_CONSTRAINT_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_events_provider_request_idempotency
    ON usage_events(workspace_id, subject, operation, event_type, provider_request_id)
    WHERE provider_request_id IS NOT NULL;
""".strip()


def usage_repository_constraint_statements() -> list[str]:
    """Constraints needed before using idempotent insert helpers.

    Phase 1 already defines the reservation uniqueness in the table shape. This
    explicit index keeps the repository contract visible for future migrations
    and adds a provider-request event de-duplication surface.
    """

    return [
        USAGE_RESERVATION_IDEMPOTENCY_CONSTRAINT_SQL,
        USAGE_EVENT_PROVIDER_REQUEST_IDEMPOTENCY_CONSTRAINT_SQL,
    ]


def upsert_usage_reservation_sql(reservation: UsageReservation) -> SqlStatement:
    """Insert a usage reservation or return the existing idempotent row.

    The repository boundary persists the UsageGate result as-is. A repeated
    idempotency key does not overwrite the original decision or estimated units.
    """

    return SqlStatement(
        sql="""
INSERT INTO usage_reservations (
    id,
    workspace_id,
    subject,
    operation,
    allocation_id,
    allocation_kind,
    estimated_units_json,
    idempotency_key,
    status,
    decision_json,
    metadata_json,
    created_at
)
VALUES (
    %(id)s,
    %(workspace_id)s,
    %(subject)s,
    %(operation)s,
    %(allocation_id)s,
    %(allocation_kind)s,
    %(estimated_units_json)s::jsonb,
    %(idempotency_key)s,
    %(status)s,
    %(decision_json)s::jsonb,
    %(metadata_json)s::jsonb,
    %(created_at)s
)
ON CONFLICT (workspace_id, idempotency_key) DO UPDATE
SET idempotency_key = usage_reservations.idempotency_key
RETURNING *;
""".strip(),
        params=usage_reservation_params(reservation),
    )


def usage_reservation_params(reservation: UsageReservation) -> SqlParams:
    return {
        "id": reservation.id,
        "workspace_id": reservation.workspace_id,
        "subject": reservation.subject,
        "operation": reservation.operation,
        "allocation_id": reservation.allocation_id,
        "allocation_kind": reservation.allocation_kind,
        "estimated_units_json": _json_param(reservation.estimated_units),
        "idempotency_key": reservation.idempotency_key,
        "status": reservation.status,
        "decision_json": _json_param(reservation.decision.model_dump(mode="json")),
        "metadata_json": _json_param(reservation.metadata),
        "created_at": reservation.created_at,
    }


def update_usage_reservation_status_sql(
    *,
    reservation_id: str,
    workspace_id: str,
    status: ReservationStatus,
) -> SqlStatement:
    return SqlStatement(
        sql="""
UPDATE usage_reservations
SET status = %(status)s
WHERE id = %(reservation_id)s
  AND workspace_id = %(workspace_id)s
RETURNING *;
""".strip(),
        params={
            "reservation_id": reservation_id,
            "workspace_id": workspace_id,
            "status": status,
        },
    )


def insert_usage_event_sql(
    event: UsageEvent,
    *,
    idempotency: UsageEventIdempotency = "event_id",
) -> SqlStatement:
    if idempotency == "event_id":
        conflict_sql = """
ON CONFLICT (id) DO UPDATE
SET id = usage_events.id
""".strip()
    else:
        conflict_sql = """
ON CONFLICT (workspace_id, subject, operation, event_type, provider_request_id)
WHERE provider_request_id IS NOT NULL
DO UPDATE SET provider_request_id = usage_events.provider_request_id
""".strip()

    return SqlStatement(
        sql=f"""
INSERT INTO usage_events (
    id,
    reservation_id,
    workspace_id,
    subject,
    operation,
    event_type,
    status,
    actual_units_json,
    provider_request_id,
    error_code,
    raw_usage_json,
    metadata_json,
    created_at
)
VALUES (
    %(id)s,
    %(reservation_id)s,
    %(workspace_id)s,
    %(subject)s,
    %(operation)s,
    %(event_type)s,
    %(status)s,
    %(actual_units_json)s::jsonb,
    %(provider_request_id)s,
    %(error_code)s,
    %(raw_usage_json)s::jsonb,
    %(metadata_json)s::jsonb,
    %(created_at)s
)
{conflict_sql}
RETURNING *;
""".strip(),
        params=usage_event_params(event),
    )


def usage_event_params(event: UsageEvent) -> SqlParams:
    return {
        "id": event.id,
        "reservation_id": event.reservation_id,
        "workspace_id": event.workspace_id,
        "subject": event.subject,
        "operation": event.operation,
        "event_type": event.event_type,
        "status": event.status,
        "actual_units_json": _json_param(event.actual_units),
        "provider_request_id": event.provider_request_id,
        "error_code": event.error_code,
        "raw_usage_json": _json_param(event.raw_usage),
        "metadata_json": _json_param(event.metadata),
        "created_at": event.created_at,
    }


def usage_reservation_from_row(row: MappingRow) -> UsageReservation:
    return UsageReservation(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        subject=row["subject"],
        operation=str(row["operation"]),
        allocation_id=_optional_str(row.get("allocation_id")),
        allocation_kind=row["allocation_kind"],
        estimated_units=dict(_json_value(row.get("estimated_units_json"))),
        idempotency_key=str(row["idempotency_key"]),
        status=row["status"],
        decision=UsageDecision.model_validate(_json_value(row.get("decision_json"))),
        metadata=dict(_json_value(row.get("metadata_json"))),
        created_at=_datetime_value(row["created_at"]),
    )


def usage_event_from_row(row: MappingRow) -> UsageEvent:
    return UsageEvent(
        id=str(row["id"]),
        reservation_id=_optional_str(row.get("reservation_id")),
        workspace_id=str(row["workspace_id"]),
        subject=row["subject"],
        operation=str(row["operation"]),
        event_type=str(row["event_type"]),
        status=row["status"],
        actual_units=dict(_json_value(row.get("actual_units_json"))),
        provider_request_id=_optional_str(row.get("provider_request_id")),
        error_code=_optional_str(row.get("error_code")),
        raw_usage=dict(_json_value(row.get("raw_usage_json"))),
        metadata=dict(_json_value(row.get("metadata_json"))),
        created_at=_datetime_value(row["created_at"]),
    )


MappingRow = dict[str, Any]


def _json_param(value: Any) -> JsonParam:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_value(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, bytes):
        return json.loads(value.decode("utf-8"))
    return value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _datetime_value(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise TypeError(f"Expected datetime-compatible value, got {type(value).__name__}.")


__all__: Sequence[str] = [
    "MappingRow",
    "SqlStatement",
    "USAGE_EVENT_PROVIDER_REQUEST_IDEMPOTENCY_CONSTRAINT_SQL",
    "USAGE_RESERVATION_IDEMPOTENCY_CONSTRAINT_SQL",
    "insert_usage_event_sql",
    "update_usage_reservation_status_sql",
    "upsert_usage_reservation_sql",
    "usage_event_params",
    "usage_event_from_row",
    "usage_repository_constraint_statements",
    "usage_reservation_from_row",
    "usage_reservation_params",
]
