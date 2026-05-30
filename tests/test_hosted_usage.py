from __future__ import annotations

from decimal import Decimal

from psycopg.types.json import Jsonb

from yutome.hosted.gate import UsageGate
from yutome.hosted.errors import classify_provider_http_error
from yutome.hosted.events import denied_usage_event, usage_event_from_normalization
from yutome.hosted.ids import idempotency_key, input_hash
from yutome.hosted.ledger import PostgresUsageGate, PostgresUsageLedger, reconcile_reservation_usage
from yutome.hosted.models import (
    EntitlementPolicy,
    ProviderAllocation,
    ServiceAllocation,
    UsageEvent,
    WorkspaceBalance,
)
from yutome.hosted.normalizers import (
    normalize_gemini_generate_content,
    normalize_search_store_usage,
    normalize_voyage_embeddings_response,
    normalize_webshare_activity,
    normalize_webshare_stats,
    normalize_webshare_subuser,
)


def _jsonb_obj(value: object) -> object:
    assert isinstance(value, Jsonb)
    return value.obj


def test_input_hash_is_stable_for_equivalent_payloads() -> None:
    left = input_hash({"b": 2, "a": {"d": 4, "c": 3}})
    right = input_hash({"a": {"c": 3, "d": 4}, "b": 2})

    assert left == right
    assert left.startswith("h_")
    assert idempotency_key(
        workspace_id="ws_alice",
        subject_id="vid_123",
        operation="voyage.embed_documents",
        input_hash_value=left,
        extras=["sip_default"],
    ) == f"ws_alice:vid_123:voyage.embed_documents:{left}:sip_default"


def test_idempotency_key_escapes_component_boundaries() -> None:
    left = idempotency_key(
        workspace_id="ws:alice",
        subject_id="vid",
        operation="op",
        input_hash_value="h",
        extras=["a:b"],
    )
    right = idempotency_key(
        workspace_id="ws",
        subject_id="alice:vid",
        operation="op",
        input_hash_value="h",
        extras=["a:b"],
    )

    assert left == "ws%3Aalice:vid:op:h:a%3Ab"
    assert right == "ws:alice%3Avid:op:h:a%3Ab"
    assert left != right


def test_usage_gate_reserves_allowed_operation() -> None:
    allocation = ProviderAllocation(
        id="alloc_gemini",
        workspace_id="ws_alice",
        provider="gemini",
        operation="cleanup_transcript",
    )
    reservation = UsageGate().reserve(
        workspace_id="ws_alice",
        subject="gemini",
        operation="cleanup_transcript",
        estimated_units={"total_tokens": 2000},
        allocation=allocation,
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={"gemini.cleanup_transcript"},
        ),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"total_tokens": 5000}),
        idempotency_key="idem",
    )

    assert reservation.status == "reserved"
    assert reservation.decision.allowed is True
    assert reservation.allocation_id == "alloc_gemini"


def test_usage_gate_denies_when_policy_operation_is_missing() -> None:
    allocation = ProviderAllocation(
        id="alloc_gemini",
        workspace_id="ws_alice",
        provider="gemini",
        operation="cleanup_transcript",
    )

    reservation = UsageGate().reserve(
        workspace_id="ws_alice",
        subject="gemini",
        operation="cleanup_transcript",
        estimated_units={"total_tokens": 2000},
        allocation=allocation,
        policy=EntitlementPolicy(id="policy", workspace_id="ws_alice"),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"total_tokens": 5000}),
        idempotency_key="idem",
    )

    assert reservation.status == "denied"
    assert reservation.decision.allowed is False
    assert reservation.decision.reason == "operation_not_allowed"
    assert reservation.decision.message == "Operation is not enabled by policy."


def test_usage_gate_denies_when_balance_unit_is_missing() -> None:
    allocation = ServiceAllocation(
        id="svc_search",
        workspace_id="ws_alice",
        service="search_store",
        operation="hybrid_query",
        backend="postgres_vectorchord",
    )

    reservation = UsageGate().reserve(
        workspace_id="ws_alice",
        subject="search_store",
        operation="hybrid_query",
        estimated_units={"queries": 1, "candidate_limit": 200},
        allocation=allocation,
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={"search_store.hybrid_query"},
        ),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"queries": 10}),
        idempotency_key="idem",
    )

    assert reservation.status == "denied"
    assert reservation.decision.allowed is False
    assert reservation.decision.reason == "insufficient_balance"
    assert reservation.decision.message == "Workspace does not have enough candidate_limit."


