from __future__ import annotations

import json
import re
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, Field

from yutome.config import GeminiConfig
from yutome.hashing import sha256_text
from yutome.quality import derived_transcript
from yutome.transcripts import NormalizedTranscript, TranscriptSegment, clean_text


@dataclass(frozen=True)
class TranscriptCleanupContext:
    video_title: str | None = None
    video_description: str | None = None
    channel_title: str | None = None
    channel_handle: str | None = None
    channel_description: str | None = None


class TranscriptCorrection(BaseModel):
    sequence: int = Field(ge=0)
    text: str = Field(min_length=1)


class TranscriptCorrectionResponse(BaseModel):
    corrections: list[TranscriptCorrection] = Field(default_factory=list)


CLEAN_TRANSCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "corrections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sequence": {"type": "integer"},
                    "text": {"type": "string"},
                },
                "required": ["sequence", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["corrections"],
    "additionalProperties": False,
}


_CLEANUP_INSTRUCTIONS = (
    "Clean this YouTube caption transcript batch. Correct obvious ASR/caption mistakes, "
    "especially technical terms, biomedical terms, names, drugs, supplements, acronyms, "
    "and punctuation when it clarifies reading. Preserve the speaker's meaning and wording. "
    "Do not summarize, expand, fact-check, add claims, remove claims, or add speaker labels. "
    "Use the metadata context only to disambiguate names and terms that appear in the transcript. "
    "Do not inject metadata-only terms into unrelated transcript lines. "
    "Return a sparse correction patch, not the whole transcript. "
    "Only include a correction when a segment's text should change. "
    "Each correction must use the input sequence number and the full corrected text for that one segment. "
    "Return an empty corrections array when no segment should change."
)

# Gemini Flash-Lite enforces a 1,024-token minimum for explicit cached content
# (measured via the caches.create API). At roughly 4 chars/token for English
# prose + bounded JSON metadata, we require ~3,500 chars in the shared prefix
# before attempting to create a cache; the API still validates and we catch
# below-floor failures with a fallback to the uncached path.
#
# Note: even clearing the floor isn't sufficient for caching to be a latency
# win on this workload. Each batch's tail (80 caption segments as JSON) runs
# ~1.5-2K tokens, comparable to the cacheable prefix. At that ~1:1 ratio the
# server-side cache lookup overhead cancels the prefill savings — see the
# config comment on [gemini].cleanup_cache_enabled. Caching pays off here
# only if the prefix grows past roughly 3-4x the per-batch tail size.
_CACHE_MIN_PREFIX_CHARS = 3_500


@dataclass(frozen=True)
class LlmCleanupStats:
    segments_changed: int
    requests: int


def cleanup_transcript_with_gemini(
    transcript: NormalizedTranscript,
    *,
    config: GeminiConfig,
    context: TranscriptCleanupContext | None = None,
    batch_segments: int = 80,
    concurrency: int = 2,
    max_change_ratio: float = 0.35,
    max_patch_retries: int = 2,
    batch_cleaner: Callable[[list[TranscriptSegment]], TranscriptCorrectionResponse] | None = None,
) -> tuple[NormalizedTranscript, LlmCleanupStats]:
    replacements: dict[int, str] = {}
    batches = list(_batched(transcript.segments, max(1, batch_segments)))
    requests = len(batches)
    if batch_cleaner is None:
        payloads = _run_async_cleanup(
            batches=batches,
            config=config,
            context=context,
            concurrency=concurrency,
            max_change_ratio=max_change_ratio,
            max_patch_retries=max_patch_retries,
            video_id=transcript.video_id,
        )
    else:
        payloads = _cleanup_batches_with_cleaner(
            batches=batches,
            batch_cleaner=batch_cleaner,
            concurrency=concurrency,
            max_change_ratio=max_change_ratio,
        )
    for payload in payloads:
        for item in payload.corrections:
            replacements[item.sequence] = item.text

    new_segments: list[TranscriptSegment] = []
    changed = 0
    for segment in transcript.segments:
        text = replacements.get(segment.sequence, segment.text)
        if _too_large_a_change(segment.text, text, max_change_ratio=max_change_ratio):
            text = segment.text
        if text != segment.text:
            changed += 1
        new_segments.append(
            TranscriptSegment(
                segment_id=sha256_text(
                    f"{transcript.video_id}:{segment.sequence}:{segment.start_ms}:{segment.end_ms}:{text}"
                )[:16],
                sequence=segment.sequence,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=text,
            )
        )
    if changed == 0:
        return transcript, LlmCleanupStats(segments_changed=0, requests=requests)
    source = f"{transcript.source}+llm-cleanup:{config.model}"
    return (
        derived_transcript(transcript, segments=new_segments, source=source),
        LlmCleanupStats(segments_changed=changed, requests=requests),
    )


