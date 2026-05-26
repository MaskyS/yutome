from __future__ import annotations

import json
import base64
import hashlib
import hmac
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from yutome.hosted.billing import (
    BillingCustomer,
    PolarWebhookVerificationError,
    CreditLedgerEntry,
    EntitlementPolicyRecord,
    PriceBook,
    PriceBookProduct,
    ProductLimit,
    WorkspaceBalanceSnapshot,
    balance_reconciliation_input_sql,
    billing_debug_reservation_from_row,
    billing_debug_snapshot_from_rows,
    billing_debug_snapshot_sql,
    claim_billing_exports_sql,
    billing_export_event_from_usage_event,
    billing_export_event_from_row,
    billing_export_idempotency_key,
    billing_schema_statements,
    credit_ledger_entry_from_order,
    derive_workspace_balance_snapshot_from_rows,
    derive_workspace_balance_snapshot,
    finish_billing_export_sql,
    mark_billing_export_replay,
    payload_sha256,
    polar_webhook_event_from_payload,
    polar_webhook_processing_statements,
    polar_webhook_snapshot_from_payload,
    process_polar_webhook_payload,
    upsert_billing_customer_sql,
    upsert_credit_ledger_entry_sql,
    upsert_billing_export_sql,
    upsert_entitlement_policy_sql,
    upsert_polar_webhook_snapshot_sql,
    upsert_price_book_sql,
    upsert_workspace_balance_sql,
    verify_standard_webhook_signature,
)
from yutome.hosted.gate import UsageGate
from yutome.hosted.models import EntitlementPolicy, ProviderAllocation, UsageEvent, WorkspaceBalance


