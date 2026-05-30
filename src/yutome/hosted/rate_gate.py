"""Process-local frequency gate for the hosted HTTP origin surface.

Buckets are capped with a least-recently-used eviction policy so caller-key
rotation cannot grow process memory without bound. The cap is intentionally
local to this in-memory gate; multi-replica deployments should keep using the
RateGate.check interface as the seam for a shared limiter backend.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from dataclasses import dataclass


DEFAULT_REQUESTS_PER_MINUTE = 120
DEFAULT_MAX_KEYS = 16_384


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    reason: str = "allowed"
    message: str | None = None
    limit: int = 0
    remaining: int = 0
    reset_seconds: int = 0
    retry_after_seconds: int | None = None


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class RateGate:
    """In-memory token-bucket limiter for the hosted HTTP origin surface."""

    def __init__(
        self,
        *,
        requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
        burst: int | None = None,
        max_keys: int = DEFAULT_MAX_KEYS,
    ) -> None:
        self.requests_per_minute = requests_per_minute
        self.burst = burst
        self.max_keys = max(1, max_keys)
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def check(self, key: str, *, requests_per_minute: int, now: float | None = None) -> RateDecision:
        if requests_per_minute <= 0:
            return RateDecision(allowed=True, limit=0, remaining=0, reset_seconds=0)

        capacity = self.burst if self.burst is not None and self.burst > 0 else requests_per_minute
        refill_rate = requests_per_minute / 60.0
        checked_at = time.monotonic() if now is None else now
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=float(capacity), last_refill=checked_at)
        else:
            self._buckets.move_to_end(key)
            elapsed = max(0.0, checked_at - bucket.last_refill)
            bucket.tokens = min(float(capacity), bucket.tokens + (elapsed * refill_rate))
            bucket.last_refill = checked_at

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            self._store_bucket(key, bucket)
            return RateDecision(
                allowed=True,
                limit=capacity,
                remaining=max(0, math.floor(bucket.tokens)),
                reset_seconds=_seconds_until_full(capacity=capacity, tokens=bucket.tokens, refill_rate=refill_rate),
            )

        self._store_bucket(key, bucket)
        retry_after_seconds = max(1, math.ceil((1.0 - bucket.tokens) / refill_rate))
        return RateDecision(
            allowed=False,
            reason="rate_limited",
            message="Too many requests.",
            limit=capacity,
            remaining=0,
            reset_seconds=_seconds_until_full(capacity=capacity, tokens=bucket.tokens, refill_rate=refill_rate),
            retry_after_seconds=retry_after_seconds,
        )

    def _store_bucket(self, key: str, bucket: _Bucket) -> None:
        self._buckets[key] = bucket
        self._buckets.move_to_end(key)
        while len(self._buckets) > self.max_keys:
            self._buckets.popitem(last=False)


def _seconds_until_full(*, capacity: int, tokens: float, refill_rate: float) -> int:
    if tokens >= capacity:
        return 0
    return max(1, math.ceil((capacity - tokens) / refill_rate))


# Multi-replica seam:
# The bucket dictionary is intentionally process-local and is expected to live on
# app.state for the single-process hosted HTTP app. It is not the search store
# (the canonical name for the corpus database in docs/hosted-glossary.md). When
# the hosted origin runs more than one replica, move the bucket state behind this
# RateGate.check interface to a shared substrate such as a Postgres
# UPDATE ... RETURNING window counter or a Cloudflare Durable Object keyed by
# token hash or trusted client IP.


__all__ = ["DEFAULT_MAX_KEYS", "DEFAULT_REQUESTS_PER_MINUTE", "RateDecision", "RateGate"]