def _cleanup_batches_with_cleaner(
    *,
    batches: list[list[TranscriptSegment]],
    batch_cleaner: Callable[[list[TranscriptSegment]], TranscriptCorrectionResponse],
    concurrency: int,
    max_change_ratio: float,
) -> list[TranscriptCorrectionResponse]:
    payloads: list[TranscriptCorrectionResponse] = []
    max_workers = min(max(1, concurrency), len(batches)) if batches else 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(batch_cleaner, batch): batch for batch in batches}
        for future in as_completed(futures):
            batch = futures[future]
            payloads.append(
                _validated_correction_response(
                    future.result(),
                    batch=batch,
                    max_change_ratio=max_change_ratio,
                )
            )
    return payloads


def _run_async_cleanup(
    *,
    batches: list[list[TranscriptSegment]],
    config: GeminiConfig,
    context: TranscriptCleanupContext | None,
    concurrency: int,
    max_change_ratio: float,
    max_patch_retries: int,
    video_id: str,
) -> list[TranscriptCorrectionResponse]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            _cleanup_batches_with_gemini_async(
                batches=batches,
                config=config,
                context=context,
                concurrency=concurrency,
                max_change_ratio=max_change_ratio,
                max_patch_retries=max_patch_retries,
                video_id=video_id,
            )
        )
    raise RuntimeError("cleanup_transcript_with_gemini cannot be called from an active event loop yet")


async def _cleanup_batches_with_gemini_async(
    *,
    batches: list[list[TranscriptSegment]],
    config: GeminiConfig,
    context: TranscriptCleanupContext | None,
    concurrency: int,
    max_change_ratio: float,
    max_patch_retries: int,
    video_id: str,
) -> list[TranscriptCorrectionResponse]:
    if not batches:
        return []
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError("google-genai is not installed; run `uv sync` or reinstall yutome") from exc

    client = genai.Client(http_options=types.HttpOptions(timeout=int(config.request_timeout_seconds * 1000)))
    cache_name = await _maybe_create_cleanup_cache(
        client=client,
        types=types,
        config=config,
        context=context,
        video_id=video_id,
        batches_count=len(batches),
    )
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def cleanup_one(batch: list[TranscriptSegment]) -> TranscriptCorrectionResponse:
        async with semaphore:
            return await _cleanup_batch_async(
                client=client,
                types=types,
                batch=batch,
                config=config,
                context=context,
                cache_name=cache_name,
                max_change_ratio=max_change_ratio,
                max_patch_retries=max_patch_retries,
            )

    try:
        return await asyncio.gather(*(cleanup_one(batch) for batch in batches))
    finally:
        if cache_name is not None:
            try:
                await client.aio.caches.delete(name=cache_name)
            except Exception:  # noqa: BLE001 - best-effort cleanup; TTL is the backstop
                pass
        await client.aio.aclose()


async def _maybe_create_cleanup_cache(
    *,
    client: Any,
    types: Any,
    config: GeminiConfig,
    context: TranscriptCleanupContext | None,
    video_id: str,
    batches_count: int,
) -> str | None:
    if not config.cleanup_cache_enabled or batches_count <= 1:
        return None
    metadata_text = _metadata_prefix_text(context)
    if len(_CLEANUP_INSTRUCTIONS) + len(metadata_text) < _CACHE_MIN_PREFIX_CHARS:
        return None
    try:
        cache = await client.aio.caches.create(
            model=config.model,
            config=types.CreateCachedContentConfig(
                display_name=f"yutome-cleanup:{video_id}",
                system_instruction=_CLEANUP_INSTRUCTIONS,
                contents=[types.Content(role="user", parts=[types.Part(text=metadata_text)])],
                ttl=f"{int(config.cleanup_cache_ttl_seconds)}s",
            ),
        )
    except Exception:  # noqa: BLE001 - caching is an optimization, never block cleanup on it
        return None
    return getattr(cache, "name", None)


