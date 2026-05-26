from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yutome.config import DEFAULT_CONFIG_FILENAME, load_config
from yutome.hosted.gate import Allocation, UsageGate
from yutome.hosted.ids import input_hash
from yutome.hosted.models import (
    EntitlementPolicy,
    UnitMap,
    UnitQuantity,
    UsageEvent,
    UsageReservation,
    WorkspaceBalance,
    normalize_unit_map,
    unit_quantity_decimal,
)
from yutome.hosted.repositories import (
    SqlStatement,
    insert_usage_event_sql,
    upsert_usage_reservation_sql,
    usage_event_from_row,
    usage_reservation_from_row,
)
from yutome.paths import ProjectPaths


def default_usage_ledger_path(config_path: Path = Path(DEFAULT_CONFIG_FILENAME)) -> Path:
    config = load_config(config_path)
    project_root = config_path.parent if config_path.is_absolute() else (Path.cwd() / config_path).parent
    configured = config.hosted.usage_ledger_path
    if configured.is_absolute():
        return configured
    if configured.parts and configured.parts[0] == str(config.storage.data_dir):
        return project_root / configured
    paths = ProjectPaths.from_config(config, project_root=project_root)
    return paths.data_dir / configured


class JsonlUsageLedger:
    """Append-only local ledger used for debug commands and early tests.

    Hosted production will use Postgres. Keeping this adapter narrow gives the
    CLI a useful inspection path before the hosted database exists.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, event: UsageEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")

    def recent(self, *, limit: int = 20) -> list[UsageEvent]:
        if not self.path.exists():
            return []
        rows: list[UsageEvent] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    rows.append(UsageEvent.model_validate(json.loads(line)))
        return rows[-max(0, limit) :]


class PostgresUsageGate:
    """UsageGate adapter that durably upserts reservations before provider calls."""

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
        reservation = self.gate.reserve(
            workspace_id=workspace_id,
            subject=subject,
            operation=operation,
            estimated_units=estimated_units,
            allocation=allocation,
            policy=policy,
            balance=balance,
            idempotency_key=idempotency_key,
        )
        durable = stable_usage_reservation(reservation)
        row = _execute_one(self.connection, upsert_usage_reservation_sql(durable))
        return usage_reservation_from_row(row) if row else durable


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
        row = _execute_one(self.connection, insert_usage_event_sql(durable, idempotency=idempotency))
        return usage_event_from_row(row) if row else durable


def stable_usage_reservation(reservation: UsageReservation) -> UsageReservation:
    return reservation.model_copy(
        update={
            "id": _stable_id("res", reservation.workspace_id, reservation.idempotency_key),
        }
    )


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


def _execute_one(connection: Any, statement: SqlStatement) -> Mapping[str, Any] | None:
    result = connection.execute(statement.sql, statement.params)
    return _one_row_from_result(result)


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