def test_usage_gate_allows_explicit_unlimited_balance_unit() -> None:
    allocation = ProviderAllocation(
        id="alloc_voyage",
        workspace_id="ws_alice",
        provider="voyage",
        operation="embed_documents",
    )

    reservation = UsageGate().reserve(
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
        estimated_units={"total_tokens": 2000},
        allocation=allocation,
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={"voyage.embed_documents"},
        ),
        balance=WorkspaceBalance(workspace_id="ws_alice", unlimited_units={"total_tokens"}),
        idempotency_key="idem",
    )

    assert reservation.status == "reserved"
    assert reservation.decision.allowed is True


def test_usage_gate_uses_decimal_quantities_without_float_rounding_denial() -> None:
    allocation = ProviderAllocation(
        id="alloc_voyage",
        workspace_id="ws_alice",
        provider="voyage",
        operation="embed_documents",
    )

    reservation = UsageGate().reserve(
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
        estimated_units={"credits": Decimal("0.10") + Decimal("0.20")},
        allocation=allocation,
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={"voyage.embed_documents"},
        ),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"credits": Decimal("0.30")}),
        idempotency_key="idem",
    )

    assert reservation.status == "reserved"
    assert reservation.estimated_units["credits"] == Decimal("0.30")


def test_usage_gate_denies_before_call_when_limit_exceeded() -> None:
    allocation = ProviderAllocation(
        id="alloc_gemini_fallback",
        workspace_id="ws_alice",
        provider="gemini",
        operation="transcribe_media",
    )
    reservation = UsageGate().reserve(
        workspace_id="ws_alice",
        subject="gemini",
        operation="transcribe_media",
        estimated_units={"media_seconds": 14_400},
        allocation=allocation,
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={"gemini.transcribe_media"},
            hard_limits_by_operation={"gemini.transcribe_media": {"media_seconds": 5_400}},
        ),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"media_seconds": 20_000}),
        idempotency_key="idem",
    )

    assert reservation.status == "denied"
    assert reservation.decision.allowed is False
    assert reservation.decision.reason == "usage_limit_exceeded"

    event = denied_usage_event(reservation)

    assert event.status == "denied"
    assert event.event_type == "reservation_created"
    assert event.error_code == "usage_limit_exceeded"


def test_usage_gate_denies_search_store_when_balance_is_missing() -> None:
    allocation = ServiceAllocation(
        id="svc_search",
        workspace_id="ws_alice",
        service="search_store",
        operation="hybrid_query",
        backend="postgres_vectorchord",
    )
    reservation = UsageGate().reserve(
        workspace_id="ws_alice",
        subject="search_store",
        operation="hybrid_query",
        estimated_units={"queries": 1, "candidate_limit": 200},
        allocation=allocation,
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={"search_store.hybrid_query"},
        ),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"queries": 0}),
        idempotency_key="idem",
    )

    assert reservation.status == "denied"
    assert reservation.decision.reason == "insufficient_balance"
    assert reservation.decision.denial_effect == "hard"


