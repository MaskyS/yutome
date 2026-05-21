from __future__ import annotations

from yutome.hashing import sha256_json
from yutome.transcripts import NormalizedTranscript, TranscriptSegment


def derived_transcript(
    transcript: NormalizedTranscript,
    *,
    segments: list[TranscriptSegment],
    source: str,
) -> NormalizedTranscript:
    normalized_for_hash = [
        {"start_ms": segment.start_ms, "end_ms": segment.end_ms, "text": segment.text}
        for segment in segments
    ]
    text_hash = sha256_json(normalized_for_hash)
    version_id = sha256_json(
        {
            "video_id": transcript.video_id,
            "source": source,
            "language": transcript.language,
            "is_generated": transcript.is_generated,
            "text_hash": text_hash,
        }
    )[:24]
    return NormalizedTranscript(
        version_id=version_id,
        video_id=transcript.video_id,
        source=source,
        language=transcript.language,
        is_generated=transcript.is_generated,
        segments=segments,
        text_hash=text_hash,
    )
