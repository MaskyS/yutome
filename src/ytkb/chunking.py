from __future__ import annotations

from dataclasses import dataclass

from ytkb.hashing import sha256_text
from ytkb.transcripts import TranscriptSegment


CHUNKER_VERSION = "timestamp-aware-v2"
DEFAULT_TARGET_TOKENS = 700
DEFAULT_OVERLAP_TOKENS = 100
DEFAULT_MAX_CHUNK_TOKENS = 1000


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    sequence: int
    start_ms: int
    end_ms: int
    text: str
    token_count: int
    text_hash: str
    segment_ids: list[str]
    forced_split: bool = False


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text.split()) * 1.33))


def _split_oversized_segment(segment: TranscriptSegment, *, max_chunk_tokens: int) -> list[TranscriptSegment]:
    if estimate_tokens(segment.text) <= max_chunk_tokens:
        return [segment]

    words = segment.text.split()
    max_words = max(1, int(max_chunk_tokens / 1.33))
    if len(words) <= max_words:
        return [segment]

    duration_ms = max(0, segment.end_ms - segment.start_ms)
    parts: list[TranscriptSegment] = []
    for part_index, start_word in enumerate(range(0, len(words), max_words)):
        part_words = words[start_word : start_word + max_words]
        part_text = " ".join(part_words)
        part_start = segment.start_ms + round(duration_ms * (start_word / len(words)))
        part_end = segment.start_ms + round(duration_ms * ((start_word + len(part_words)) / len(words)))
        parts.append(
            TranscriptSegment(
                segment_id=f"{segment.segment_id}-split-{part_index}",
                sequence=segment.sequence,
                start_ms=part_start,
                end_ms=max(part_start, part_end),
                text=part_text,
            )
        )
    return parts


def build_chunks(
    *,
    video_id: str,
    transcript_version_id: str,
    segments: list[TranscriptSegment],
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    max_chunk_tokens: int = DEFAULT_MAX_CHUNK_TOKENS,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    current: list[TranscriptSegment] = []
    current_tokens = 0

    def emit(active_segments: list[TranscriptSegment]) -> None:
        if not active_segments:
            return
        text = " ".join(segment.text for segment in active_segments).strip()
        text_hash = sha256_text(text)
        start_ms = active_segments[0].start_ms
        end_ms = active_segments[-1].end_ms
        chunk_seed = (
            f"{video_id}:{transcript_version_id}:{start_ms}:{end_ms}:"
            f"{text_hash}:{CHUNKER_VERSION}"
        )
        chunks.append(
            Chunk(
                chunk_id=sha256_text(chunk_seed),
                sequence=len(chunks),
                start_ms=start_ms,
                end_ms=end_ms,
                text=text,
                token_count=estimate_tokens(text),
                text_hash=text_hash,
                segment_ids=[segment.segment_id for segment in active_segments],
                forced_split=any("-split-" in segment.segment_id for segment in active_segments),
            )
        )

    expanded_segments: list[TranscriptSegment] = []
    for segment in segments:
        expanded_segments.extend(_split_oversized_segment(segment, max_chunk_tokens=max_chunk_tokens))

    for segment in expanded_segments:
        segment_tokens = estimate_tokens(segment.text)
        if current and current_tokens + segment_tokens > target_tokens:
            emit(current)
            overlap: list[TranscriptSegment] = []
            overlap_count = 0
            for prior_segment in reversed(current):
                prior_tokens = estimate_tokens(prior_segment.text)
                if overlap_count + prior_tokens > overlap_tokens:
                    break
                overlap.insert(0, prior_segment)
                overlap_count += prior_tokens
            current = overlap
            current_tokens = overlap_count
        current.append(segment)
        current_tokens += segment_tokens

    emit(current)
    return chunks