class AtomicReservationConnection:
    def __init__(self) -> None:
        self.balance = {
            "workspace_id": "ws_alice",
            "entitlement_policy_id": "policy",
            "remaining_units_jsonb": {"total_tokens": 500},
            "reserved_units_jsonb": {},
            "unlimited_units": [],
        }
        self.reservations: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, statement: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        params = dict(params or {})
        self.calls.append((statement, params))
        if statement in {"BEGIN", "COMMIT", "ROLLBACK"}:
            return []
        if "FROM workspace_balances" in statement and "FOR UPDATE" in statement:
            return [dict(self.balance)]
        if "FROM usage_reservations" in statement and "FOR UPDATE" in statement:
            if "idempotency_key" in params:
                key = str(params["idempotency_key"])
                return [self.reservations[key]] if key in self.reservations else []
            reservation_id = str(params["reservation_id"])
            return [row for row in self.reservations.values() if row["id"] == reservation_id]
        if "INSERT INTO usage_reservations" in statement:
            row = {
                "id": params["id"],
                "workspace_id": params["workspace_id"],
                "subject": params["subject"],
                "operation": params["operation"],
                "allocation_id": params["allocation_id"],
                "credential_mode": params["credential_mode"],
                "estimated_units_json": _jsonb_obj(params["estimated_units_json"]),
                "idempotency_key": params["idempotency_key"],
                "status": params["status"],
                "decision_json": _jsonb_obj(params["decision_json"]),
                "metadata_json": _jsonb_obj(params["metadata_json"]),
                "created_at": params["created_at"],
            }
            self.reservations[str(params["idempotency_key"])] = row
            return [row]
        if "INSERT INTO usage_events" in statement:
            return [
                {
                    "id": params["id"],
                    "reservation_id": params["reservation_id"],
                    "workspace_id": params["workspace_id"],
                    "subject": params["subject"],
                    "operation": params["operation"],
                    "event_type": params["event_type"],
                    "status": params["status"],
                    "actual_units_json": _jsonb_obj(params["actual_units_json"]),
                    "provider_request_id": params["provider_request_id"],
                    "error_code": params["error_code"],
                    "raw_usage_json": _jsonb_obj(params["raw_usage_json"]),
                    "metadata_json": _jsonb_obj(params["metadata_json"]),
                    "created_at": params["created_at"],
                }
            ]
        if "UPDATE usage_reservations" in statement:
            for row in self.reservations.values():
                if row["id"] == params["reservation_id"]:
                    row["status"] = params["status"]
                    return [row]
            return []
        if "UPDATE workspace_balances" in statement:
            self.balance["remaining_units_jsonb"] = _jsonb_obj(params["remaining_units_jsonb"])
            self.balance["reserved_units_jsonb"] = _jsonb_obj(params["reserved_units_jsonb"])
            return [dict(self.balance)]
        return []


def test_postgres_usage_gate_locks_balance_and_updates_reserved_units_once() -> None:
    connection = AtomicReservationConnection()
    gate = PostgresUsageGate(connection)
    allocation = ProviderAllocation(
        id="alloc_voyage",
        workspace_id="ws_alice",
        provider="voyage",
        operation="embed_documents",
    )
    policy = EntitlementPolicy(id="policy", workspace_id="ws_alice", allowed_operations={"voyage.embed_documents"})
    balance = WorkspaceBalance(workspace_id="ws_alice", remaining_units={"total_tokens": 500})

    first = gate.reserve(
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
        estimated_units={"total_tokens": 100},
        allocation=allocation,
        policy=policy,
        balance=balance,
        idempotency_key="idem_once",
    )
    second = gate.reserve(
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
        estimated_units={"total_tokens": 100},
        allocation=allocation,
        policy=policy,
        balance=balance,
        idempotency_key="idem_once",
    )

    assert first.id == second.id
    assert first.status == "reserved"
    assert connection.balance["remaining_units_jsonb"] == {"total_tokens": 400}
    assert connection.balance["reserved_units_jsonb"] == {"total_tokens": 100}
    assert sum(1 for sql, _params in connection.calls if "UPDATE workspace_balances" in sql) == 1


def test_postgres_usage_ledger_reconciles_reserved_units_once_on_success() -> None:
    connection = AtomicReservationConnection()
    gate = PostgresUsageGate(connection)
    ledger = PostgresUsageLedger(connection)
    allocation = ProviderAllocation(
        id="alloc_voyage",
        workspace_id="ws_alice",
        provider="voyage",
        operation="embed_documents",
    )
    policy = EntitlementPolicy(id="policy", workspace_id="ws_alice", allowed_operations={"voyage.embed_documents"})
    reservation = gate.reserve(
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
        estimated_units={"total_tokens": 100},
        allocation=allocation,
        policy=policy,
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"total_tokens": 500}),
        idempotency_key="idem_success",
    )
    event = UsageEvent(
        reservation_id=reservation.id,
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
        event_type="provider_attempt_succeeded",
        status="succeeded",
        actual_units={"total_tokens": 91},
        provider_request_id="req_success",
        metadata={"idempotency_key": "idem_success"},
    )

    ledger.append(event)
    ledger.append(event)

    assert connection.balance["remaining_units_jsonb"] == {"total_tokens": 409}
    assert connection.balance["reserved_units_jsonb"] == {}
    assert next(iter(connection.reservations.values()))["status"] == "reconciled"


