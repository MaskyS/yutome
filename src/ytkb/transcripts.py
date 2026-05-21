from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ytkb.hashing import sha256_json, sha256_text


WEBVTT_TIMESTAMP = "{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
SRT_TIMESTAMP = "{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


@dataclass(frozen=True)
class TranscriptSegment:
    segment_id: str
    sequence: int
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class NormalizedTranscript:
    version_id: str
    video_id: str
    source: str
    language: str | None
    is_generated: bool
    segments: list[TranscriptSegment]
    text_hash: str


def clean_text(text: str) -> str:
    unescaped = html.unescape(text)
    no_tags = re.sub(r"<[^>]+>", " ", unescaped)
    return re.sub(r"\s+", " ", no_tags).strip()


def normalize_transcript(
    *,
    video_id: str,
    raw_snippets: list[dict[str, Any]],
    source: str,
    language: str | None,
    is_generated: bool,
) -> NormalizedTranscript:
    segments: list[TranscriptSegment] = []
    normalized_for_hash: list[dict[str, Any]] = []
    for sequence, snippet in enumerate(raw_snippets):
        text = clean_text(str(snippet.get("text", "")))
        if not text:
            continue
        start = float(snippet.get("start", 0.0) or 0.0)
        duration = float(snippet.get("duration", 0.0) or 0.0)
        start_ms = max(0, round(start * 1000))
        end_ms = max(start_ms, round((start + duration) * 1000))
        segment_seed = f"{video_id}:{sequence}:{start_ms}:{end_ms}:{text}"
        segment = TranscriptSegment(
            segment_id=sha256_text(segment_seed)[:16],
            sequence=len(segments),
            start_ms=start_ms,
            end_ms=end_ms,
            text=text,
        )
        segments.append(segment)
        normalized_for_hash.append(
            {
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
                "text": segment.text,
            }
        )
    text_hash = sha256_json(normalized_for_hash)
    version_seed = {
        "video_id": video_id,
        "source": source,
        "language": language,
        "is_generated": is_generated,
        "text_hash": text_hash,
    }
    version_id = sha256_json(version_seed)[:24]
    return NormalizedTranscript(
        version_id=version_id,
        video_id=video_id,
        source=source,
        language=language,
        is_generated=is_generated,
        segments=segments,
        text_hash=text_hash,
    )


def format_timestamp(ms: int, *, srt: bool = False) -> str:
    seconds_total, millis = divmod(max(0, ms), 1000)
    minutes_total, seconds = divmod(seconds_total, 60)
    hours, minutes = divmod(minutes_total, 60)
    template = SRT_TIMESTAMP if srt else WEBVTT_TIMESTAMP
    return template.format(hours=hours, minutes=minutes, seconds=seconds, millis=millis)


def render_plain_text(segments: list[TranscriptSegment]) -> str:
    return "\n\n".join(segment.text for segment in segments).strip() + "\n"


def render_markdown(segments: list[TranscriptSegment], *, video_id: str) -> str:
    lines = ["# Transcript", ""]
    for segment in segments:
        seconds = segment.start_ms // 1000
        lines.append(f"- [{format_timestamp(segment.start_ms)}](https://youtube.com/watch?v={video_id}&t={seconds}s) {segment.text}")
    return "\n".join(lines).strip() + "\n"


def render_vtt(segments: list[TranscriptSegment]) -> str:
    lines = ["WEBVTT", ""]
    for segment in segments:
        lines.append(f"{format_timestamp(segment.start_ms)} --> {format_timestamp(segment.end_ms)}")
        lines.append(segment.text)
        lines.append("")
    return "\n".join(lines)


def render_srt(segments: list[TranscriptSegment]) -> str:
    lines: list[str] = []
    for index, segment in enumerate(segments, start=1):
        lines.append(str(index))
        lines.append(f"{format_timestamp(segment.start_ms, srt=True)} --> {format_timestamp(segment.end_ms, srt=True)}")
        lines.append(segment.text)
        lines.append("")
    return "\n".join(lines)


def read_normalized_segments(path: Path) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    with path.open("r", encoding="utf-8") as jsonl_file:
        for line in jsonl_file:
            if not line.strip():
                continue
            item = json.loads(line)
            segments.append(
                TranscriptSegment(
                    segment_id=str(item["segment_id"]),
                    sequence=int(item["sequence"]),
                    start_ms=int(item["start_ms"]),
                    end_ms=int(item["end_ms"]),
                    text=str(item["text"]),
                )
            )
    return segments


def write_transcript_artifacts(
    *,
    paths_root: Path,
    raw_snippets: list[dict[str, Any]],
    transcript: NormalizedTranscript,
    include_markdown: bool,
    include_srt: bool,
) -> None:
    paths_root.mkdir(parents=True, exist_ok=True)
    (paths_root / "raw.json").write_text(
        json.dumps(raw_snippets, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with (paths_root / "normalized.jsonl").open("w", encoding="utf-8") as jsonl_file:
        for segment in transcript.segments:
            jsonl_file.write(
                json.dumps(
                    {
                        "segment_id": segment.segment_id,
                        "sequence": segment.sequence,
                        "start_ms": segment.start_ms,
                        "end_ms": segment.end_ms,
                        "text": segment.text,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    (paths_root / "transcript.txt").write_text(render_plain_text(transcript.segments), encoding="utf-8")
    (paths_root / "transcript.vtt").write_text(render_vtt(transcript.segments), encoding="utf-8")
    if include_srt:
        (paths_root / "transcript.srt").write_text(render_srt(transcript.segments), encoding="utf-8")
    if include_markdown:
        (paths_root / "transcript.md").write_text(
            render_markdown(transcript.segments, video_id=transcript.video_id),
            encoding="utf-8",
        )
