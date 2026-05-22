from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from yutome.config import GeminiConfig
from yutome.youtube import TranscriptFetchResult


class GeminiTranscriptSegment(BaseModel):
    start: float = Field(ge=0)
    duration: float = Field(ge=0)
    text: str = Field(min_length=1)


class GeminiTranscriptResponse(BaseModel):
    segments: list[GeminiTranscriptSegment]


GEMINI_TRANSCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "number",
                        "description": "Segment start time in seconds from the beginning of the video.",
                    },
                    "duration": {
                        "type": "number",
                        "description": "Segment duration in seconds.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Clean transcript text for this segment.",
                    },
                },
                "required": ["start", "duration", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["segments"],
    "additionalProperties": False,
}


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    return fenced.group(1).strip() if fenced else stripped


def _drop_blank_segment_text(payload: dict[str, Any]) -> dict[str, Any]:
    segments = payload.get("segments")
    if not isinstance(segments, list):
        return payload
    payload = dict(payload)
    payload["segments"] = [
        segment
        for segment in segments
        if isinstance(segment, dict) and str(segment.get("text") or "").strip()
    ]
    return payload


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if text:
        return str(text)
    raise RuntimeError("Gemini returned no text response")


def _media_resolution(types, value: str):
    match value:
        case "low":
            return types.MediaResolution.MEDIA_RESOLUTION_LOW
        case "medium":
            return types.MediaResolution.MEDIA_RESOLUTION_MEDIUM
        case "high":
            return types.MediaResolution.MEDIA_RESOLUTION_HIGH
        case _:
            return types.MediaResolution.MEDIA_RESOLUTION_LOW


def _offset(seconds: int) -> str:
    return f"{max(0, int(seconds))}s"


def _window_bounds(duration_seconds: int | None, window_seconds: int) -> list[tuple[int, int | None]]:
    if duration_seconds is None or duration_seconds <= window_seconds:
        return [(0, None)]
    bounds = []
    start = 0
    while start < duration_seconds:
        end = min(duration_seconds, start + window_seconds)
        bounds.append((start, end))
        start = end
    return bounds


def _normalize_window_segments(
    segments: list[GeminiTranscriptSegment],
    *,
    window_start: int,
    window_end: int | None,
) -> list[dict[str, float | str]]:
    if not segments:
        return []
    starts = [segment.start for segment in segments]
    relative_timestamps = window_start > 0 and max(starts) < ((window_end or window_start) - window_start + 60)
    normalized = []
    for segment in segments:
        start = segment.start + window_start if relative_timestamps else segment.start
        if window_end is not None and start > window_end + 10:
            continue
        normalized.append(
            {
                "text": segment.text,
                "start": start,
                "duration": segment.duration,
            }
        )
    return normalized


def _transcribe_gemini_window(
    *,
    client,
    types,
    video_url: str,
    config: GeminiConfig,
    window_start: int,
    window_end: int | None,
) -> list[dict[str, float | str]]:
    window_label = (
        "the whole video"
        if window_start == 0 and window_end is None
        else f"the video window from {window_start} seconds to {window_end} seconds"
    )
    prompt = (
        "Create a faithful transcript of the spoken audio in this YouTube video. "
        f"Transcribe only {window_label}. Return JSON only. "
        "Split the transcript into timestamped segments. Use start times and durations in seconds. "
        "If this request is for a nonzero video window, add the window start offset to each segment "
        "so every start time is relative to the beginning of the full video. "
        "Keep segment text clean and plain; do not add commentary, summaries, bullets, or speaker labels "
        "unless the speaker label is spoken."
    )
    video_metadata = None
    if window_start or window_end is not None:
        video_metadata = types.VideoMetadata(
            startOffset=_offset(window_start),
            endOffset=_offset(window_end) if window_end is not None else None,
        )
    response = client.models.generate_content(
        model=config.model,
        contents=types.Content(
            parts=[
                types.Part(
                    fileData=types.FileData(fileUri=video_url),
                    videoMetadata=video_metadata,
                ),
                types.Part(text=prompt),
            ]
        ),
        config=types.GenerateContentConfig(
            maxOutputTokens=config.max_output_tokens,
            responseMimeType="application/json",
            responseJsonSchema=GEMINI_TRANSCRIPT_SCHEMA,
            mediaResolution=_media_resolution(types, config.media_resolution),
        ),
    )
    payload = _drop_blank_segment_text(json.loads(_strip_json_fence(_response_text(response))))
    parsed = GeminiTranscriptResponse.model_validate(payload)
    return _normalize_window_segments(
        parsed.segments,
        window_start=window_start,
        window_end=window_end,
    )


def transcribe_youtube_url_with_gemini(
    *,
    video_id: str,
    config: GeminiConfig,
    duration_seconds: int | None = None,
) -> TranscriptFetchResult:
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError("google-genai is not installed; run `uv sync` or reinstall yutome") from exc

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    client = genai.Client()
    snippets = []
    for window_start, window_end in _window_bounds(duration_seconds, config.window_seconds):
        snippets.extend(
            _transcribe_gemini_window(
                client=client,
                types=types,
                video_url=video_url,
                config=config,
                window_start=window_start,
                window_end=window_end,
            )
        )
    if not snippets:
        raise RuntimeError("Gemini returned an empty transcript")
    return TranscriptFetchResult(
        raw_snippets=snippets,
        source=f"gemini:{config.model}",
        language=None,
        is_generated=True,
    )