def test_postgres_usage_ledger_persists_overage_as_negative_balance() -> None:
    connection = AtomicReservationConnection()
    connection.balance["remaining_units_jsonb"] = {"total_tokens": 100}
    gate = PostgresUsageGate(connection)
    ledger = PostgresUsageLedger(connection)
    allocation = ProviderAllocation(
        id="alloc_voyage",
        workspace_id="ws_alice",
        provider="voyage",
        operation="embed_documents",
    )
    policy = EntitlementPolicy(id="policy", workspace_id="ws_alice", allowed_operations={"voyage.embed_documents"})
    reservation = gate.reserve(
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
        estimated_units={"total_tokens": 100},
        allocation=allocation,
        policy=policy,
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"total_tokens": 100}),
        idempotency_key="idem_overage",
    )

    ledger.append(
        UsageEvent(
            reservation_id=reservation.id,
            workspace_id="ws_alice",
            subject="voyage",
            operation="embed_documents",
            event_type="provider_attempt_succeeded",
            status="succeeded",
            actual_units={"total_tokens": 150},
            provider_request_id="req_overage",
            metadata={"idempotency_key": "idem_overage"},
        )
    )

    assert connection.balance["remaining_units_jsonb"] == {"total_tokens": -50}
    assert connection.balance["reserved_units_jsonb"] == {}


class SubscribedReservationConnection(AtomicReservationConnection):
    """AtomicReservationConnection that also models an active Stripe subscriber and captures
    the meter exports enqueued by the usage->meter loop."""

    def __init__(self) -> None:
        super().__init__()
        self.meter_export_params: list[dict[str, object]] = []

    def execute(self, statement: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        params = dict(params or {})
        if "FROM stripe_customers" in statement and "subscription_status = 'active'" in statement:
            self.calls.append((statement, params))
            return [{"stripe_customer_id": "cus_sub_1"}]
        if "INSERT INTO stripe_meter_exports" in statement:
            self.calls.append((statement, params))
            self.meter_export_params.append(params)
            return []
        return super().execute(statement, params)


def _reserve_and_settle(
    connection: SubscribedReservationConnection,
    *,
    estimate_vectors,
    actual_vectors,
    key: str,
):
    # The gate authorizes on the ESTIMATE (which must fit the remaining included allowance);
    # the metered overage is computed from the settled ACTUAL units (which may exceed it).
    gate = PostgresUsageGate(connection)
    ledger = PostgresUsageLedger(connection)
    remaining = connection.balance["remaining_units_jsonb"]
    allocation = ProviderAllocation(
        id="alloc_voyage", workspace_id="ws_alice", provider="voyage", operation="embed_documents"
    )
    policy = EntitlementPolicy(id="policy", workspace_id="ws_alice", allowed_operations={"voyage.embed_documents"})
    reservation = gate.reserve(
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
        estimated_units={"vectors": estimate_vectors},
        allocation=allocation,
        policy=policy,
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units=dict(remaining)),
        idempotency_key=key,
    )
    assert reservation.status == "reserved"
    ledger.append(
        UsageEvent(
            reservation_id=reservation.id,
            workspace_id="ws_alice",
            subject="voyage",
            operation="embed_documents",
            event_type="provider_attempt_succeeded",
            status="succeeded",
            actual_units={"vectors": actual_vectors},
            provider_request_id=f"req_{key}",
            metadata={"idempotency_key": key},
        )
    )
    return reservation


def test_usage_within_included_allowance_enqueues_no_meter_export(monkeypatch) -> None:
    # vectors weight 7e-3. Balance 10 vectors = 0.07 credits; settled actual 2 vectors = 0.014
    # credits, comfortably within the included allowance -> nothing metered (even with overage on).
    monkeypatch.setenv("STRIPE_OVERAGE_ENABLED", "1")
    connection = SubscribedReservationConnection()
    connection.balance["remaining_units_jsonb"] = {"vectors": 10}

    _reserve_and_settle(connection, estimate_vectors=2, actual_vectors=2, key="idem_within")

    assert connection.meter_export_params == []