def _usage_event(**overrides: object) -> UsageEvent:
    values = {
        "id": "evt_usage_1",
        "reservation_id": "res_1",
        "workspace_id": "ws_alice",
        "subject": "voyage",
        "operation": "embed_documents",
        "event_type": "provider_attempt_succeeded",
        "status": "succeeded",
        "actual_units": {"total_tokens": 91, "vectors": 2, "ignored_label": "warm"},
        "provider_request_id": "req_1",
        "created_at": datetime(2026, 5, 26, 3, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return UsageEvent(**values)  # type: ignore[arg-type]


def _standard_webhook_headers(raw_body: bytes, *, secret: str = "polar-secret", webhook_id: str = "wh_msg_123") -> dict[str, str]:
    timestamp = "1780000000"
    signed = webhook_id.encode() + b"." + timestamp.encode() + b"." + raw_body
    signature = base64.b64encode(hmac.new(secret.encode(), signed, hashlib.sha256).digest()).decode("ascii")
    return {
        "webhook-id": webhook_id,
        "webhook-timestamp": timestamp,
        "webhook-signature": f"v1,{signature}",
    }


def test_price_book_models_product_limits_without_authorizing_usage() -> None:
    limit = ProductLimit(
        product_code="pro",
        operation_key="voyage.embed_documents",
        unit="total_tokens",
        included_quantity=1_000_000,
        hard_limit=2_000_000,
        polar_meter_name="ai_usage",
    )
    product = PriceBookProduct(code="pro", name="Pro", polar_product_id="prod_123", limits=(limit,))
    price_book = PriceBook(id="pb_2026_05", version="2026-05", products=(product,))

    assert price_book.product("pro") == product
    assert product.limits_for_operation("voyage.embed_documents") == (limit,)
    assert "allowed" not in price_book.model_dump()


def test_billing_export_key_is_stable_for_usage_event_replay() -> None:
    event = _usage_event()

    left = billing_export_event_from_usage_event(event, price_book_version="2026-05", product_code="pro")
    right = billing_export_event_from_usage_event(event, price_book_version="2026-05", product_code="pro")

    assert left.idempotency_key == right.idempotency_key
    assert left.idempotency_key == billing_export_idempotency_key(event)
    assert left.replay_status == "pending"
    assert left.authorization_effect == "none"

    polar = left.to_polar_event()

    assert polar.name == "yutome.voyage.embed_documents"
    assert polar.external_customer_id == "ws_alice"
    assert polar.external_id == "polar:ws_alice:evt_usage_1:voyage.embed_documents"
    assert polar.metadata["total_tokens"] == 91
    assert polar.metadata["vectors"] == 2
    assert polar.metadata["usage_event_id"] == "evt_usage_1"
    assert polar.metadata["price_book_version"] == "2026-05"
    assert "ignored_label" not in polar.metadata


def test_billing_export_preserves_large_integer_units_without_float_rounding() -> None:
    huge = 9_007_199_254_740_993
    event = _usage_event(actual_units={"total_tokens": huge})

    export = billing_export_event_from_usage_event(event)
    polar = export.to_polar_event()

    assert export.actual_units["total_tokens"] == huge
    assert polar.metadata["total_tokens"] == huge


def test_billing_export_polar_metadata_uses_exact_decimal_json() -> None:
    event = _usage_event(
        actual_units={"credits": Decimal("0.30")},
        metadata={"minimum_billable_credit": Decimal("0.10")},
    )

    export = billing_export_event_from_usage_event(event)
    polar = export.to_polar_event()
    payload = export.to_polar_export().model_dump(mode="json")

    assert polar.metadata["credits"] == 0.3
    assert polar.metadata["credits_exact"] == "0.30"
    assert polar.metadata["credits_micros"] == 300000
    assert polar.metadata["minimum_billable_credit"] == "0.10"
    assert payload["events"][0]["metadata"]["credits"] == 0.3
    assert payload["events"][0]["metadata"]["credits_exact"] == "0.30"
    assert payload["events"][0]["metadata"]["credits_micros"] == 300000
    assert payload["events"][0]["metadata"]["minimum_billable_credit"] == "0.10"


def test_billing_export_rejects_negative_usage_units_for_explicit_reconciliation() -> None:
    event = _usage_event(
        id="evt_credit_1",
        event_type="usage_credit_released",
        status="released",
        actual_units={"total_tokens": -25, "credits": -1.5, "human_note": "retry credit"},
    )

    with pytest.raises(ValueError, match="Negative billing unit"):
        billing_export_event_from_usage_event(event, event_name="yutome.usage_credit")


def test_credit_order_grants_are_positive_and_idempotent() -> None:
    occurred_at = datetime(2026, 5, 26, 4, 30, tzinfo=timezone.utc)

    left = credit_ledger_entry_from_order(
        workspace_id="ws_alice",
        external_order_id="ord_123",
        external_customer_id="cus_123",
        unit="credits",
        quantity=Decimal("12.50"),
        occurred_at=occurred_at,
    )
    right = credit_ledger_entry_from_order(
        workspace_id="ws_alice",
        external_order_id="ord_123",
        external_customer_id="cus_123",
        unit="credits",
        quantity=Decimal("12.50"),
        occurred_at=occurred_at,
    )
    statement = upsert_credit_ledger_entry_sql(left)

    assert left.idempotency_key == right.idempotency_key
    assert left.quantity == Decimal("12.50")
    assert left.signed_units == {"credits": Decimal("12.50")}
    assert "ON CONFLICT (workspace_id, idempotency_key) DO UPDATE" in statement.sql
    assert statement.params["id"] == left.idempotency_key
    assert statement.params["quantity_text"] == "12.50"

    with pytest.raises(ValueError, match="must be positive"):
        CreditLedgerEntry(
            id="cred_bad",
            workspace_id="ws_alice",
            idempotency_key="ord_bad",
            unit="credits",
            quantity=Decimal("-1"),
            reason="order_grant",
            occurred_at=occurred_at,
        )


def test_failed_polar_replay_does_not_change_usage_gate_decision() -> None:
    allocation = ProviderAllocation(
        id="alloc_voyage",
        workspace_id="ws_alice",
        provider="voyage",
        operation="embed_documents",
    )
    gate = UsageGate()

    before = gate.reserve(
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
        idempotency_key="idem_1",
    )
    export = billing_export_event_from_usage_event(_usage_event())
    failed = mark_billing_export_replay(
        export,
        replay_status="failed",
        error_code="polar_unavailable",
        error_message="Polar event ingestion timed out",
    )
    after = gate.reserve(
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
        idempotency_key="idem_2",
    )

    assert before.status == "reserved"
    assert failed.replay_status == "failed"
    assert failed.last_error_code == "polar_unavailable"
    assert after.status == "reserved"
    assert after.decision.allowed is True


def test_denied_usage_events_are_mirror_only_and_skipped_for_export() -> None:
    denied = _usage_event(
        id="evt_denied_1",
        event_type="reservation_created",
        status="denied",
        actual_units={},
        provider_request_id=None,
        error_code="insufficient_balance",
    )

    export = billing_export_event_from_usage_event(denied)

    assert export.replay_status == "skipped"
    assert export.authorization_effect == "none"
    assert "allowed" not in export.model_dump()
    assert "decision" not in export.model_dump()


def test_polar_webhook_event_mirrors_current_type_timestamp_data_shape() -> None:
    payload = {
        "type": "customer.state_changed",
        "timestamp": "2026-05-26T03:00:00Z",
        "data": {"customer": {"id": "cus_123"}, "active_benefit_ids": ["ben_123"]},
    }

    event = polar_webhook_event_from_payload(payload)

    assert event.type == "customer.state_changed"
    assert event.timestamp == datetime(2026, 5, 26, 3, 0, tzinfo=timezone.utc)
    assert event.data["customer"]["id"] == "cus_123"
    assert event.raw == payload


def test_billing_schema_statements_cover_durable_phase6_tables() -> None:
    statements = billing_schema_statements()
    joined = "\n".join(statements)

    assert "CREATE TABLE IF NOT EXISTS price_books" in joined
    assert "CREATE TABLE IF NOT EXISTS entitlement_policies" in joined
    assert "CREATE TABLE IF NOT EXISTS workspace_balances" in joined
    assert "CREATE TABLE IF NOT EXISTS credit_ledger_entries" in joined
    assert "CREATE TABLE IF NOT EXISTS billing_customers" in joined
    assert "CREATE TABLE IF NOT EXISTS billing_exports" in joined
    assert "CREATE TABLE IF NOT EXISTS polar_webhook_snapshots" in joined
    assert "ADD COLUMN IF NOT EXISTS payload_hash text" in joined
    assert "ALTER COLUMN payload_hash SET NOT NULL" in joined
    assert "UNIQUE(provider, source_event_dedupe_key)" in joined
    assert "idx_billing_exports_replay" in joined
    assert "idx_polar_webhook_snapshots_replay" in joined
    assert all(statement.endswith(";") for statement in statements)


def test_price_book_upsert_persists_products_and_unit_mapping() -> None:
    limit = ProductLimit(
        product_code="pro",
        operation_key="voyage.embed_documents",
        unit="total_tokens",
        included_quantity=1_000,
        hard_limit=2_000,
        polar_meter_name="ai_usage",
    )
    price_book = PriceBook(
        id="pb_2026_05",
        version="2026-05",
        status="active",
        products=(PriceBookProduct(code="pro", name="Pro", limits=(limit,)),),
        unit_mapping={"voyage.embed_documents": {"total_tokens": "ai_usage.total_tokens"}},
    )

    statement = upsert_price_book_sql(price_book)

    assert "INSERT INTO price_books" in statement.sql
    assert "ON CONFLICT (version) DO UPDATE" in statement.sql
    assert statement.params["status"] == "active"
    assert json.loads(statement.params["products_jsonb"])[0]["limits"][0]["polar_meter_name"] == "ai_usage"
    assert json.loads(statement.params["unit_mapping_jsonb"]) == {
        "voyage.embed_documents": {"total_tokens": "ai_usage.total_tokens"}
    }


def test_entitlement_policy_and_balance_records_feed_usage_gate_without_polar() -> None:
    policy = EntitlementPolicyRecord(
        id="policy_pro",
        workspace_id="ws_alice",
        plan_key="pro",
        price_book_id="pb_2026_05",
        allowed_operations=("voyage.embed_documents",),
        included_units={"voyage.embed_documents": {"total_tokens": 1_000}},
        hard_limits={"voyage.embed_documents": {"total_tokens": 2_000}},
    )
    balance = WorkspaceBalanceSnapshot(
        workspace_id="ws_alice",
        entitlement_policy_id="policy_pro",
        period_start_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        period_end_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        used_units={"total_tokens": 50},
        remaining_units={"total_tokens": 950},
    )

    policy_statement = upsert_entitlement_policy_sql(policy)
    balance_statement = upsert_workspace_balance_sql(balance)
    runtime_policy = policy.to_runtime_policy()
    runtime_balance = balance.to_runtime_balance()

    assert "ON CONFLICT (workspace_id, plan_key, price_book_id) DO UPDATE" in policy_statement.sql
    assert "ON CONFLICT (workspace_id) DO UPDATE" in balance_statement.sql
    assert policy_statement.params["allowed_operations"] == ["voyage.embed_documents"]
    assert json.loads(policy_statement.params["hard_limits_jsonb"]) == {
        "voyage.embed_documents": {"total_tokens": 2000}
    }
    assert runtime_policy.operation_allowed("voyage.embed_documents")
    assert runtime_balance.has_units({"total_tokens": 900}) == (True, None)


def test_decimal_balance_comparison_does_not_deny_exact_decimal_estimate() -> None:
    balance = WorkspaceBalance(workspace_id="ws_alice", remaining_units={"credits": Decimal("0.30")})

    assert balance.has_units({"credits": Decimal("0.10") + Decimal("0.20")}) == (True, None)


def test_workspace_balance_derives_from_starting_units_credits_usage_and_reservations() -> None:
    occurred_at = datetime(2026, 5, 26, 4, 30, tzinfo=timezone.utc)
    credit = credit_ledger_entry_from_order(
        workspace_id="ws_alice",
        external_order_id="ord_123",
        unit="credits",
        quantity=Decimal("0.30"),
        occurred_at=occurred_at,
    )

    snapshot = derive_workspace_balance_snapshot(
        workspace_id="ws_alice",
        entitlement_policy_id="policy_pro",
        period_start_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        period_end_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        starting_units={"credits": Decimal("0.70"), "total_tokens": 1_000},
        credit_entries=(credit,),
        used_units={"credits": Decimal("0.40"), "total_tokens": 200},
        reserved_units={"credits": Decimal("0.10"), "total_tokens": 50},
    )

    statement = upsert_workspace_balance_sql(snapshot)

    assert snapshot.remaining_units == {"credits": Decimal("0.50"), "total_tokens": 750}
    assert snapshot.metadata["derived_from"]["credit_entry_ids"] == [credit.id]
    assert json.loads(statement.params["remaining_units_jsonb"]) == {
        "credits": "0.50",
        "total_tokens": 750,
    }


def test_workspace_balance_overdraw_clamps_remaining_and_records_deficit() -> None:
    snapshot = derive_workspace_balance_snapshot(
        workspace_id="ws_alice",
        entitlement_policy_id="policy_pro",
        period_start_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        period_end_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        starting_units={"credits": Decimal("1.00"), "total_tokens": 100},
        used_units={"credits": Decimal("1.25"), "total_tokens": 125},
        reserved_units={"credits": Decimal("0.25"), "total_tokens": 5},
    )
    runtime_balance = snapshot.to_runtime_balance()
    statement = upsert_workspace_balance_sql(snapshot)

    assert snapshot.remaining_units == {}
    assert snapshot.metadata["balance_status"] == "overdrawn"
    assert snapshot.metadata["remaining_units_clamped"] is True
    assert snapshot.metadata["net_remaining_units"] == {
        "credits": Decimal("-0.50"),
        "total_tokens": -30,
    }
    assert snapshot.metadata["overdrawn_units"] == {
        "credits": Decimal("0.50"),
        "total_tokens": 30,
    }
    assert runtime_balance.has_units({"credits": Decimal("0.01")}) == (False, "credits")
    assert json.loads(statement.params["remaining_units_jsonb"]) == {}
    assert json.loads(statement.params["metadata_json"])["overdrawn_units"] == {
        "credits": "0.50",
        "total_tokens": 30,
    }


def test_billing_customer_and_export_sql_preserve_local_replay_keys() -> None:
    customer = BillingCustomer(
        id="bc_alice",
        workspace_id="ws_alice",
        external_customer_id="cus_123",
        external_subscription_id="sub_123",
        subscription_status_snapshot={"status": "active"},
    )
    export = billing_export_event_from_usage_event(
        _usage_event(),
        billing_customer_id="bc_alice",
        price_book_id="pb_2026_05",
        price_book_version="2026-05",
        external_meter_key="ai_usage",
    )
    sent = mark_billing_export_replay(export, replay_status="succeeded", provider_event_id="evt_polar_123")

    customer_statement = upsert_billing_customer_sql(customer)
    export_statement = upsert_billing_export_sql(sent)
    polar_event = sent.to_polar_event()

    assert "UNIQUE(workspace_id, provider)" not in customer_statement.sql
    assert "ON CONFLICT (workspace_id, provider) DO UPDATE" in customer_statement.sql
    assert json.loads(customer_statement.params["subscription_status_snapshot_jsonb"]) == {"status": "active"}
    assert "ON CONFLICT (provider, source_event_dedupe_key) DO UPDATE" in export_statement.sql
    assert export_statement.params["id"] == export.idempotency_key
    assert export_statement.params["source_event_dedupe_key"] == "polar:ws_alice:evt_usage_1:voyage.embed_documents"
    assert export_statement.params["external_event_id"] == "evt_polar_123"
    assert polar_event.metadata["billing_export_idempotency_key"] == export.idempotency_key
    assert polar_event.metadata["source_event_dedupe_key"] == "polar:ws_alice:evt_usage_1:voyage.embed_documents"
    assert polar_event.metadata["external_meter_key"] == "ai_usage"
    assert polar_event.external_id == "polar:ws_alice:evt_usage_1:voyage.embed_documents"


def test_billing_debug_snapshot_sql_reads_usage_and_export_state_without_authorizing() -> None:
    statement = billing_debug_snapshot_sql(workspace_id="ws_alice", limit=5, operation="voyage.embed_documents")

    assert "WITH recent_reservations AS" in statement.sql
    assert "FROM usage_reservations AS reservation" in statement.sql
    assert "LEFT JOIN job_operations AS job_operation" in statement.sql
    assert "FROM usage_events AS usage_event" in statement.sql
    assert "FROM billing_exports AS billing_export" in statement.sql
    assert "source_event_dedupe_key" in statement.sql
    assert "external_event_id" in statement.sql
    assert "%(operation)s::text IS NULL" in statement.sql
    assert statement.params == {
        "workspace_id": "ws_alice",
        "operation": "voyage.embed_documents",
        "limit": 5,
    }


def test_billing_debug_snapshot_maps_denied_decision_events_and_replay_status() -> None:
    created_at = datetime(2026, 5, 26, 4, 0, tzinfo=timezone.utc)
    row = {
        "reservation_id": "res_denied",
        "workspace_id": "ws_alice",
        "job_id": "job_1",
        "job_status": "retry_wait",
        "job_error_code": "usage_limit_exceeded",
        "job_error_message": "Fallback paused by policy.",
        "operation_id": "op_1",
        "job_operation": "gemini.transcribe_media",
        "operation_status": "denied",
        "video_id": "vid_1",
        "subject": "gemini",
        "operation": "transcribe_media",
        "operation_key": "gemini.transcribe_media",
        "allocation_id": "alloc_gemini_fallback",
        "credential_mode": "hosted",
        "reservation_status": "denied",
        "decision_json": json.dumps(
            {
                "allowed": False,
                "reason": "usage_limit_exceeded",
                "message": "Estimated media_seconds exceeds the operation limit.",
            }
        ),
        "estimated_units_json": json.dumps({"media_seconds": 14_400}),
        "idempotency_key": "ws_alice:vid_1:gemini.transcribe_media:h_media",
        "created_at": created_at,
        "metadata_json": json.dumps({"estimate_method": "duration_seconds"}),
        "usage_events_json": json.dumps(
            [
                {
                    "id": "evt_denied",
                    "event_type": "reservation_created",
                    "status": "denied",
                    "actual_units": {},
                    "error_code": "usage_limit_exceeded",
                    "provider_request_id": None,
                    "created_at": created_at.isoformat(),
                    "metadata": {"message": "policy denied"},
                }
            ]
        ),
        "billing_exports_json": json.dumps(
            [
                {
                    "id": "bill_evt_denied",
                    "usage_event_id": "evt_denied",
                    "provider": "polar",
                    "replay_status": "skipped",
                    "external_customer_id": "ws_alice",
                    "customer_id": None,
                    "external_meter_key": "ai_usage",
                    "external_event_id": None,
                    "source_event_dedupe_key": "polar:evt_denied:gemini.transcribe_media",
                    "attempt_count": 0,
                    "last_error": {},
                    "exported_at": None,
                    "updated_at": created_at.isoformat(),
                }
            ]
        ),
    }

    mapped = billing_debug_reservation_from_row(row)
    snapshot = billing_debug_snapshot_from_rows([row], workspace_id="ws_alice", limit=20)

    assert mapped.reservation_status == "denied"
    assert mapped.entitlement_decision["reason"] == "usage_limit_exceeded"
    assert mapped.usage_events[0].error_code == "usage_limit_exceeded"
    assert mapped.billing_exports[0].replay_status == "skipped"
    assert mapped.billing_exports[0].source_event_dedupe_key == "polar:evt_denied:gemini.transcribe_media"
    assert snapshot.rows == (mapped,)


def test_polar_webhook_snapshot_sql_is_idempotent_and_keeps_customer_state() -> None:
    payload = {
        "id": "wh_123",
        "type": "customer.state_changed",
        "timestamp": "2026-05-26T03:00:00Z",
        "data": {
            "customer": {"id": "cus_123", "external_id": "ws_alice"},
            "subscription": {"id": "sub_123", "status": "active"},
            "active_benefit_ids": ["ben_123"],
            "active_meters": [{"meter_id": "meter_123", "balance": 500}],
        },
    }

    snapshot = polar_webhook_snapshot_from_payload(payload)
    statement = upsert_polar_webhook_snapshot_sql(snapshot)

    assert snapshot.id == "polar_wh_wh_123"
    assert snapshot.payload_hash == payload_sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    assert snapshot.workspace_id == "ws_alice"
    assert snapshot.external_customer_id == "cus_123"
    assert snapshot.external_subscription_id == "sub_123"
    assert snapshot.customer_state_snapshot["active_meters"][0]["balance"] == 500
    assert "ON CONFLICT (id) DO UPDATE" in statement.sql
    assert statement.params["payload_hash"] == snapshot.payload_hash
    assert json.loads(statement.params["customer_state_snapshot_jsonb"])["active_benefit_ids"] == ["ben_123"]
    assert json.loads(statement.params["payload_jsonb"]) == payload


def test_standard_webhook_signature_accepts_raw_body_and_rejects_mutation() -> None:
    raw_body = b'{"type":"customer.state_changed","timestamp":"2026-05-26T03:00:00Z","data":{"id":"cus_123"}}'
    headers = _standard_webhook_headers(raw_body)

    webhook_id = verify_standard_webhook_signature(
        raw_body=raw_body,
        headers=headers,
        secret="polar-secret",
        now=1780000000,
    )

    assert webhook_id == "wh_msg_123"
    with pytest.raises(PolarWebhookVerificationError, match="webhook_signature_invalid"):
        verify_standard_webhook_signature(
            raw_body=raw_body + b"\n",
            headers=headers,
            secret="polar-secret",
            now=1780000000,
        )


def test_polar_order_paid_processing_creates_customer_and_deduped_credit_entry() -> None:
    raw_body = json.dumps(
        {
            "type": "order.paid",
            "timestamp": "2026-05-26T03:00:00Z",
            "data": {
                "id": "ord_123",
                "billing_reason": "purchase",
                "customer_id": "cus_123",
                "product_id": "prod_topup",
                "customer": {"id": "cus_123", "external_id": "ws_alice"},
                "metadata": {"yutome_credit_grants": [{"unit": "credits", "quantity": "12.50"}]},
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    payload = json.loads(raw_body)

    first = process_polar_webhook_payload(payload, raw_body=raw_body, webhook_event_id="msg_1")
    second = process_polar_webhook_payload(payload, raw_body=raw_body, webhook_event_id="msg_1")
    statements = polar_webhook_processing_statements(first)

    assert first.snapshot.webhook_event_id == "msg_1"
    assert first.snapshot.payload_hash == payload_sha256(raw_body)
    assert first.billing_customer is not None
    assert first.billing_customer.workspace_id == "ws_alice"
    assert first.credit_entries[0].idempotency_key == second.credit_entries[0].idempotency_key
    assert first.credit_entries[0].signed_units == {"credits": Decimal("12.50")}
    assert [statement.sql.split()[2] for statement in statements if statement.sql.startswith("INSERT INTO")] == [
        "polar_webhook_snapshots",
        "billing_customers",
        "credit_ledger_entries",
        "polar_webhook_snapshots",
    ]
    assert "ON CONFLICT (workspace_id, idempotency_key) DO UPDATE" in statements[2].sql


def test_polar_order_paid_same_unit_grants_get_distinct_idempotency_keys() -> None:
    raw_body = json.dumps(
        {
            "type": "order.paid",
            "timestamp": "2026-05-26T03:00:00Z",
            "data": {
                "id": "ord_same_unit",
                "billing_reason": "purchase",
                "customer_id": "cus_123",
                "product_id": "prod_topup",
                "customer": {"id": "cus_123", "external_id": "ws_alice"},
                "metadata": {
                    "yutome_credit_grants": [
                        {"unit": "credits", "quantity": "10"},
                        {"unit": "credits", "quantity": "5"},
                    ]
                },
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    payload = json.loads(raw_body)

    first = process_polar_webhook_payload(payload, raw_body=raw_body, webhook_event_id="msg_same_unit")
    second = process_polar_webhook_payload(payload, raw_body=raw_body, webhook_event_id="msg_same_unit")

    assert [entry.signed_units for entry in first.credit_entries] == [{"credits": 10}, {"credits": 5}]
    assert len({entry.idempotency_key for entry in first.credit_entries}) == 2
    assert [entry.idempotency_key for entry in first.credit_entries] == [
        entry.idempotency_key for entry in second.credit_entries
    ]


def test_polar_subscription_and_customer_events_upsert_billing_customer() -> None:
    subscription_payload = {
        "type": "subscription.active",
        "timestamp": "2026-05-26T03:00:00Z",
        "data": {
            "id": "sub_123",
            "status": "active",
            "customer_id": "cus_123",
            "customer": {"id": "cus_123", "external_id": "ws_alice"},
        },
    }
    customer_payload = {
        "type": "customer.state_changed",
        "timestamp": "2026-05-26T03:01:00Z",
        "data": {
            "id": "cus_123",
            "external_id": "ws_alice",
            "active_subscriptions": [{"id": "sub_123"}],
            "active_meters": [{"name": "ai_credits", "balance": 1200}],
        },
    }

    subscription = process_polar_webhook_payload(subscription_payload)
    customer = process_polar_webhook_payload(customer_payload)

    assert subscription.billing_customer is not None
    assert subscription.billing_customer.external_subscription_id == "sub_123"
    assert subscription.billing_customer.status == "active"
    assert customer.billing_customer is not None
    assert customer.billing_customer.subscription_status_snapshot["active_meters"][0]["balance"] == 1200


def test_balance_reconciliation_from_credit_usage_and_reserved_rows() -> None:
    period_start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    period_end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    snapshot = derive_workspace_balance_snapshot_from_rows(
        workspace_id="ws_alice",
        entitlement_policy_id="policy_pro",
        period_start_at=period_start,
        period_end_at=period_end,
        credit_rows=(
            {
                "id": "cred_1",
                "workspace_id": "ws_alice",
                "idempotency_key": "cred_1",
                "direction": "grant",
                "unit": "credits",
                "quantity_text": "10",
                "occurred_at": period_start,
            },
        ),
        usage_rows=(
            {
                "id": "evt_1",
                "actual_units_json": json.dumps({"credits": "2.5", "ignored": "label"}),
            },
        ),
        reserved_rows=(
            {
                "id": "res_1",
                "estimated_units_json": json.dumps({"credits": "1.5"}),
            },
        ),
        starting_units={"credits": Decimal("1")},
    )
    statement = balance_reconciliation_input_sql(
        workspace_id="ws_alice",
        period_start_at=period_start,
        period_end_at=period_end,
    )

    assert snapshot.used_units == {"credits": Decimal("2.5")}
    assert snapshot.reserved_units == {"credits": Decimal("1.5")}
    assert snapshot.remaining_units == {"credits": Decimal("7.0")}
    assert "FROM credit_ledger_entries" in statement.sql
    assert "FROM usage_events" in statement.sql
    assert "FROM usage_reservations" in statement.sql


def test_billing_export_claim_and_finish_sql_marks_rows() -> None:
    now = datetime(2026, 5, 26, 4, 0, tzinfo=timezone.utc)

    claim = claim_billing_exports_sql(lease_owner="worker-1", now=now, limit=5)
    success = finish_billing_export_sql(
        export_id="bill_1",
        now=now,
        replay_status="succeeded",
        external_event_id="polar:evt_1:voyage.embed_documents",
    )
    failure = finish_billing_export_sql(
        export_id="bill_2",
        now=now,
        replay_status="failed",
        error_code="polar_unavailable",
        error_message="timeout",
    )

    assert "FOR UPDATE SKIP LOCKED" in claim.sql
    assert "status = 'processing'" in claim.sql
    assert claim.params["lease_owner"] == "worker-1"
    assert success.params["status"] == "succeeded"
    assert success.params["external_event_id"] == "polar:evt_1:voyage.embed_documents"
    assert json.loads(failure.params["last_error_jsonb"]) == {
        "code": "polar_unavailable",
        "message": "timeout",
    }


def test_billing_export_event_from_row_posts_polar_ingest_shape() -> None:
    row = {
        "id": "bill_1",
        "workspace_id": "ws_alice",
        "usage_event_id": "evt_1",
        "reservation_id": "res_1",
        "provider": "polar",
        "event_name": "yutome.voyage.embed_documents",
        "export_units_jsonb": json.dumps({"total_tokens": 91}),
        "source_event_dedupe_key": "polar:evt_1:voyage.embed_documents",
        "status": "processing",
        "authorization_effect": "none",
        "external_customer_id": "ws_alice",
        "customer_id": None,
        "event_timestamp": datetime(2026, 5, 26, 3, 0, tzinfo=timezone.utc),
        "attempt_count": 1,
        "metadata_json": json.dumps({"operation_key": "voyage.embed_documents"}),
    }

    export = billing_export_event_from_row(row)
    payload = export.to_polar_export().model_dump(mode="json")

    assert payload["events"][0]["name"] == "yutome.voyage.embed_documents"
    assert payload["events"][0]["external_customer_id"] == "ws_alice"
    assert payload["events"][0]["metadata"]["total_tokens"] == 91
    assert payload["events"][0]["external_id"] == "polar:ws_alice:evt_1:voyage.embed_documents"


def test_billing_export_event_from_row_parses_workspace_scoped_dedupe_key_without_metadata() -> None:
    row = {
        "id": "bill_1",
        "workspace_id": "ws_alice",
        "usage_event_id": "evt_1",
        "reservation_id": "res_1",
        "provider": "polar",
        "event_name": "yutome.fallback",
        "export_units_jsonb": json.dumps({"total_tokens": 91}),
        "source_event_dedupe_key": "polar:ws_alice:evt_1:voyage.embed_documents",
        "status": "processing",
        "authorization_effect": "none",
        "external_customer_id": "ws_alice",
        "customer_id": None,
        "event_timestamp": datetime(2026, 5, 26, 3, 0, tzinfo=timezone.utc),
        "attempt_count": 1,
        "metadata_json": json.dumps({}),
    }

    export = billing_export_event_from_row(row)

    assert export.operation_key == "voyage.embed_documents"
