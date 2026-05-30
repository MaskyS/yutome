from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from psycopg.types.json import Jsonb
from yutome.hosted.billing import (
    CREDITS_METER_EVENT_NAME,
    EntitlementPolicyRecord,
    PriceBook,
    PriceBookProduct,
    ProductLimit,
    StripeCustomer,
    StripeWebhookVerificationError,
    WorkspaceBalanceSnapshot,
    balance_reconciliation_input_sql,
    billing_debug_reservation_from_row,
    billing_debug_snapshot_from_rows,
    billing_debug_snapshot_sql,
    billing_schema_statements,
    claim_stripe_meter_exports_sql,
    credits_from_billable_units,
    derive_workspace_balance_snapshot,
    finish_stripe_meter_export_sql,
    included_allowance_credits,
    mark_stripe_meter_export_replay,
    overage_credits_for_event,
    process_stripe_webhook_event,
    provision_starter_entitlement_statements,
    stripe_meter_event_payload,
    stripe_meter_export_event_from_row,
    stripe_meter_export_idempotency_key,
    stripe_meter_exports_from_usage_event,
    stripe_webhook_event_from_payload,
    stripe_webhook_processing_statements,
    upsert_entitlement_policy_sql,
    upsert_price_book_sql,
    upsert_stripe_customer_sql,
    upsert_stripe_meter_export_sql,
    upsert_stripe_webhook_event_sql,
    upsert_workspace_balance_sql,
    verify_stripe_webhook_signature,
)
from yutome.hosted.gate import UsageGate
from yutome.hosted.models import EntitlementPolicy, ProviderAllocation, UsageEvent, WorkspaceBalance


def _jsonb_obj(value: object) -> object:
    assert isinstance(value, Jsonb)
    return value.obj