def test_usage_beyond_included_allowance_meters_only_the_overage(monkeypatch) -> None:
    # With overage metering enabled (yt-indexer-6a0): balance 2 vectors = 0.014 included credits.
    # The estimate (2) fits and is authorized, but the settled actual is 10 vectors = 0.07
    # credits. Only the excess 0.07 - 0.014 = 0.056 credits is metered; the included 0.014 is
    # never billed.
    monkeypatch.setenv("STRIPE_OVERAGE_ENABLED", "1")
    connection = SubscribedReservationConnection()
    connection.balance["remaining_units_jsonb"] = {"vectors": 2}

    _reserve_and_settle(connection, estimate_vectors=2, actual_vectors=10, key="idem_overage_meter")

    assert len(connection.meter_export_params) == 1
    assert connection.meter_export_params[0]["value_text"] == "0.056"
    # Remaining included allowance is now overdrawn (2 - 10 = -8 vectors).
    assert connection.balance["remaining_units_jsonb"] == {"vectors": -8}
    # event_credits records the full event; metered_credits is just the overage.
    metadata = _jsonb_obj(connection.meter_export_params[0]["metadata_json"])
    assert metadata["event_credits"] == "0.070"
    assert metadata["metered_credits"] == "0.056"


def test_overage_metering_disabled_by_default_enqueues_nothing(monkeypatch) -> None:
    # Launch hard-cap default (STRIPE_OVERAGE_ENABLED unset): even usage beyond the included
    # allowance enqueues no meter export -- the flat seat is the only charge.
    monkeypatch.delenv("STRIPE_OVERAGE_ENABLED", raising=False)
    connection = SubscribedReservationConnection()
    connection.balance["remaining_units_jsonb"] = {"vectors": 2}

    _reserve_and_settle(connection, estimate_vectors=2, actual_vectors=10, key="idem_overage_off")

    assert connection.meter_export_params == []


def test_postgres_usage_gate_allows_new_reservations_with_existing_negative_balance_units() -> None:
    connection = AtomicReservationConnection()
    connection.balance["remaining_units_jsonb"] = {"total_tokens": 500, "request_duration": -1.2}
    gate = PostgresUsageGate(connection)
    allocation = ProviderAllocation(
        id="alloc_voyage",
        workspace_id="ws_alice",
        provider="voyage",
        operation="embed_documents",
    )

    reservation = gate.reserve(
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
        estimated_units={"total_tokens": 100},
        allocation=allocation,
        policy=EntitlementPolicy(id="policy", workspace_id="ws_alice", allowed_operations={"voyage.embed_documents"}),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"total_tokens": 500, "request_duration": -1.2}),
        idempotency_key="idem_after_overage",
    )

    assert reservation.status == "reserved"
    assert connection.balance["remaining_units_jsonb"] == {"request_duration": "-1.2", "total_tokens": 400}
    assert connection.balance["reserved_units_jsonb"] == {"total_tokens": 100}


def test_gemini_usage_normalizer_preserves_raw_usage_and_core_units() -> None:
    normalized = normalize_gemini_generate_content(
        {
            "responseId": "resp-123",
            "modelVersion": "gemini-3.1-flash-lite",
            "candidates": [{"index": 0, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 100,
                "cachedContentTokenCount": 25,
                "candidatesTokenCount": 20,
                "toolUsePromptTokenCount": 5,
                "thoughtsTokenCount": 7,
                "totalTokenCount": 132,
                "serviceTier": "standard",
            },
        },
        operation="cleanup_transcript",
    )

    assert normalized.subject == "gemini"
    assert normalized.provider_request_id == "resp-123"
    assert normalized.actual_units["prompt_tokens"] == 100
    assert normalized.actual_units["cached_content_tokens"] == 25
    assert normalized.actual_units["total_tokens"] == 132
    assert normalized.raw_usage["usageMetadata"]["serviceTier"] == "standard"


def test_usage_event_from_normalization_links_to_reservation() -> None:
    allocation = ProviderAllocation(
        id="alloc_voyage",
        workspace_id="ws_alice",
        provider="voyage",
        operation="embed_documents",
    )
    reservation = UsageGate().reserve(
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
        estimated_units={"total_tokens": 100},
        allocation=allocation,
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={"voyage.embed_documents"},
        ),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"total_tokens": 500}),
        idempotency_key="idem",
    )
    normalized = normalize_voyage_embeddings_response(
        {"embeddings": [[0.1], [0.2]], "usage": {"total_tokens": 91}},
        operation="embed_documents",
    )

    event = usage_event_from_normalization(
        normalized,
        reservation=reservation,
        event_type="provider_attempt_succeeded",
    )

    assert event.reservation_id == reservation.id
    assert event.workspace_id == "ws_alice"
    assert event.actual_units["total_tokens"] == 91
    assert event.event_type == "provider_attempt_succeeded"


