from __future__ import annotations

import re
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
    message = redact_sensitive_failure_text(message)
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


_URL_USERINFO_RE = re.compile(r"([a-z][a-z0-9+.-]*://)([^/@\s:]+):([^/@\s]+)@")
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|password|passwd|pwd|secret|token|access[_-]?token)\s*([=:])\s*([^\s,&;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{8,})")
_PROVIDER_TOKEN_RE = re.compile(
    r"\b("
    r"sk-[A-Za-z0-9_-]{8,}|"
    r"pa-[A-Za-z0-9_-]{8,}|"
    r"AIza[0-9A-Za-z_-]{12,}|"
    r"[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"
    r")\b"
)


def redact_sensitive_failure_text(message: str) -> str:
    """Remove common credential shapes before persisting provider errors."""

    if not message:
        return message
    redacted = _URL_USERINFO_RE.sub(r"\1***:***@", message)
    redacted = _BEARER_RE.sub("Bearer ***", redacted)
    redacted = _CREDENTIAL_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}***", redacted)
    return _PROVIDER_TOKEN_RE.sub("***", redacted)
