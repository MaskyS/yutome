from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from yutome.hosted.gate import Allocation, UsageGate
from yutome.hosted.ids import input_hash
from yutome.hosted.models import (
    EntitlementPolicy,
    UnitMap,
    UnitQuantity,
    UsageEvent,
    UsageReservation,
    WorkspaceBalance,
    add_unit_maps,
    jsonable_exact,
    normalize_unit_map,
    subtract_unit_maps,
    unit_quantity_decimal,
)
from yutome.hosted.repositories import (
    SqlStatement,
    insert_usage_event_sql,
    update_usage_reservation_status_sql,
    upsert_usage_reservation_sql,
    usage_event_from_row,
    usage_reservation_from_row,
)


class PostgresUsageGate:
    """UsageGate adapter that durably reserves balance before provider calls."""

    def __init__(self, connection: Any, *, gate: UsageGate | None = None) -> None:
        self.connection = connection
        self.gate = gate or UsageGate()

    def reserve(
        self,
        *,
        workspace_id: str,
        subject: str,
        operation: str,
        estimated_units: dict[str, UnitQuantity],
        allocation: Allocation | None,
        policy: EntitlementPolicy,
        balance: WorkspaceBalance,
        idempotency_key: str,
    ) -> UsageReservation:
        with _transaction(self.connection):
            balance_row = _lock_active_balance_row(
                self.connection,
                workspace_id=workspace_id,
                entitlement_policy_id=policy.id,
            )
            existing = _lock_existing_reservation_row(
                self.connection,
                workspace_id=workspace_id,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                return usage_reservation_from_row(existing)

            locked_balance = _workspace_balance_from_row(balance_row) if balance_row is not None else WorkspaceBalance(workspace_id=workspace_id)
            reservation = self.gate.reserve(
                workspace_id=workspace_id,
                subject=subject,
                operation=operation,
                estimated_units=estimated_units,
                allocation=allocation,
                policy=policy,
                balance=locked_balance,
                idempotency_key=idempotency_key,
            )
            durable = stable_usage_reservation(reservation)
            row = _execute_one(self.connection, upsert_usage_reservation_sql(durable))
            persisted = usage_reservation_from_row(row) if row else durable
            if persisted.decision.allowed and balance_row is not None:
                remaining_units, reserved_units = _balance_after_reservation(balance_row, persisted)
                _execute_one(
                    self.connection,
                    SqlStatement(
                        sql=_UPDATE_WORKSPACE_BALANCE_RESERVATION_SQL,
                        params={
                            "workspace_id": workspace_id,
                            "entitlement_policy_id": policy.id,
                            "remaining_units_jsonb": _json_param(remaining_units),
                            "reserved_units_jsonb": _json_param(reserved_units),
                        },
                    ),
                )
            return persisted


@dataclass(frozen=True)
class UsageReservationReconciliation:
    id: str
    workspace_id: str
    reservation_id: str
    usage_event_id: str
    estimated_units: UnitMap
    actual_units: UnitMap
    released_units: UnitMap
    overage_units: UnitMap


class PostgresUsageLedger:
    """Append usage events to hosted Postgres with retry-safe event IDs."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def append(self, event: UsageEvent) -> UsageEvent:
        durable = stable_usage_event(event)
        idempotency = "provider_request" if durable.provider_request_id else "event_id"
        with _transaction(self.connection):
            row = _execute_one(self.connection, insert_usage_event_sql(durable, idempotency=idempotency))
            persisted = usage_event_from_row(row) if row else durable
            _reconcile_balance_for_usage_event(self.connection, persisted)
            return persisted

    def recent(self, *, workspace_id: str, limit: int = 20) -> list[UsageEvent]:
        rows = _execute_rows(
            self.connection,
            SqlStatement(
                sql="""
SELECT *
FROM usage_events
WHERE workspace_id = %(workspace_id)s
ORDER BY created_at DESC, id DESC
LIMIT %(limit)s;
""".strip(),
                params={"workspace_id": workspace_id, "limit": max(0, limit)},
            ),
        )
        return [usage_event_from_row(row) for row in reversed(rows)]


def stable_usage_reservation(reservation: UsageReservation) -> UsageReservation:
    return reservation.model_copy(
        update={
            "id": stable_usage_reservation_id(
                workspace_id=reservation.workspace_id,
                idempotency_key=reservation.idempotency_key,
            ),
        }
    )


def stable_usage_reservation_id(*, workspace_id: str, idempotency_key: str) -> str:
    return _stable_id("res", workspace_id, idempotency_key)


def stable_usage_event(event: UsageEvent) -> UsageEvent:
    """Return an event ID stable across retries of the same hosted operation."""

    metadata = dict(event.metadata)
    operation_key = metadata.get("idempotency_key") or event.reservation_id or event.id
    provider_request = event.provider_request_id or ""
    return event.model_copy(
        update={
            "id": _stable_id(
                "evt",
                event.workspace_id,
                event.subject,
                event.operation,
                event.event_type,
                event.status,
                str(operation_key),
                provider_request,
            )
        }
    )


def reconcile_reservation_usage(
    reservation: UsageReservation,
    event: UsageEvent,
) -> UsageReservationReconciliation:
    if event.reservation_id != reservation.id:
        raise ValueError("Usage event does not belong to the reservation being reconciled.")
    if event.workspace_id != reservation.workspace_id:
        raise ValueError("Usage event workspace does not match reservation workspace.")

    estimated = normalize_unit_map(reservation.estimated_units)
    actual = _numeric_actual_units(event)
    released: UnitMap = {}
    overage: UnitMap = {}
    for unit in sorted(set(estimated) | set(actual)):
        estimate_quantity = unit_quantity_decimal(estimated.get(unit, 0))
        actual_quantity = unit_quantity_decimal(actual.get(unit, 0))
        delta = estimate_quantity - actual_quantity
        if delta > 0:
            released[unit] = delta
        elif delta < 0:
            overage[unit] = -delta

    return UsageReservationReconciliation(
        id=input_hash(
            {
                "workspace_id": reservation.workspace_id,
                "reservation_id": reservation.id,
                "usage_event_id": event.id,
            },
            prefix="recon",
        ),
        workspace_id=reservation.workspace_id,
        reservation_id=reservation.id,
        usage_event_id=event.id,
        estimated_units=estimated,
        actual_units=actual,
        released_units=released,
        overage_units=overage,
    )


def _stable_id(prefix: str, *parts: str) -> str:
    return input_hash({"parts": parts}, prefix=prefix)


def _numeric_actual_units(event: UsageEvent) -> UnitMap:
    numeric: UnitMap = {}
    for unit, quantity in event.actual_units.items():
        if isinstance(quantity, bool) or quantity is None or isinstance(quantity, str):
            continue
        if unit_quantity_decimal(quantity) < 0:
            raise ValueError("Actual usage units must be non-negative; credits and releases use explicit ledger entries.")
        numeric[unit] = quantity
    return numeric


def _reconcile_balance_for_usage_event(connection: Any, event: UsageEvent) -> None:
    if event.reservation_id is None or event.status not in {"succeeded", "failed", "released"}:
        return
    row = _lock_reservation_by_id(connection, workspace_id=event.workspace_id, reservation_id=event.reservation_id)
    if row is None or str(row.get("status")) != "reserved":
        return
    reservation = usage_reservation_from_row(row)
    balance_row = _lock_current_workspace_balance_row(connection, workspace_id=event.workspace_id)
    if balance_row is not None:
        remaining_units, reserved_units = _balance_after_usage_event(balance_row, reservation, event)
        _execute_one(
            connection,
            SqlStatement(
                sql=_UPDATE_CURRENT_WORKSPACE_BALANCE_SQL,
                params={
                    "workspace_id": event.workspace_id,
                    "remaining_units_jsonb": _json_param(remaining_units),
                    "reserved_units_jsonb": _json_param(reserved_units),
                },
            ),
        )
    next_status = "reconciled" if event.status == "succeeded" else "released"
    _execute_one(
        connection,
        update_usage_reservation_status_sql(
            reservation_id=reservation.id,
            workspace_id=reservation.workspace_id,
            status=next_status,
        ),
    )


def _lock_reservation_by_id(connection: Any, *, workspace_id: str, reservation_id: str) -> Mapping[str, Any] | None:
    return _execute_one(
        connection,
        SqlStatement(
            sql=_LOCK_USAGE_RESERVATION_BY_ID_SQL,
            params={"workspace_id": workspace_id, "reservation_id": reservation_id},
        ),
    )


def _lock_active_balance_row(connection: Any, *, workspace_id: str, entitlement_policy_id: str) -> Mapping[str, Any] | None:
    return _execute_one(
        connection,
        SqlStatement(
            sql=_LOCK_ACTIVE_WORKSPACE_BALANCE_SQL,
            params={"workspace_id": workspace_id, "entitlement_policy_id": entitlement_policy_id},
        ),
    )


def _lock_existing_reservation_row(connection: Any, *, workspace_id: str, idempotency_key: str) -> Mapping[str, Any] | None:
    return _execute_one(
        connection,
        SqlStatement(
            sql=_LOCK_EXISTING_USAGE_RESERVATION_SQL,
            params={"workspace_id": workspace_id, "idempotency_key": idempotency_key},
        ),
    )


def _lock_current_workspace_balance_row(connection: Any, *, workspace_id: str) -> Mapping[str, Any] | None:
    return _execute_one(
        connection,
        SqlStatement(
            sql=_LOCK_CURRENT_WORKSPACE_BALANCE_SQL,
            params={"workspace_id": workspace_id},
        ),
    )


def _workspace_balance_from_row(row: Mapping[str, Any]) -> WorkspaceBalance:
    return WorkspaceBalance(
        workspace_id=str(row["workspace_id"]),
        remaining_units=_json_mapping(row.get("remaining_units_jsonb")),
        unlimited_units=set(_text_array(row.get("unlimited_units"))),
    )


def _balance_after_reservation(row: Mapping[str, Any], reservation: UsageReservation) -> tuple[UnitMap, UnitMap]:
    unlimited_units = set(_text_array(row.get("unlimited_units")))
    chargeable_estimate = {
        unit: quantity for unit, quantity in normalize_unit_map(reservation.estimated_units).items() if unit not in unlimited_units
    }
    remaining = normalize_unit_map(_json_mapping(row.get("remaining_units_jsonb")), allow_negative=True)
    reserved = normalize_unit_map(_json_mapping(row.get("reserved_units_jsonb")))
    return subtract_unit_maps(remaining, chargeable_estimate), add_unit_maps(reserved, chargeable_estimate)


def _balance_after_usage_event(
    row: Mapping[str, Any],
    reservation: UsageReservation,
    event: UsageEvent,
) -> tuple[UnitMap, UnitMap]:
    unlimited_units = set(_text_array(row.get("unlimited_units")))
    estimated = {
        unit: quantity for unit, quantity in normalize_unit_map(reservation.estimated_units).items() if unit not in unlimited_units
    }
    if event.status == "succeeded":
        actual = _numeric_actual_units(event) or estimated
        actual = {unit: quantity for unit, quantity in actual.items() if unit not in unlimited_units}
    else:
        actual = {}
    remaining = normalize_unit_map(_json_mapping(row.get("remaining_units_jsonb")), allow_negative=True)
    reserved = normalize_unit_map(_json_mapping(row.get("reserved_units_jsonb")))
    remaining_delta = subtract_unit_maps(estimated, actual)
    return add_unit_maps(remaining, remaining_delta), _clamp_non_negative(subtract_unit_maps(reserved, estimated))


def release_stale_unknown_usage_reservations(
    connection: Any,
    *,
    now: datetime | None = None,
    older_than_seconds: int = 3600,
    limit: int = 100,
) -> int:
    if older_than_seconds <= 0:
        raise ValueError("older_than_seconds must be positive.")
    if limit <= 0:
        raise ValueError("limit must be positive.")
    clock = now or datetime.now(timezone.utc)
    rows = _execute_rows(
        connection,
        SqlStatement(
            sql=_STALE_UNKNOWN_USAGE_RESERVATIONS_SQL,
            params={
                "now": clock,
                "older_than": clock - timedelta(seconds=older_than_seconds),
                "limit": limit,
            },
        ),
    )
    ledger = PostgresUsageLedger(connection)
    released = 0
    for row in rows:
        if "workspace_id" not in row or "subject" not in row or "idempotency_key" not in row:
            continue
        reservation = usage_reservation_from_row(row)
        event = UsageEvent(
            reservation_id=reservation.id,
            workspace_id=reservation.workspace_id,
            subject=reservation.subject,
            operation=reservation.operation,
            event_type="provider_attempt_released",
            status="released",
            error_code="unknown_timeout",
            metadata={
                "idempotency_key": reservation.idempotency_key,
                "unknown_usage_event_id": row.get("unknown_usage_event_id"),
                "estimated_units": dict(reservation.estimated_units),
                "release_reason": "unknown_provider_outcome_ttl",
            },
            created_at=clock,
        )
        ledger.append(event)
        released += 1
    return released


def _clamp_non_negative(units: Mapping[str, UnitQuantity]) -> UnitMap:
    clamped: UnitMap = {}
    for unit, quantity in units.items():
        exact = unit_quantity_decimal(quantity)
        if exact > 0:
            clamped[unit] = quantity
    return clamped


@contextmanager
def _transaction(connection: Any):
    transaction = getattr(connection, "transaction", None)
    if callable(transaction):
        with transaction():
            yield
        return
    connection.execute("BEGIN", {})
    try:
        yield
    except Exception:
        connection.execute("ROLLBACK", {})
        raise
    connection.execute("COMMIT", {})


def _execute_one(connection: Any, statement: SqlStatement) -> Mapping[str, Any] | None:
    result = connection.execute(statement.sql, statement.params)
    return _one_row_from_result(result)


def _execute_rows(connection: Any, statement: SqlStatement) -> list[dict[str, Any]]:
    result = connection.execute(statement.sql, statement.params)
    if result is None:
        return []
    if isinstance(result, list):
        return [dict(row) for row in result]
    if isinstance(result, tuple):
        return [dict(row) for row in result]
    if hasattr(result, "mappings"):
        return [dict(row) for row in result.mappings()]
    if hasattr(result, "fetchall"):
        return [dict(row) for row in result.fetchall()]
    try:
        return [dict(row) for row in result]
    except TypeError:
        return []


def _one_row_from_result(result: Any) -> Mapping[str, Any] | None:
    if result is None:
        return None
    if isinstance(result, list):
        return dict(result[0]) if result else None
    if isinstance(result, tuple):
        return dict(result[0]) if result else None
    if hasattr(result, "mappings"):
        rows = list(result.mappings())
        return dict(rows[0]) if rows else None
    if hasattr(result, "fetchone"):
        row = result.fetchone()
        return dict(row) if row is not None else None
    try:
        iterator = iter(result)
    except TypeError:
        return None
    try:
        row = next(iterator)
    except StopIteration:
        return None
    return dict(row)


def _json_param(value: Any) -> str:
    return json.dumps(jsonable_exact(value), sort_keys=True, separators=(",", ":"))


def _json_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    if isinstance(value, bytes):
        parsed = json.loads(value.decode("utf-8"))
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _text_array(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


_LOCK_ACTIVE_WORKSPACE_BALANCE_SQL = """
SELECT
    workspace_id,
    entitlement_policy_id,
    remaining_units_jsonb,
    reserved_units_jsonb,
    unlimited_units
FROM workspace_balances
WHERE workspace_id = %(workspace_id)s
  AND entitlement_policy_id = %(entitlement_policy_id)s
  AND period_start_at <= now()
  AND period_end_at > now()
FOR UPDATE;
""".strip()


_LOCK_EXISTING_USAGE_RESERVATION_SQL = """
SELECT *
FROM usage_reservations
WHERE workspace_id = %(workspace_id)s
  AND idempotency_key = %(idempotency_key)s
FOR UPDATE;
""".strip()


_LOCK_USAGE_RESERVATION_BY_ID_SQL = """
SELECT *
FROM usage_reservations
WHERE workspace_id = %(workspace_id)s
  AND id = %(reservation_id)s
FOR UPDATE;
""".strip()


_LOCK_CURRENT_WORKSPACE_BALANCE_SQL = """
SELECT
    workspace_id,
    entitlement_policy_id,
    remaining_units_jsonb,
    reserved_units_jsonb,
    unlimited_units
FROM workspace_balances
WHERE workspace_id = %(workspace_id)s
  AND period_start_at <= now()
  AND period_end_at > now()
FOR UPDATE;
""".strip()


_STALE_UNKNOWN_USAGE_RESERVATIONS_SQL = """
SELECT
    reservation.*,
    event.id AS unknown_usage_event_id
FROM usage_reservations AS reservation
JOIN usage_events AS event
  ON event.reservation_id = reservation.id
 AND event.workspace_id = reservation.workspace_id
WHERE reservation.status = 'reserved'
  AND event.status = 'unknown'
  AND event.created_at <= %(older_than)s
ORDER BY event.created_at ASC, event.id ASC
LIMIT %(limit)s;
""".strip()


_UPDATE_WORKSPACE_BALANCE_RESERVATION_SQL = """
UPDATE workspace_balances
SET remaining_units_jsonb = %(remaining_units_jsonb)s::jsonb,
    reserved_units_jsonb = %(reserved_units_jsonb)s::jsonb,
    updated_at = now()
WHERE workspace_id = %(workspace_id)s
  AND entitlement_policy_id = %(entitlement_policy_id)s
RETURNING *;
""".strip()


_UPDATE_CURRENT_WORKSPACE_BALANCE_SQL = """
UPDATE workspace_balances
SET remaining_units_jsonb = %(remaining_units_jsonb)s::jsonb,
    reserved_units_jsonb = %(reserved_units_jsonb)s::jsonb,
    updated_at = now()
WHERE workspace_id = %(workspace_id)s
RETURNING *;
""".strip()