def test_reservation_reconciliation_derives_release_and_overage_units() -> None:
    allocation = ProviderAllocation(
        id="alloc_voyage",
        workspace_id="ws_alice",
        provider="voyage",
        operation="embed_documents",
    )
    reservation = UsageGate().reserve(
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
        estimated_units={"total_tokens": 100, "credits": Decimal("0.30")},
        allocation=allocation,
        policy=EntitlementPolicy(
            id="policy",
            workspace_id="ws_alice",
            allowed_operations={"voyage.embed_documents"},
        ),
        balance=WorkspaceBalance(workspace_id="ws_alice", remaining_units={"total_tokens": 500, "credits": 1}),
        idempotency_key="idem",
    )
    event = UsageEvent(
        id="evt_usage_1",
        reservation_id=reservation.id,
        workspace_id="ws_alice",
        subject="voyage",
        operation="embed_documents",
        event_type="provider_attempt_succeeded",
        status="succeeded",
        actual_units={"total_tokens": 91, "credits": Decimal("0.35")},
    )

    reconciliation = reconcile_reservation_usage(reservation, event)
    repeat = reconcile_reservation_usage(reservation, event)

    assert reconciliation.id == repeat.id
    assert reconciliation.released_units == {"total_tokens": 9}
    assert reconciliation.overage_units == {"credits": Decimal("0.05")}


def test_voyage_usage_normalizer_accepts_rest_shape() -> None:
    normalized = normalize_voyage_embeddings_response(
        {
            "object": "list",
            "model": "voyage-4-lite",
            "data": [{"index": 0, "object": "embedding"}, {"index": 1, "object": "embedding"}],
            "usage": {"total_tokens": 18191},
        },
        operation="embed_documents",
        input_type="document",
        output_dimension=1024,
        output_dtype="float",
    )

    assert normalized.subject == "voyage"
    assert normalized.actual_units["total_tokens"] == 18191
    assert normalized.actual_units["vectors"] == 2
    assert normalized.metadata["input_type"] == "document"
    assert normalized.metadata["output_dimension"] == 1024


def test_webshare_normalizers_keep_quota_and_byte_units_separate() -> None:
    subuser = normalize_webshare_subuser(
        {
            "id": 451,
            "label": "Alice",
            "proxy_limit": 0,
            "max_thread_count": 10,
            "bandwidth_use_start_date": "2026-05-01",
            "bandwidth_use_end_date": "2026-06-01",
        }
    )
    stats = normalize_webshare_stats(
        {
            "timestamp": "2026-05-25T23:00:00Z",
            "is_projected": False,
            "bandwidth_total": 5000,
            "requests_total": 5,
            "requests_successful": 4,
            "requests_failed": 1,
            "number_of_proxies_used": 2,
            "error_reasons": [],
            "countries_used": ["US"],
        }
    )
    activity = normalize_webshare_activity(
        {
            "timestamp": "2026-05-25T23:00:00Z",
            "request_duration": 1.2,
            "handshake_duration": 0.1,
            "tunnel_duration": 1.0,
            "bytes": 1234,
            "hostname": "youtube.com",
            "domain": "youtube.com",
            "error_reason": None,
        }
    )

    assert subuser["proxy_limit_gb"] is None
    assert stats.actual_units["bandwidth_bytes"] == 5000
    assert stats.actual_units["requests_failed"] == 1
    assert activity.actual_units["bytes"] == 1234
    assert activity.metadata["error_reason"] is None


def test_search_store_usage_normalizer_records_internal_service_units() -> None:
    normalized = normalize_search_store_usage(
        operation="hybrid_query",
        backend="postgres_vectorchord",
        index_profile_ref="sip_default",
        units={"queries": 1, "candidate_count": 42, "latency_ms": 18.5},
    )

    assert normalized.subject == "search_store"
    assert normalized.actual_units["candidate_count"] == 42
    assert normalized.metadata["backend"] == "postgres_vectorchord"
    assert normalized.metadata["index_profile_ref"] == "sip_default"


def test_provider_http_failure_classification() -> None:
    quota = classify_provider_http_error(provider="webshare", status_code=402, message="Payment Required")
    rate_limit = classify_provider_http_error(provider="voyage", status_code=429, message="Too Many Requests")
    transient = classify_provider_http_error(provider="gemini", status_code=503, message="Unavailable")

    assert quota.kind == "quota"
    assert quota.retryable is False
    assert rate_limit.kind == "rate_limit"
    assert rate_limit.retryable is True
    assert transient.kind == "transient"
    assert transient.retryable is True
