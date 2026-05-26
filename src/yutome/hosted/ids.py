from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


def _canonical_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonical_payload(value[key]) for key in sorted(value)}
    if isinstance(value, tuple):
        return [_canonical_payload(item) for item in value]
    if isinstance(value, list):
        return [_canonical_payload(item) for item in value]
    if isinstance(value, set):
        return sorted(_canonical_payload(item) for item in value)
    return value


def canonical_json(value: Any) -> str:
    """Return a stable JSON representation suitable for hashing.

    Provider requests often contain dicts built from unordered config sources.
    Sorting keys and using compact separators prevents harmless key-order
    differences from producing duplicate billable operation IDs.
    """

    return json.dumps(_canonical_payload(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def input_hash(value: Any, *, prefix: str = "h") -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def idempotency_key(
    *,
    workspace_id: str,
    operation: str,
    input_hash_value: str,
    subject_id: str | None = None,
    extras: Sequence[str] | None = None,
) -> str:
    parts = [_idempotency_component(workspace_id)]
    if subject_id:
        parts.append(_idempotency_component(subject_id))
    parts.extend([_idempotency_component(operation), _idempotency_component(input_hash_value)])
    if extras:
        parts.extend(_idempotency_component(str(item)) for item in extras if item)
    return ":".join(parts)


def _idempotency_component(value: str) -> str:
    return value.replace("%", "%25").replace(":", "%3A")
