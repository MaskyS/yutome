from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


FailureKind = Literal["quota", "auth", "rate_limit", "transient", "invalid_request", "provider", "unknown"]


@dataclass(frozen=True)
class ProviderFailure:
    provider: str
    kind: FailureKind
    code: str
    retryable: bool
    message: str


def classify_provider_http_error(
    *,
    provider: str,
    status_code: int | None,
    message: str = "",
) -> ProviderFailure:
    text = message.lower()
    if status_code in {401, 403}:
        return ProviderFailure(provider=provider, kind="auth", code=f"http_{status_code}", retryable=False, message=message)
    if status_code == 402 or "payment required" in text or "quota" in text or "bandwidth" in text:
        return ProviderFailure(provider=provider, kind="quota", code=f"http_{status_code or 'quota'}", retryable=False, message=message)
    if status_code == 429 or "rate limit" in text or "too many requests" in text:
        return ProviderFailure(provider=provider, kind="rate_limit", code="http_429", retryable=True, message=message)
    if status_code is not None and 400 <= status_code < 500:
        return ProviderFailure(provider=provider, kind="invalid_request", code=f"http_{status_code}", retryable=False, message=message)
    if status_code is not None and status_code >= 500:
        return ProviderFailure(provider=provider, kind="transient", code=f"http_{status_code}", retryable=True, message=message)
    return ProviderFailure(provider=provider, kind="unknown", code="unknown", retryable=True, message=message)
