from __future__ import annotations

from yutome.quality_heuristics import assess_transcript_quality
from yutome.transcripts import TranscriptSegment


def _segment(sequence: int, text: str) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=f"s{sequence}",
        sequence=sequence,
        start_ms=sequence * 1000,
        end_ms=(sequence + 1) * 1000,
        text=text,
    )


def test_quality_heuristic_flags_general_caption_corruption() -> None:
    report = assess_transcript_quality(
        [
            _segment(0, "my brain is so scatterrained these days"),
            _segment(1, "I could publish the eepop format"),
            _segment(2, "the essay is about braed loops"),
        ]
    )

    assert report.needs_cleanup
    assert {finding.token for segment in report.flagged_segments for finding in segment.token_findings} >= {
        "scatterrained",
        "eepop",
        "braed",
    }


def test_quality_heuristic_downweights_normal_derived_and_urlish_terms() -> None:
    report = assess_transcript_quality(
        [
            _segment(0, "having a child is one of fourish things"),
            _segment(1, "this might be the sketchiest road in the city"),
            _segment(2, "people habitent here and payent for the train"),
            _segment(3, "tachyphylaxis is a valid technical term in this context"),
            _segment(4, "valid interleave interleaving interleavings terminology appears here"),
            _segment(5, "let the onions caramelize before the next step"),
            _segment(6, "go to BetterHelp.com/example for the sponsor link"),
            _segment(7, "teenhood and unserious counterfactuals are normal words here"),
        ]
    )

    assert report.label == "ok"


def test_quality_heuristic_escalates_repeated_entity_variants() -> None:
    report = assess_transcript_quality(
        [
            _segment(0, "we finally entered Azerbaan after a long drive"),
            _segment(1, "people in Azarbajan were very welcoming"),
            _segment(2, "the capital of Aerban was our next stop"),
            _segment(3, "leaving Azerban felt emotional"),
        ]
    )

    assert report.needs_cleanup
    assert any(
        any(reason.startswith("variant_cluster") for reason in finding.reasons)
        for segment in report.flagged_segments
        for finding in segment.token_findings
    )


def test_quality_heuristic_uses_metadata_to_downweight_known_entities() -> None:
    report = assess_transcript_quality(
        [
            _segment(0, "Hermandus was my coach and everyone called him Mandus"),
            _segment(1, "Mandus had a clear approach to training"),
        ],
        metadata_text="The Legend of Hermandus",
    )

    assert not report.needs_cleanup


def test_quality_heuristic_still_flags_corruption_near_technical_terms() -> None:
    report = assess_transcript_quality(
        [
            _segment(0, "the patient had glyopblastoma and needed treatment"),
            _segment(1, "that error makes retrieval materially worse"),
        ]
    )

    assert report.needs_cleanup


def test_quality_heuristic_keeps_single_language_or_name_like_segment_as_watch() -> None:
    report = assess_transcript_quality(
        [
            _segment(0, "Nikso Kowaiks ni Stun it aniko pitaki"),
            _segment(1, "the rest of this transcript is normal and useful"),
        ]
    )

    assert report.label == "watch"