def _usage_event(**overrides: object) -> UsageEvent:
    values = {
        "id": "evt_usage_1",
        "reservation_id": "res_1",
        "workspace_id": "ws_alice",
        "subject": "voyage",
        "operation": "embed_documents",
        "event_type": "provider_attempt_succeeded",
        "status": "succeeded",
        "actual_units": {"total_tokens": 1_000_000, "vectors": 2, "ignored_label": "warm"},
        "provider_request_id": "req_1",
        "created_at": datetime(2026, 5, 26, 3, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return UsageEvent(**values)  # type: ignore[arg-type]


def _stripe_signature_header(raw_body: bytes, *, secret: str = "whsec_test", timestamp: int | None = None) -> str:
    ts = str(timestamp if timestamp is not None else int(time.time()))
    signature = hmac.new(secret.encode("utf-8"), ts.encode("utf-8") + b"." + raw_body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={signature}"


def test_price_book_models_product_limits_without_authorizing_usage() -> None:
    limit = ProductLimit(
        product_code="metered",
        operation_key="voyage.embed_documents",
        unit="total_tokens",
        included_quantity=1_000_000,
        hard_limit=2_000_000,
        meter_event_name=CREDITS_METER_EVENT_NAME,
    )
    product = PriceBookProduct(code="metered", name="Metered", stripe_price_id="price_123", limits=(limit,))
    price_book = PriceBook(id="pb_2026_05", version="2026-05", products=(product,))

    assert price_book.product("metered") == product
    assert product.limits_for_operation("voyage.embed_documents") == (limit,)
    assert "allowed" not in price_book.model_dump()


def test_credits_meter_collapses_billable_units_and_drops_internal_units() -> None:
    # total_tokens=1_000_000 * 5e-5 = 50; vectors=2 * 7e-3 = 0.014; candidate_limit is not billable.
    credits = credits_from_billable_units({"total_tokens": 1_000_000, "vectors": 2, "candidate_limit": 50})

    assert credits == Decimal("50.01400")


def test_retuned_weights_make_one_credit_about_one_indexed_video_hour() -> None:
    # ~20-min video on the common index path: ~5_700 total_tokens + ~8 vectors.
    per_video = credits_from_billable_units({"total_tokens": 5_700, "vectors": 8})
    assert per_video == Decimal("0.34100")  # 5_700*5e-5 + 8*7e-3
    # ~3 such videos make one video-hour -> ~1 credit per video-hour.
    assert per_video * 3 == Decimal("1.02300")


def test_included_allowance_credits_ignores_non_billable_and_overdrawn_units() -> None:
    # Only billable units contribute; candidate_limit (quota) is dropped; a negative
    # (overdrawn) billable unit contributes 0, never a negative credit.
    credits = included_allowance_credits(
        {"total_tokens": 100_000, "vectors": 5, "candidate_limit": 9_999, "queries": Decimal("-3")}
    )
    assert credits == Decimal("5.03500")  # 100_000*5e-5 + 5*7e-3 + 0 (queries clamped)


def test_overage_credits_meters_only_the_portion_beyond_the_included_allowance() -> None:
    # Allowance fully covers the event -> nothing metered.
    assert overage_credits_for_event(Decimal("4"), included_remaining_credits=Decimal("10")) == Decimal("0")
    # Allowance partially covers it -> meter exactly the uncovered remainder.
    assert overage_credits_for_event(Decimal("8"), included_remaining_credits=Decimal("6")) == Decimal("2")
    # Allowance already exhausted (0 or overdrawn) -> meter the whole event.
    assert overage_credits_for_event(Decimal("5"), included_remaining_credits=Decimal("0")) == Decimal("5")
    assert overage_credits_for_event(Decimal("5"), included_remaining_credits=Decimal("-3")) == Decimal("5")


def test_meter_export_within_included_allowance_enqueues_nothing() -> None:
    event = _usage_event(actual_units={"total_tokens": 1_000, "vectors": 1})
    event_credits = credits_from_billable_units(event.actual_units)  # 0.057
    # Allowance still free comfortably exceeds the event credits.
    override = overage_credits_for_event(event_credits, included_remaining_credits=Decimal("10"))

    assert override == Decimal("0")
    assert stripe_meter_exports_from_usage_event(event, stripe_customer_id="cus_1", credits_override=override) == ()


def test_meter_export_beyond_included_allowance_meters_only_the_excess() -> None:
    event = _usage_event(actual_units={"total_tokens": 1_000_000, "vectors": 2})  # 50.014 credits
    event_credits = credits_from_billable_units(event.actual_units)
    override = overage_credits_for_event(event_credits, included_remaining_credits=Decimal("0.014"))

    exports = stripe_meter_exports_from_usage_event(event, stripe_customer_id="cus_1", credits_override=override)
    assert len(exports) == 1
    export = exports[0]
    # 50.014 event credits - 0.014 free allowance = 50 metered overage.
    assert override == Decimal("50.00000")
    # value sent to Stripe is the canonicalized decimal.
    assert export.value_decimal == Decimal("50")
    assert export.metadata["event_credits"] == "50.01400"
    assert export.metadata["metered_credits"] == "50.00000"


def test_meter_export_is_one_row_with_stable_deterministic_identifier() -> None:
    event = _usage_event()

    left = stripe_meter_exports_from_usage_event(event, stripe_customer_id="cus_1")
    right = stripe_meter_exports_from_usage_event(event, stripe_customer_id="cus_1")

    assert len(left) == 1
    export = left[0]
    assert export.idempotency_key == right[0].idempotency_key
    assert export.idempotency_key == stripe_meter_export_idempotency_key(event)
    assert export.idempotency_key == "stripe:ws_alice:evt_usage_1:credits"
    assert export.replay_status == "pending"
    assert export.meter_unit == "credits"
    assert export.event_name == CREDITS_METER_EVENT_NAME
    # total_tokens=1_000_000*5e-5 + vectors=2*7e-3 = 50.014
    assert export.value_decimal == Decimal("50.01400")


def test_meter_event_payload_shape_uses_exact_decimal_string() -> None:
    event = _usage_event(actual_units={"media_seconds": Decimal("123.5")})

    export = stripe_meter_exports_from_usage_event(event, stripe_customer_id="cus_99")[0]
    payload = stripe_meter_event_payload(export)

    assert payload["event_name"] == CREDITS_METER_EVENT_NAME
    assert payload["payload"]["stripe_customer_id"] == "cus_99"
    # media_seconds 123.5 * 3e-4 = 0.03705 — exact, no float truncation.
    assert payload["payload"]["value"] == "0.03705"
    # identifier is the compact hash of the dedupe key (Stripe caps identifier at 100 chars).
    assert payload["identifier"] == export.stripe_identifier
    assert payload["identifier"].startswith("me_") and len(payload["identifier"]) <= 100
    assert payload["timestamp"] == int(event.created_at.timestamp())


def test_meter_event_identifier_stays_within_stripe_100_char_limit() -> None:
    # Production usage_event ids are evt_<64 hex>, so the readable source_event_dedupe_key
    # runs ~111 chars — over Stripe's 100-char meter_event.identifier cap, which would 400
    # every meter POST. Guard that the value actually sent is compact, deterministic, 1:1.
    from yutome.hosted.ids import input_hash

    realistic_id = input_hash({"seed": 1}, prefix="evt")
    assert len(realistic_id) > 60  # evt_ + 64 hex
    export = stripe_meter_exports_from_usage_event(_usage_event(id=realistic_id), stripe_customer_id="cus_1")[0]
    identifier = stripe_meter_event_payload(export)["identifier"]

    assert len(identifier) <= 100
    assert identifier == export.stripe_identifier  # deterministic, 1:1 with the dedupe key
    other = stripe_meter_exports_from_usage_event(
        _usage_event(id=input_hash({"seed": 2}, prefix="evt")), stripe_customer_id="cus_1"
    )[0]
    assert stripe_meter_event_payload(other)["identifier"] != identifier


def test_meter_export_skips_when_no_billable_units_present() -> None:
    event = _usage_event(actual_units={"candidate_limit": 100, "request_count": 4})

    assert stripe_meter_exports_from_usage_event(event, stripe_customer_id="cus_1") == ()


def test_meter_export_marks_unsettled_events_skipped() -> None:
    denied = _usage_event(
        id="evt_denied_1",
        event_type="reservation_created",
        status="denied",
        actual_units={"total_tokens": 1_000_000},
        provider_request_id=None,
        error_code="insufficient_balance",
    )

    export = stripe_meter_exports_from_usage_event(denied, stripe_customer_id="cus_1")[0]

    assert export.replay_status == "skipped"


def test_meter_export_rejects_negative_usage_units() -> None:
    event = _usage_event(
        id="evt_credit_1",
        event_type="usage_credit_released",
        status="released",
        actual_units={"total_tokens": -25, "human_note": "retry credit"},
    )

    with pytest.raises(ValueError, match="Negative billing unit"):
        stripe_meter_exports_from_usage_event(event, stripe_customer_id="cus_1")


def test_failed_meter_export_replay_does_not_change_usage_gate_decision() -> None:
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
    export = stripe_meter_exports_from_usage_event(_usage_event(), stripe_customer_id="cus_1")[0]
    failed = mark_stripe_meter_export_replay(
        export,
        replay_status="failed",
        error_code="stripe_meter_event_failed",
        error_message="Stripe meter event POST timed out",
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
    assert failed.last_error_code == "stripe_meter_event_failed"
    assert after.status == "reserved"
    assert after.decision.allowed is True


def test_stripe_webhook_signature_accepts_valid_and_rejects_tampered_or_missing() -> None:
    raw_body = b'{"id":"evt_1","type":"checkout.session.completed","data":{"object":{}}}'
    header = _stripe_signature_header(raw_body)

    verify_stripe_webhook_signature(raw_body=raw_body, header=header, secret="whsec_test")

    with pytest.raises(StripeWebhookVerificationError, match="webhook_signature_invalid"):
        verify_stripe_webhook_signature(
            raw_body=raw_body,
            header=header.rsplit("=", 1)[0] + "=deadbeef",
            secret="whsec_test",
        )
    with pytest.raises(StripeWebhookVerificationError, match="webhook_signature_missing"):
        verify_stripe_webhook_signature(raw_body=raw_body, header=None, secret="whsec_test")
    with pytest.raises(StripeWebhookVerificationError, match="webhook_signature_invalid"):
        verify_stripe_webhook_signature(raw_body=raw_body, header=header, secret="whsec_other")


def test_stripe_webhook_signature_rejects_expired_timestamp() -> None:
    raw_body = b'{"id":"evt_1","type":"customer.subscription.updated","data":{"object":{}}}'
    header = _stripe_signature_header(raw_body, timestamp=1_000)

    with pytest.raises(StripeWebhookVerificationError, match="webhook_timestamp_outside_tolerance"):
        verify_stripe_webhook_signature(raw_body=raw_body, header=header, secret="whsec_test", now=2_000_000)


def test_stripe_webhook_event_extracts_workspace_from_metadata() -> None:
    payload = {
        "id": "evt_123",
        "type": "checkout.session.completed",
        "created": 1779753600,
        "data": {
            "object": {
                "customer": "cus_123",
                "subscription": "sub_123",
                "status": "complete",
                "metadata": {"workspace_id": "ws_http"},
            }
        },
    }

    event = stripe_webhook_event_from_payload(payload)

    assert event.id == "evt_123"
    assert event.type == "checkout.session.completed"
    assert event.workspace_id == "ws_http"


def test_stripe_webhook_processing_upserts_customer_and_finalizes_exactly_once() -> None:
    payload = {
        "id": "evt_456",
        "type": "customer.subscription.updated",
        "created": 1779753600,
        "data": {
            "object": {
                "id": "sub_456",
                "customer": "cus_456",
                "status": "past_due",
                "metadata": {"workspace_id": "ws_http"},
            }
        },
    }

    result = process_stripe_webhook_event(payload)
    statements = stripe_webhook_processing_statements(result)

    assert result.ignored is False
    assert result.stripe_customer is not None
    assert result.stripe_customer.subscription_status == "past_due"
    assert result.stripe_customer.status == "paused"
    # snapshot insert (exactly-once via PK), customer upsert, workspace status mirror, finalize.
    assert len(statements) == 4
    # The subscription lifecycle is mirrored onto the workspace so the entitlement layer can
    # compute trial-expiry read-only.
    workspace_stmt = next(stmt for stmt in statements if "UPDATE workspaces" in stmt.sql)
    assert workspace_stmt.params["workspace_id"] == "ws_http"
    assert workspace_stmt.params["subscription_status"] == "past_due"
    # The webhook snapshot is keyed by the Stripe event id, so a replay is a PK conflict
    # that does nothing destructive.
    snapshot_stmt = upsert_stripe_webhook_event_sql(result.event)
    assert "ON CONFLICT (id) DO UPDATE" in snapshot_stmt.sql
    assert "evt_456" in snapshot_stmt.params.values()


def test_stripe_webhook_ignores_unrelated_event_types() -> None:
    payload = {
        "id": "evt_789",
        "type": "invoice.paid",
        "created": 1779753600,
        "data": {"object": {"customer": "cus_789"}},
    }

    result = process_stripe_webhook_event(payload)

    assert result.ignored is True
    assert result.stripe_customer is None


def test_active_subscription_webhook_provisions_entitlement_policy_and_balance() -> None:
    # A workspace that subscribes (or renews into active/trialing) must come out of the webhook
    # with a usable EntitlementPolicy + seeded WorkspaceBalance for the current period, even if
    # it never bootstrapped an account first.
    payload = {
        "id": "evt_active",
        "type": "customer.subscription.created",
        "created": 1779753600,
        "data": {
            "object": {
                "id": "sub_active",
                "customer": "cus_active",
                "status": "active",
                "metadata": {"workspace_id": "ws_pay"},
            }
        },
    }

    result = process_stripe_webhook_event(payload)
    statements = stripe_webhook_processing_statements(result)
    sqls = [statement.sql for statement in statements]

    assert result.stripe_customer is not None
    assert result.stripe_customer.subscription_status == "active"
    policy_stmt = next(stmt for stmt in statements if "INSERT INTO entitlement_policies" in stmt.sql)
    balance_stmt = next(stmt for stmt in statements if "INSERT INTO workspace_balances" in stmt.sql)
    assert "ws_pay" in policy_stmt.params.values()
    assert "ws_pay" in balance_stmt.params.values()
    # Provisioning seeds the price book the starter policy references, so the FK holds on a
    # never-bootstrapped workspace.
    assert any("INSERT INTO price_books" in sql for sql in sqls)
    # The starter included allowance lands in the seeded balance.
    assert _jsonb_obj(balance_stmt.params["remaining_units_jsonb"])["total_tokens"] == 430000


def test_trialing_subscription_webhook_provisions_entitlement() -> None:
    # Stripe `trialing` normalizes to the entitled `active` status, so a card-gated trial seat is
    # also provisioned.
    payload = {
        "id": "evt_trialing",
        "type": "customer.subscription.updated",
        "created": 1779753600,
        "data": {
            "object": {
                "id": "sub_trial",
                "customer": "cus_trial",
                "status": "trialing",
                "metadata": {"workspace_id": "ws_trial"},
            }
        },
    }

    statements = stripe_webhook_processing_statements(process_stripe_webhook_event(payload))

    assert any("INSERT INTO entitlement_policies" in stmt.sql for stmt in statements)
    assert any("INSERT INTO workspace_balances" in stmt.sql for stmt in statements)


def test_non_entitled_subscription_webhook_does_not_provision_entitlement() -> None:
    # past_due / canceled events mirror the status onto the workspace but must NOT seed a fresh
    # balance (that would silently re-credit an unpaid workspace).
    payload = {
        "id": "evt_pd",
        "type": "customer.subscription.updated",
        "created": 1779753600,
        "data": {
            "object": {
                "id": "sub_pd",
                "customer": "cus_pd",
                "status": "past_due",
                "metadata": {"workspace_id": "ws_pd"},
            }
        },
    }

    statements = stripe_webhook_processing_statements(process_stripe_webhook_event(payload))

    assert not any("INSERT INTO entitlement_policies" in stmt.sql for stmt in statements)
    assert not any("INSERT INTO workspace_balances" in stmt.sql for stmt in statements)
    # The subscription status is still mirrored onto the workspace row.
    assert any("UPDATE workspaces" in stmt.sql for stmt in statements)


def test_provision_starter_entitlement_statements_preserve_existing_balance_on_replay() -> None:
    # Idempotent: the balance upsert keeps the existing remaining_units_jsonb (the ledger owns
    # the live period), so a webhook replay never resets a mid-period balance.
    statements = provision_starter_entitlement_statements("ws_replay")

    balance_stmt = next(stmt for stmt in statements if "INSERT INTO workspace_balances" in stmt.sql)
    assert "ON CONFLICT" in balance_stmt.sql
    assert "remaining_units_jsonb = workspace_balances.remaining_units_jsonb" in balance_stmt.sql


def test_billing_schema_statements_cover_durable_stripe_tables() -> None:
    statements = billing_schema_statements()
    joined = "\n".join(statements)

    assert "CREATE TABLE IF NOT EXISTS price_books" in joined
    assert "CREATE TABLE IF NOT EXISTS entitlement_policies" in joined
    assert "CREATE TABLE IF NOT EXISTS workspace_balances" in joined
    assert "CREATE TABLE IF NOT EXISTS stripe_customers" in joined
    assert "CREATE TABLE IF NOT EXISTS stripe_meter_exports" in joined
    assert "CREATE TABLE IF NOT EXISTS stripe_webhook_events" in joined
    assert "UNIQUE(source_event_dedupe_key)" in joined
    assert "idx_stripe_meter_exports_replay" in joined
    assert "idx_stripe_webhook_events_replay" in joined
    assert "polar" not in joined.lower()
    assert "credit_ledger_entries" not in joined
    assert all(statement.endswith(";") for statement in statements)


def test_price_book_upsert_persists_products_and_unit_mapping() -> None:
    limit = ProductLimit(
        product_code="metered",
        operation_key="voyage.embed_documents",
        unit="total_tokens",
        included_quantity=1_000,
        hard_limit=2_000,
        meter_event_name=CREDITS_METER_EVENT_NAME,
    )
    price_book = PriceBook(
        id="pb_2026_05",
        version="2026-05",
        status="active",
        products=(PriceBookProduct(code="metered", name="Metered", limits=(limit,)),),
        unit_mapping={"voyage.embed_documents": {"total_tokens": "credits"}},
    )

    statement = upsert_price_book_sql(price_book)

    assert "INSERT INTO price_books" in statement.sql
    assert "ON CONFLICT (version) DO UPDATE" in statement.sql
    assert "active" in statement.params.values()
    products = _jsonb_obj(statement.params["products_jsonb"])
    assert products[0]["limits"][0]["meter_event_name"] == CREDITS_METER_EVENT_NAME


def test_entitlement_policy_and_balance_records_feed_usage_gate_independently_of_stripe() -> None:
    policy = EntitlementPolicyRecord(
        id="policy_metered",
        workspace_id="ws_alice",
        plan_key="metered",
        price_book_id="pb_2026_05",
        allowed_operations=("voyage.embed_documents",),
        included_units={"voyage.embed_documents": {"total_tokens": 1_000}},
        hard_limits={"voyage.embed_documents": {"total_tokens": 2_000}},
    )
    balance = WorkspaceBalanceSnapshot(
        workspace_id="ws_alice",
        entitlement_policy_id="policy_metered",
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
    assert ["voyage.embed_documents"] in policy_statement.params.values()
    assert runtime_policy.operation_allowed("voyage.embed_documents")
    assert runtime_balance.has_units({"total_tokens": 900}) == (True, None)


def test_decimal_balance_comparison_does_not_deny_exact_decimal_estimate() -> None:
    balance = WorkspaceBalance(workspace_id="ws_alice", remaining_units={"credits": Decimal("0.30")})

    assert balance.has_units({"credits": Decimal("0.10") + Decimal("0.20")}) == (True, None)


def test_workspace_balance_derives_from_starting_units_usage_and_reservations() -> None:
    snapshot = derive_workspace_balance_snapshot(
        workspace_id="ws_alice",
        entitlement_policy_id="policy_metered",
        period_start_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        period_end_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        starting_units={"credits": Decimal("1.00"), "total_tokens": 1_000},
        used_units={"credits": Decimal("0.40"), "total_tokens": 200},
        reserved_units={"credits": Decimal("0.10"), "total_tokens": 50},
    )

    statement = upsert_workspace_balance_sql(snapshot)

    assert snapshot.remaining_units == {"credits": Decimal("0.50"), "total_tokens": 750}
    assert snapshot.metadata["derived_from"]["starting_units"] == {"credits": Decimal("1.00"), "total_tokens": 1_000}
    remaining = _jsonb_obj(statement.params["remaining_units_jsonb"])
    assert remaining == {"credits": "0.50", "total_tokens": 750}


def test_workspace_balance_excludes_unprovisioned_telemetry_units() -> None:
    snapshot = derive_workspace_balance_snapshot(
        workspace_id="ws_alice",
        entitlement_policy_id="policy_metered",
        period_start_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        period_end_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        starting_units={"queries": 10_000, "total_tokens": 250_000},
        # Providers report telemetry units the plan does not provision; they must
        # not enter the balance.
        used_units={"queries": 1_240, "total_tokens": 60_000, "latency_ms": 999, "candidate_tokens": 5_000},
        reserved_units={"queries": 10, "query_vector_dimensions": 2_048_000},
    )

    assert set(snapshot.used_units) == {"queries", "total_tokens"}
    assert set(snapshot.reserved_units) == {"queries"}
    assert snapshot.remaining_units == {"queries": 8_750, "total_tokens": 190_000}
    assert "latency_ms" not in snapshot.remaining_units
    assert "candidate_tokens" not in snapshot.remaining_units
    assert "query_vector_dimensions" not in snapshot.remaining_units
    assert snapshot.metadata["untracked_units"] == ["candidate_tokens", "latency_ms", "query_vector_dimensions"]


def test_workspace_balance_overdraw_clamps_remaining_and_records_deficit() -> None:
    snapshot = derive_workspace_balance_snapshot(
        workspace_id="ws_alice",
        entitlement_policy_id="policy_metered",
        period_start_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        period_end_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        starting_units={"credits": Decimal("1.00"), "total_tokens": 100},
        used_units={"credits": Decimal("1.25"), "total_tokens": 125},
        reserved_units={"credits": Decimal("0.25"), "total_tokens": 5},
    )
    runtime_balance = snapshot.to_runtime_balance()

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


def test_stripe_customer_and_meter_export_sql_preserve_local_replay_keys() -> None:
    customer = StripeCustomer(
        id="sc_alice",
        workspace_id="ws_alice",
        stripe_customer_id="cus_123",
        stripe_subscription_id="sub_123",
        subscription_status="active",
        subscription_status_snapshot={"status": "active"},
    )
    export = stripe_meter_exports_from_usage_event(_usage_event(), stripe_customer_id="cus_123")[0]
    sent = mark_stripe_meter_export_replay(
        export, replay_status="succeeded", stripe_meter_event_identifier=export.idempotency_key
    )

    customer_statement = upsert_stripe_customer_sql(customer)
    export_statement = upsert_stripe_meter_export_sql(sent)
    payload = stripe_meter_event_payload(sent)

    assert "ON CONFLICT (workspace_id) DO UPDATE" in customer_statement.sql
    assert "provider" not in customer_statement.sql
    assert _jsonb_obj(customer_statement.params["subscription_status_snapshot_jsonb"]) == {"status": "active"}
    assert "ON CONFLICT (source_event_dedupe_key) DO UPDATE" in export_statement.sql
    assert export.idempotency_key in export_statement.params.values()
    assert "stripe:ws_alice:evt_usage_1:credits" in export_statement.params.values()
    # DB dedupe key stays readable; the value SENT to Stripe is the compact hash (<=100 chars).
    assert payload["identifier"] == export.stripe_identifier
    assert payload["identifier"].startswith("me_") and len(payload["identifier"]) <= 100


def test_meter_export_event_from_row_round_trips() -> None:
    created_at = datetime(2026, 5, 26, 3, 0, tzinfo=timezone.utc)
    row = {
        "id": "stripe:ws_alice:evt_usage_1:credits",
        "workspace_id": "ws_alice",
        "usage_event_id": "evt_usage_1",
        "reservation_id": "res_1",
        "stripe_customer_id": "cus_123",
        "meter_unit": "credits",
        "event_name": CREDITS_METER_EVENT_NAME,
        "value_text": "0.10002",
        "source_event_dedupe_key": "stripe:ws_alice:evt_usage_1:credits",
        "status": "processing",
        "stripe_meter_event_identifier": None,
        "attempt_count": 1,
        "event_timestamp": created_at,
        "metadata_json": {"operation_key": "voyage.embed_documents"},
    }

    export = stripe_meter_export_event_from_row(row)
    payload = stripe_meter_event_payload(export)

    assert export.value_decimal == Decimal("0.10002")
    assert export.stripe_customer_id == "cus_123"
    assert payload["payload"]["value"] == "0.10002"
    assert payload["identifier"] == export.stripe_identifier
    assert payload["identifier"].startswith("me_") and len(payload["identifier"]) <= 100


def test_claim_and_finish_meter_export_sql_stay_raw_locking_statements() -> None:
    now = datetime(2026, 5, 26, 4, 0, tzinfo=timezone.utc)

    claim = claim_stripe_meter_exports_sql(lease_owner="worker-1", now=now, limit=10)
    finish = finish_stripe_meter_export_sql(export_id="x", now=now, replay_status="succeeded")

    assert "FOR UPDATE SKIP LOCKED" in claim.sql
    assert "FROM stripe_meter_exports" in claim.sql
    assert "provider" not in claim.sql
    assert "UPDATE stripe_meter_exports" in finish.sql
    with pytest.raises(ValueError, match="terminal or retryable"):
        finish_stripe_meter_export_sql(export_id="x", now=now, replay_status="processing")


def test_balance_reconciliation_input_sql_reads_usage_and_reservations_only() -> None:
    statement = balance_reconciliation_input_sql(
        workspace_id="ws_alice",
        period_start_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        period_end_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )

    assert "FROM usage_events" in statement.sql
    assert "FROM usage_reservations" in statement.sql
    assert "credit_ledger_entries" not in statement.sql
    assert "'usage'" in statement.sql
    assert "'reservation'" in statement.sql


def test_billing_debug_snapshot_sql_reads_usage_and_export_state_without_authorizing() -> None:
    statement = billing_debug_snapshot_sql(workspace_id="ws_alice", limit=5, operation="voyage.embed_documents")

    assert "WITH recent_reservations AS" in statement.sql
    assert "FROM usage_reservations AS reservation" in statement.sql
    assert "LEFT JOIN job_operations AS job_operation" in statement.sql
    assert "FROM usage_events AS usage_event" in statement.sql
    assert "FROM stripe_meter_exports AS meter_export" in statement.sql
    assert "source_event_dedupe_key" in statement.sql
    assert "stripe_meter_event_identifier" in statement.sql
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
        "decision_json": {
            "allowed": False,
            "reason": "usage_limit_exceeded",
            "message": "Estimated media_seconds exceeds the operation limit.",
        },
        "estimated_units_json": {"media_seconds": 14_400},
        "idempotency_key": "ws_alice:vid_1:gemini.transcribe_media:h_media",
        "created_at": created_at,
        "metadata_json": {"estimate_method": "duration_seconds"},
        "usage_events_json": [
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
        ],
        "meter_exports_json": [
            {
                "id": "stripe:ws_alice:evt_denied:credits",
                "usage_event_id": "evt_denied",
                "replay_status": "skipped",
                "stripe_customer_id": None,
                "meter_unit": "credits",
                "value": "0",
                "stripe_meter_event_identifier": None,
                "source_event_dedupe_key": "stripe:ws_alice:evt_denied:credits",
                "attempt_count": 0,
                "last_error": {},
                "exported_at": None,
                "updated_at": created_at.isoformat(),
            }
        ],
    }

    mapped = billing_debug_reservation_from_row(row)
    snapshot = billing_debug_snapshot_from_rows([row], workspace_id="ws_alice", limit=20)

    assert mapped.reservation_status == "denied"
    assert mapped.entitlement_decision["reason"] == "usage_limit_exceeded"
    assert mapped.usage_events[0].error_code == "usage_limit_exceeded"
    assert mapped.meter_exports[0].replay_status == "skipped"
    assert mapped.meter_exports[0].source_event_dedupe_key == "stripe:ws_alice:evt_denied:credits"
    assert snapshot.rows == (mapped,)
