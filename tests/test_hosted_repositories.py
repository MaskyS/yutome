from __future__ import annotations

import json
from datetime import datetime, timezone

from yutome.hosted.ledger import stable_usage_event, stable_usage_reservation
from yutome.hosted.models import UsageDecision, UsageEvent, UsageReservation
from yutome.hosted.repositories import (
    insert_usage_event_sql,
    update_usage_reservation_status_sql,
    upsert_usage_reservation_sql,
    usage_event_from_row,
    usage_repository_constraint_statements,
    usage_reservation_from_row,
)


def test_usage_reservation_upsert_preserves_idempotency_boundary() -> None:
    reservation = UsageReservation(
        id="res_1",
        workspace_id="ws_alice",
        subject="gemini",
        operation="cleanup_transcript",
        allocation_id="alloc_gemini",
        allocation_kind="hosted",
        estimated_units={"total_tokens": 2000},
        idempotency_key="ws_alice:video:cleanup:h_123",
        status="reserved",
        decision=UsageDecision(allowed=True),
    )

    statement = upsert_usage_reservation_sql(reservation)

    assert "INSERT INTO usage_reservations" in statement.sql
    assert "ON CONFLICT (workspace_id, idempotency_key) DO UPDATE" in statement.sql
    assert "SET idempotency_key = usage_reservations.idempotency_key" in statement.sql
    assert statement.params["workspace_id"] == "ws_alice"
    assert json.loads(statement.params["estimated_units_json"]) == {"total_tokens": 2000}
    assert json.loads(statement.params["decision_json"]) == {"allowed": True, "message": None, "reason": "allowed"}


def test_usage_event_insert_supports_event_id_and_provider_request_idempotency() -> None:
    event = UsageEvent(
        id="evt_1",
        reservation_id="res_1",
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
        event_type="provider_attempt_succeeded",
        status="succeeded",
        actual_units={"total_tokens": 91, "vectors": 2},
        provider_request_id="req_123",
        raw_usage={"usage": {"total_tokens": 91}},
    )

    by_id = insert_usage_event_sql(event)
    by_request = insert_usage_event_sql(event, idempotency="provider_request")

    assert "ON CONFLICT (id) DO UPDATE" in by_id.sql
    assert "ON CONFLICT (workspace_id, subject, operation, event_type, provider_request_id)" in by_request.sql
    assert "WHERE provider_request_id IS NOT NULL" in by_request.sql
    assert json.loads(by_id.params["actual_units_json"]) == {"total_tokens": 91, "vectors": 2}
    assert json.loads(by_id.params["raw_usage_json"]) == {"usage": {"total_tokens": 91}}


def test_usage_repository_constraints_make_idempotency_explicit() -> None:
    statements = "\n".join(usage_repository_constraint_statements())

    assert "idx_usage_reservations_workspace_idempotency_key" in statements
    assert "ON usage_reservations(workspace_id, idempotency_key)" in statements
    assert "idx_usage_events_provider_request_idempotency" in statements
    assert "WHERE provider_request_id IS NOT NULL" in statements


def test_usage_reservation_status_update_is_workspace_scoped() -> None:
    statement = update_usage_reservation_status_sql(
        reservation_id="res_1",
        workspace_id="ws_alice",
        status="released",
    )

    assert "UPDATE usage_reservations" in statement.sql
    assert "workspace_id = %(workspace_id)s" in statement.sql
    assert statement.params == {"reservation_id": "res_1", "workspace_id": "ws_alice", "status": "released"}


def test_usage_repository_row_mappers_round_trip_json_fields() -> None:
    created_at = datetime(2026, 5, 26, 3, 45, tzinfo=timezone.utc)
    reservation = usage_reservation_from_row(
        {
            "id": "res_1",
            "workspace_id": "ws_alice",
            "subject": "gemini",
            "operation": "cleanup_transcript",
            "allocation_id": "alloc_gemini",
            "allocation_kind": "hosted",
            "estimated_units_json": '{"total_tokens":2000}',
            "idempotency_key": "idem",
            "status": "reserved",
            "decision_json": '{"allowed":true,"reason":"allowed","message":null}',
            "metadata_json": '{"job_id":"job_1"}',
            "created_at": created_at,
        }
    )
    event = usage_event_from_row(
        {
            "id": "evt_1",
            "reservation_id": "res_1",
            "workspace_id": "ws_alice",
            "subject": "gemini",
            "operation": "cleanup_transcript",
            "event_type": "provider_attempt_succeeded",
            "status": "succeeded",
            "actual_units_json": {"total_tokens": 91},
            "provider_request_id": "req_123",
            "error_code": None,
            "raw_usage_json": '{"usage":{"total_tokens":91}}',
            "metadata_json": {"job_id": "job_1"},
            "created_at": created_at.isoformat(),
        }
    )

    assert reservation.estimated_units == {"total_tokens": 2000}
    assert reservation.decision.allowed is True
    assert reservation.metadata == {"job_id": "job_1"}
    assert event.actual_units == {"total_tokens": 91}
    assert event.raw_usage == {"usage": {"total_tokens": 91}}
    assert event.created_at == created_at


def test_stable_usage_ids_are_derived_from_retry_idempotency_key() -> None:
    reservation = UsageReservation(
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
        allocation_kind="hosted",
        estimated_units={"total_tokens": 200},
        idempotency_key="ws_alice:vid_123:voyage.embed_documents:h_fake",
        status="reserved",
        decision=UsageDecision(allowed=True),
    )
    first_event = UsageEvent(
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
        event_type="provider_attempt_succeeded",
        status="succeeded",
        provider_request_id="req_123",
        metadata={"idempotency_key": reservation.idempotency_key},
    )
    retry_event = first_event.model_copy(update={"id": "evt_random_retry"})

    assert stable_usage_reservation(reservation).id == stable_usage_reservation(
        reservation.model_copy(update={"id": "res_random_retry"})
    ).id
    assert stable_usage_event(first_event).id == stable_usage_event(retry_event).id
