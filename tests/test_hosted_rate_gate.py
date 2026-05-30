from __future__ import annotations

from yutome.hosted.rate_gate import RateDecision, RateGate


def test_token_bucket_allows_up_to_capacity_then_denies() -> None:
    gate = RateGate()

    decisions = [gate.check("ws:alpha", requests_per_minute=2, now=0.0) for _ in range(2)]
    denied = gate.check("ws:alpha", requests_per_minute=2, now=0.0)

    assert [decision.allowed for decision in decisions] == [True, True]
    assert denied.allowed is False
    assert denied.reason == "rate_limited"
    assert denied.retry_after_seconds is not None
    assert denied.retry_after_seconds >= 1


def test_token_bucket_refills_over_time() -> None:
    gate = RateGate()

    gate.check("ws:alpha", requests_per_minute=2, now=0.0)
    gate.check("ws:alpha", requests_per_minute=2, now=0.0)
    denied = gate.check("ws:alpha", requests_per_minute=2, now=0.0)
    refilled = gate.check("ws:alpha", requests_per_minute=2, now=60.0)

    assert denied.allowed is False
    assert denied.remaining == 0
    assert refilled.allowed is True
    assert refilled.remaining > denied.remaining


def test_zero_or_negative_rpm_is_unlimited() -> None:
    gate = RateGate()

    decisions = [gate.check("ws:alpha", requests_per_minute=0, now=0.0) for _ in range(5)]
    negative = gate.check("ws:alpha", requests_per_minute=-1, now=0.0)

    assert all(decision.allowed for decision in decisions)
    assert negative.allowed is True


def test_bucket_map_evicts_least_recently_used_keys_at_size_cap() -> None:
    gate = RateGate(max_keys=3)

    for key in ("key:a", "key:b", "key:c"):
        gate.check(key, requests_per_minute=10, now=0.0)
    gate.check("key:a", requests_per_minute=10, now=1.0)
    gate.check("key:d", requests_per_minute=10, now=2.0)

    assert len(gate._buckets) == 3
    assert set(gate._buckets) == {"key:a", "key:c", "key:d"}


def test_rate_decision_shape_mirrors_usage_decision() -> None:
    decision = RateDecision(allowed=True, limit=120, remaining=119, reset_seconds=1)

    assert decision.allowed is True
    assert decision.reason == "allowed"
    assert decision.message is None
    assert decision.limit == 120
    assert decision.remaining == 119
    assert decision.reset_seconds == 1
    assert decision.retry_after_seconds is None