async def _cleanup_batch_async(
    *,
    client: Any,
    types: Any,
    batch: list[TranscriptSegment],
    config: GeminiConfig,
    context: TranscriptCleanupContext | None,
    cache_name: str | None,
    max_change_ratio: float,
    max_patch_retries: int,
) -> TranscriptCorrectionResponse:
    async def generate_patch(validation_error: str | None) -> TranscriptCorrectionResponse:
        if cache_name:
            contents = _batch_tail_text(batch, validation_error)
        else:
            contents = _cleanup_prompt(batch=batch, context=context, validation_error=validation_error)
        response = await client.aio.models.generate_content(
            model=config.model,
            contents=contents,
            config=_generate_content_config(types, config, cache_name=cache_name),
        )
        return _parse_correction_response(_response_text(response))

    return await _cleanup_batch_with_async_generator(
        batch=batch,
        generate_patch=generate_patch,
        max_change_ratio=max_change_ratio,
        max_patch_retries=max_patch_retries,
    )


def _cleanup_batch(
    *,
    batch: list[TranscriptSegment],
    config: GeminiConfig,
    context: TranscriptCleanupContext | None,
    max_change_ratio: float,
    max_patch_retries: int,
) -> TranscriptCorrectionResponse:
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError("google-genai is not installed; run `uv sync` or reinstall yutome") from exc

    http_options = types.HttpOptions(timeout=int(config.request_timeout_seconds * 1000))
    client = genai.Client(http_options=http_options)

    def generate_patch(validation_error: str | None) -> TranscriptCorrectionResponse:
        response = client.models.generate_content(
            model=config.model,
            contents=_cleanup_prompt(batch=batch, context=context, validation_error=validation_error),
            config=_generate_content_config(types, config),
        )
        return _parse_correction_response(_response_text(response))

    return _cleanup_batch_with_generator(
        batch=batch,
        generate_patch=generate_patch,
        max_change_ratio=max_change_ratio,
        max_patch_retries=max_patch_retries,
    )


def _cleanup_batch_with_generator(
    *,
    batch: list[TranscriptSegment],
    generate_patch: Callable[[str | None], TranscriptCorrectionResponse],
    max_change_ratio: float,
    max_patch_retries: int,
) -> TranscriptCorrectionResponse:
    validation_error: str | None = None
    for attempt in range(max_patch_retries + 1):
        try:
            correction_response = generate_patch(validation_error)
            return _validated_correction_response(
                correction_response,
                batch=batch,
                max_change_ratio=max_change_ratio,
            )
        except ValueError as exc:
            validation_error = str(exc)
            if attempt >= max_patch_retries:
                raise RuntimeError(f"LLM cleanup returned invalid patch after retries: {validation_error}") from exc
    raise RuntimeError("LLM cleanup patch validation failed unexpectedly")


async def _cleanup_batch_with_async_generator(
    *,
    batch: list[TranscriptSegment],
    generate_patch: Callable[[str | None], Any],
    max_change_ratio: float,
    max_patch_retries: int,
) -> TranscriptCorrectionResponse:
    validation_error: str | None = None
    for attempt in range(max_patch_retries + 1):
        try:
            correction_response = await generate_patch(validation_error)
            return _validated_correction_response(
                correction_response,
                batch=batch,
                max_change_ratio=max_change_ratio,
            )
        except ValueError as exc:
            validation_error = str(exc)
            if attempt >= max_patch_retries:
                raise RuntimeError(f"LLM cleanup returned invalid patch after retries: {validation_error}") from exc
    raise RuntimeError("LLM cleanup patch validation failed unexpectedly")


def _cleanup_prompt(
    *,
    batch: list[TranscriptSegment],
    context: TranscriptCleanupContext | None,
    validation_error: str | None,
) -> str:
    return (
        f"{_CLEANUP_INSTRUCTIONS}\n"
        f"{_metadata_prefix_text(context)}\n"
        f"{_batch_tail_text(batch, validation_error)}"
    )


def _metadata_prefix_text(context: TranscriptCleanupContext | None) -> str:
    return f"Metadata context JSON:\n{json.dumps(_context_payload(context), ensure_ascii=False)}"


