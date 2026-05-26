from __future__ import annotations

from typing import Any

from yutome.hosted.models import UsageNormalization


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "dict"):
        return value.dict()
    payload: dict[str, Any] = {}
    for name in dir(value):
        if name.startswith("_"):
            continue
        try:
            attr = getattr(value, name)
        except Exception:  # noqa: BLE001 - provider SDK objects vary.
            continue
        if not callable(attr):
            payload[name] = attr
    return payload


def _get(payload: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return default


def _number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_gemini_generate_content(response: Any, *, operation: str) -> UsageNormalization:
    payload = _mapping(response)
    usage = _mapping(_get(payload, "usageMetadata", "usage_metadata", default={}))
    candidates = _get(payload, "candidates", default=[]) or []
    units = {
        "request_count": 1,
        "candidate_count": len(candidates),
        "prompt_tokens": _number(_get(usage, "promptTokenCount", "prompt_token_count")),
        "cached_content_tokens": _number(_get(usage, "cachedContentTokenCount", "cached_content_token_count")),
        "candidate_tokens": _number(_get(usage, "candidatesTokenCount", "candidates_token_count")),
        "tool_use_prompt_tokens": _number(_get(usage, "toolUsePromptTokenCount", "tool_use_prompt_token_count")),
        "thoughts_tokens": _number(_get(usage, "thoughtsTokenCount", "thoughts_token_count")),
        "total_tokens": _number(_get(usage, "totalTokenCount", "total_token_count")),
    }
    service_tier = _get(usage, "serviceTier", "service_tier")
    if service_tier is not None:
        units["service_tier"] = service_tier
    return UsageNormalization(
        subject="gemini",
        operation=operation,
        actual_units=units,
        provider_request_id=_get(payload, "responseId", "response_id"),
        raw_usage={"usageMetadata": usage},
        metadata={
            "model_version": _get(payload, "modelVersion", "model_version"),
            "model_status": _get(payload, "modelStatus", "model_status"),
            "prompt_feedback": _get(payload, "promptFeedback", "prompt_feedback"),
        },
    )


def normalize_voyage_embeddings_response(
    response: Any,
    *,
    operation: str,
    input_type: str | None = None,
    output_dimension: int | None = None,
    output_dtype: str | None = None,
) -> UsageNormalization:
    payload = _mapping(response)
    usage = _mapping(_get(payload, "usage", default={}))
    embeddings = _get(payload, "embeddings", default=None)
    data = _get(payload, "data", default=None)
    vector_count = len(embeddings if embeddings is not None else data or [])
    units = {
        "request_count": 1,
        "total_tokens": _number(_get(usage, "total_tokens", "totalTokens", default=_get(payload, "total_tokens", "totalTokens"))),
        "vectors": vector_count,
    }
    if output_dimension is not None:
        units["output_dimension"] = output_dimension
    return UsageNormalization(
        subject="voyage",
        operation=operation,
        actual_units=units,
        raw_usage={"usage": usage},
        metadata={
            "model": _get(payload, "model"),
            "input_type": input_type,
            "output_dimension": output_dimension,
            "output_dtype": output_dtype,
        },
    )


def normalize_webshare_subuser(payload: dict[str, Any]) -> dict[str, Any]:
    proxy_limit = _get(payload, "proxy_limit")
    proxy_limit_gb = None if proxy_limit in {0, 0.0, "0", "0.0"} else _number(proxy_limit)
    return {
        "subuser_id": str(_get(payload, "id", default="")),
        "label": _get(payload, "label"),
        "proxy_limit_gb": proxy_limit_gb,
        "max_thread_count": _get(payload, "max_thread_count"),
        "bandwidth_use_start_date": _get(payload, "bandwidth_use_start_date"),
        "bandwidth_use_end_date": _get(payload, "bandwidth_use_end_date"),
        "raw": payload,
    }


def normalize_webshare_stats(payload: dict[str, Any], *, operation: str = "proxy_stats") -> UsageNormalization:
    units = {
        "bandwidth_bytes": _number(_get(payload, "bandwidth_total")),
        "requests_total": _number(_get(payload, "requests_total")),
        "requests_successful": _number(_get(payload, "requests_successful")),
        "requests_failed": _number(_get(payload, "requests_failed")),
        "number_of_proxies_used": _number(_get(payload, "number_of_proxies_used")),
    }
    return UsageNormalization(
        subject="webshare",
        operation=operation,
        actual_units=units,
        raw_usage=payload,
        metadata={
            "timestamp": _get(payload, "timestamp"),
            "is_projected": bool(_get(payload, "is_projected", default=False)),
            "error_reasons": _get(payload, "error_reasons", default=[]),
            "countries_used": _get(payload, "countries_used", default=[]),
        },
    )


def normalize_webshare_activity(payload: dict[str, Any], *, operation: str = "proxy_fetch") -> UsageNormalization:
    units = {
        "bytes": _number(_get(payload, "bytes")),
        "local_request_bytes": _number(_get(payload, "local_request_bytes")),
        "local_response_bytes": _number(_get(payload, "local_response_bytes")),
        "request_duration": _number(_get(payload, "request_duration")),
        "handshake_duration": _number(_get(payload, "handshake_duration")),
        "tunnel_duration": _number(_get(payload, "tunnel_duration")),
    }
    return UsageNormalization(
        subject="webshare",
        operation=operation,
        actual_units=units,
        raw_usage=payload,
        metadata={
            "timestamp": _get(payload, "timestamp"),
            "hostname": _get(payload, "hostname"),
            "domain": _get(payload, "domain"),
            "error_reason": _get(payload, "error_reason"),
            "error_reason_how_to_fix": _get(payload, "error_reason_how_to_fix"),
            "auth_username": _get(payload, "auth_username"),
            "byte_accounting_status": _get(payload, "byte_accounting_status"),
            "byte_accounting_basis": _get(payload, "byte_accounting_basis"),
            "provider_byte_accounting": _get(payload, "provider_byte_accounting"),
            "provider_bytes_exact_for_call": _get(payload, "provider_bytes_exact_for_call"),
        },
    )


def normalize_search_store_usage(
    *,
    operation: str,
    backend: str,
    index_profile_ref: str | None = None,
    units: dict[str, float | int | str | bool | None] | None = None,
    metadata: dict[str, Any] | None = None,
) -> UsageNormalization:
    return UsageNormalization(
        subject="search_store",
        operation=operation,
        actual_units=units or {},
        raw_usage={},
        metadata={
            "backend": backend,
            "index_profile_ref": index_profile_ref,
            **(metadata or {}),
        },
    )
