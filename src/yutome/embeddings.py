from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from yutome.hosted.provider_wrappers import ProviderCallContext


def _retryable_embedding_error(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    if status_code in {408, 429, 500, 502, 503, 504}:
        return True
    text = str(error).lower()
    return any(
        marker in text
        for marker in (
            "429",
            "rate limit",
            "too many requests",
            "timeout",
            "temporarily",
            "connection",
            "server error",
            "503",
            "504",
        )
    )


def _embed_voyage_batch(
    batch: list[dict[str, Any]],
    *,
    model: str,
    dimension: int,
    max_retries: int,
    retry_base_seconds: float,
    hosted_context: ProviderCallContext | None = None,
) -> list[list[float]]:
    import voyageai

    client: Any | None = None
    texts = [row["text"] for row in batch]

    def call() -> Any:
        nonlocal client
        if client is None:
            client = voyageai.Client()
        return client.embed(
            texts,
            model=model,
            input_type="document",
            output_dimension=dimension,
        )

    response = _execute_hosted_voyage_call(
        lambda: _call_with_retries(
            call,
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
        ),
        hosted_context=hosted_context,
        input_type="document",
        output_dimension=dimension,
    )
    return [list(vector) for vector in response.embeddings]


def _embed_voyage_query(
    *,
    query: str,
    model: str,
    dimension: int,
    hosted_context: ProviderCallContext | None = None,
) -> list[float]:
    import voyageai

    client: Any | None = None

    def call() -> Any:
        nonlocal client
        if client is None:
            client = voyageai.Client()
        return client.embed(
            [query],
            model=model,
            input_type="query",
            output_dimension=dimension,
        )

    response = _execute_hosted_voyage_call(
        call,
        hosted_context=hosted_context,
        input_type="query",
        output_dimension=dimension,
    )
    return response.embeddings[0]


def _execute_hosted_voyage_call(
    call: Callable[[], Any],
    *,
    hosted_context: ProviderCallContext | None,
    input_type: str,
    output_dimension: int,
) -> Any:
    if hosted_context is None:
        return call()

    from yutome.hosted.normalizers import normalize_voyage_embeddings_response
    from yutome.hosted.provider_wrappers import execute_provider_call

    return execute_provider_call(
        hosted_context,
        call,
        normalize_usage=lambda response: normalize_voyage_embeddings_response(
            response,
            operation=hosted_context.operation,
            input_type=input_type,
            output_dimension=output_dimension,
        ),
    )


def _call_with_retries(
    call: Callable[[], Any],
    *,
    max_retries: int,
    retry_base_seconds: float,
) -> Any:
    for attempt in range(max_retries + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - provider clients expose mixed exception types.
            if attempt >= max_retries or not _retryable_embedding_error(exc):
                raise
            sleep_seconds = retry_base_seconds * (2**attempt)
            if retry_base_seconds:
                sleep_seconds += random.uniform(0, retry_base_seconds)
            time.sleep(sleep_seconds)
    raise AssertionError("unreachable retry loop exit")