def _batch_tail_text(batch: list[TranscriptSegment], validation_error: str | None) -> str:
    text = (
        "Input JSON:\n"
        f"{json.dumps({'segments': [_segment_payload(segment) for segment in batch]}, ensure_ascii=False)}"
    )
    if validation_error:
        text += (
            "\n"
            "Previous patch validation failed. Return a corrected sparse patch that satisfies the schema "
            "and validation rules. Validation error:\n"
            f"{validation_error}"
        )
    return text


def _validated_correction_response(
    response: TranscriptCorrectionResponse,
    *,
    batch: list[TranscriptSegment],
    max_change_ratio: float,
) -> TranscriptCorrectionResponse:
    by_sequence = {segment.sequence: segment for segment in batch}
    seen_sequences: set[int] = set()
    valid_corrections: list[TranscriptCorrection] = []
    for correction in response.corrections:
        original = by_sequence.get(correction.sequence)
        if original is None:
            raise ValueError(f"unexpected sequence {correction.sequence}")
        if correction.sequence in seen_sequences:
            raise ValueError(f"duplicate sequence {correction.sequence}")
        seen_sequences.add(correction.sequence)
        text = clean_text(correction.text)
        if not text:
            raise ValueError(f"empty correction for sequence {correction.sequence}")
        if text == original.text:
            continue
        if _too_large_a_change(original.text, text, max_change_ratio=max_change_ratio):
            continue
        valid_corrections.append(TranscriptCorrection(sequence=correction.sequence, text=text))
    return TranscriptCorrectionResponse(corrections=valid_corrections)


def _parse_correction_response(text: str) -> TranscriptCorrectionResponse:
    try:
        payload = json.loads(_strip_json_fence(text))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON patch response: {exc.msg}") from exc
    return TranscriptCorrectionResponse.model_validate(payload)


def _segment_payload(segment: TranscriptSegment) -> dict[str, Any]:
    return {
        "sequence": segment.sequence,
        "start_ms": segment.start_ms,
        "end_ms": segment.end_ms,
        "text": segment.text,
    }


def _generate_content_config(types: Any, config: GeminiConfig, *, cache_name: str | None = None) -> Any:
    kwargs: dict[str, Any] = {
        "max_output_tokens": config.cleanup_max_output_tokens,
        "response_mime_type": "application/json",
        "response_json_schema": CLEAN_TRANSCRIPT_SCHEMA,
        "temperature": 0,
        "thinking_config": _thinking_config(types, config),
    }
    if cache_name:
        kwargs["cached_content"] = cache_name
    return types.GenerateContentConfig(**kwargs)


def _thinking_config(types: Any, config: GeminiConfig) -> Any:
    if config.cleanup_thinking_budget is not None:
        return types.ThinkingConfig(thinkingBudget=config.cleanup_thinking_budget)
    if config.cleanup_thinking_level:
        return types.ThinkingConfig(thinkingLevel=config.cleanup_thinking_level.upper())
    return None


def _context_payload(context: TranscriptCleanupContext | None) -> dict[str, str | None]:
    if context is None:
        return {}
    return {
        "video_title": _bounded_text(context.video_title, max_chars=300),
        "video_description": _bounded_text(context.video_description, max_chars=2500),
        "channel_title": _bounded_text(context.channel_title, max_chars=200),
        "channel_handle": _bounded_text(context.channel_handle, max_chars=100),
        "channel_description": _bounded_text(context.channel_description, max_chars=1000),
    }


def _bounded_text(text: str | None, *, max_chars: int) -> str | None:
    if not text:
        return None
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def _batched(segments: list[TranscriptSegment], batch_size: int):
    for index in range(0, len(segments), batch_size):
        yield segments[index : index + batch_size]


def _too_large_a_change(original: str, cleaned: str, *, max_change_ratio: float) -> bool:
    original_length = len(re.sub(r"\s+", "", original))
    cleaned_length = len(re.sub(r"\s+", "", cleaned))
    if not original_length or not cleaned_length:
        return bool(original_length or cleaned_length)
    delta = abs(cleaned_length - original_length) / max(1, original_length)
    return delta > max_change_ratio


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    return fenced.group(1).strip() if fenced else stripped


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if text:
        return str(text)
    raise RuntimeError("Gemini returned no text response")
