from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from yutome.chunking import build_chunks
from yutome.config import default_config
from yutome.db import bootstrap_catalog, connect_catalog
from yutome.evals import EvalCase, EvalSuite, run_eval_suite
from yutome.exports import export_markdown
from yutome.paths import ProjectPaths
from yutome.quality_llm import (
    TranscriptCorrection,
    TranscriptCorrectionResponse,
    _cleanup_batch_with_generator,
    cleanup_transcript_with_gemini,
)
import yutome.quality_upgrade as quality_upgrade_module
from yutome.quality_upgrade import upgrade_active_transcripts
from yutome.api import ContextRequest, context_expand, find as api_find
from yutome.store import rebuild_fts, upsert_video_metadata
from yutome.transcripts import TranscriptSegment, normalize_transcript


def _sample_project(tmp_path: Path) -> ProjectPaths:
    config = default_config()
    paths = ProjectPaths.from_config(config, project_root=tmp_path)
    paths.ensure_base_dirs()
    bootstrap_catalog(paths.catalog_db)
    normalized_path = paths.transcript_dir("vid123", "tx123") / "normalized.jsonl"
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    segments = [
        {"segment_id": "s1", "sequence": 0, "start_ms": 0, "end_ms": 4000, "text": "Crohn probiotics intro"},
        {"segment_id": "s2", "sequence": 1, "start_ms": 4000, "end_ms": 8000, "text": "lentils and salads"},
    ]
    normalized_path.write_text("\n".join(json.dumps(segment) for segment in segments) + "\n", encoding="utf-8")
    with connect_catalog(paths.catalog_db) as connection:
        connection.execute(
            "INSERT INTO channels(channel_id, title, handle) VALUES ('chan123', 'Leo and Longevity', '@LeoandLongevity')"
        )
        connection.execute(
            """
            INSERT INTO videos(
                video_id, channel_id, title, description, duration_seconds, published_at, ingest_status
            )
            VALUES (
                'vid123', 'chan123', 'How I Overcame Crohn''s Disease',
                'Long description text', 120, '20200101', 'indexed'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO transcript_versions(
                transcript_version_id, video_id, source, language, is_generated,
                raw_path, normalized_path, text_hash, segment_count, active
            )
            VALUES ('tx123', 'vid123', 'youtube-transcript-api', 'en', 1, 'raw.json', ?, 'hash', 2, 1)
            """,
            (str(normalized_path),),
        )
        chunk_rows = [
            (
                "chunk-a",
                "tx123",
                "vid123",
                "chan123",
                0,
                0,
                60000,
                "Crohn disease background and diagnosis context " * 20,
                400,
                "hash-a",
                "timestamp-aware-v1",
            ),
            (
                "chunk-b",
                "tx123",
                "vid123",
                "chan123",
                1,
                60000,
                120000,
                "Crohn probiotics lentils salads and circadian eating " * 20,
                400,
                "hash-b",
                "timestamp-aware-v1",
            ),
            (
                "chunk-c",
                "tx123",
                "vid123",
                "chan123",
                2,
                120000,
                180000,
                "sauna cardio supplements and follow up context " * 20,
                400,
                "hash-c",
                "timestamp-aware-v1",
            ),
        ]
        connection.executemany(
            """
            INSERT INTO chunks(
                chunk_id, transcript_version_id, video_id, channel_id, sequence,
                start_ms, end_ms, text, token_count, text_hash, chunker_version
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            chunk_rows,
        )
        rebuild_fts(connection)
        connection.commit()
    return paths


def test_retrieve_thin_omits_full_text(tmp_path: Path) -> None:
    paths = _sample_project(tmp_path)

    results = api_find(
        config=default_config(),
        paths=paths,
        text="Crohn probiotics",
        mode="lexical",
        project="thin",
        limit=5,
    ).rows

    assert results
    assert "text" not in results[0]
    assert results[0]["chunk_id"] == "chunk-b"
    assert results[0]["resource_uri"] == "yutome://chunk/chunk-b"
    assert results[0]["transcript_source"] == "youtube-transcript-api"


def test_retrieve_chunk_detail_includes_text(tmp_path: Path) -> None:
    paths = _sample_project(tmp_path)

    results = api_find(
        config=default_config(),
        paths=paths,
        text="probiotics",
        mode="lexical",
        project="chunk",
        limit=1,
    ).rows

    assert "text" in results[0]
    assert "probiotics" in results[0]["text"]


def test_eval_suite_checks_expected_video_and_terms(tmp_path: Path) -> None:
    paths = _sample_project(tmp_path)
    suite = EvalSuite(
        cases=[
            EvalCase(
                name="crohn-probiotics",
                query="Crohn probiotics",
                mode="lexical",
                expected_video_ids=["vid123"],
                expected_terms=["probiotics"],
            )
        ]
    )

    result = run_eval_suite(config=default_config(), paths=paths, suite=suite)

    assert result["passed"] == 1
    assert result["failed"] == 0
    assert result["cases"][0]["passed"] is True


def test_context_expands_within_token_budget(tmp_path: Path) -> None:
    paths = _sample_project(tmp_path)

    context = context_expand(
        paths=paths,
        request=ContextRequest(chunk_id="chunk-b"),
        token_budget=900,
    )

    assert context["estimated_tokens"] <= 900
    assert "Crohn probiotics" in context["text"]
    assert len(context["chunks"]) == 2
    assert context["anchor"]["chunk_id"] == "chunk-b"


def test_exports_write_frontmatter_and_timestamp_links(tmp_path: Path) -> None:
    paths = _sample_project(tmp_path)

    portable = export_markdown(paths=paths, mode="portable-md")
    obsidian = export_markdown(paths=paths, mode="obsidian")

    assert portable.exported == 1
    assert obsidian.exported == 1
    portable_text = next(portable.output_dir.glob("*.md")).read_text(encoding="utf-8")
    obsidian_text = next(obsidian.output_dir.glob("*.md")).read_text(encoding="utf-8")
    assert portable_text.startswith("---\n")
    assert "video_id: \"vid123\"" in portable_text
    assert "[00:00:04](https://youtu.be/vid123?t=4)" in portable_text
    assert "^chunk-" in obsidian_text
    assert "## Timestamps" in obsidian_text


def test_metadata_backfill_does_not_remove_indexed_video_from_exports(tmp_path: Path) -> None:
    paths = _sample_project(tmp_path)
    with connect_catalog(paths.catalog_db) as connection:
        upsert_video_metadata(
            connection,
            video_id="vid123",
            channel_id="chan123",
            metadata={
                "title": "How I Overcame Crohn's Disease",
                "description": "Backfilled description",
                "duration": 120,
                "upload_date": "20200101",
            },
        )
        status = connection.execute(
            "SELECT ingest_status FROM videos WHERE video_id = 'vid123'"
        ).fetchone()["ingest_status"]
        connection.commit()

    assert status == "indexed"
    assert export_markdown(paths=paths, mode="obsidian").exported == 1


def test_oversized_segments_are_split_under_hard_cap() -> None:
    text = " ".join(f"word{i}" for i in range(900))
    segment = TranscriptSegment(
        segment_id="large",
        sequence=0,
        start_ms=0,
        end_ms=90000,
        text=text,
    )

    chunks = build_chunks(
        video_id="vid123",
        transcript_version_id="tx123",
        segments=[segment],
        target_tokens=700,
        overlap_tokens=100,
        max_chunk_tokens=1000,
    )

    assert len(chunks) == 2
    assert max(chunk.token_count for chunk in chunks) <= 1000
    assert any(chunk.forced_split for chunk in chunks)


def test_llm_cleanup_parallel_batches_preserve_sequence_order() -> None:
    transcript = normalize_transcript(
        video_id="vid123",
        raw_snippets=[
            {"start": index, "duration": 1, "text": f"segment {index}"}
            for index in range(6)
        ],
        source="youtube-transcript-api",
        language="en",
        is_generated=True,
    )

    def clean_batch(batch: list[TranscriptSegment]) -> TranscriptCorrectionResponse:
        time.sleep(0.02 if batch[0].sequence == 0 else 0.0)
        return TranscriptCorrectionResponse(
            corrections=[
                TranscriptCorrection(sequence=segment.sequence, text=segment.text.replace("segment", "caption"))
                for segment in batch
            ]
        )

    corrected, stats = cleanup_transcript_with_gemini(
        transcript,
        config=default_config().gemini,
        batch_segments=2,
        concurrency=3,
        batch_cleaner=clean_batch,
    )

    assert stats.requests == 3
    assert stats.segments_changed == 6
    assert [segment.sequence for segment in corrected.segments] == list(range(6))
    assert corrected.segments[0].text == "caption 0"


def test_llm_cleanup_change_guard_allows_technical_term_merges() -> None:
    transcript = normalize_transcript(
        video_id="vid123",
        raw_snippets=[{"start": 0, "duration": 1, "text": "cerebral lysine"}],
        source="youtube-transcript-api",
        language="en",
        is_generated=True,
    )

    def clean_batch(batch: list[TranscriptSegment]) -> TranscriptCorrectionResponse:
        return TranscriptCorrectionResponse(
            corrections=[TranscriptCorrection(sequence=batch[0].sequence, text="cerebrolysin")]
        )

    corrected, stats = cleanup_transcript_with_gemini(
        transcript,
        config=default_config().gemini,
        batch_cleaner=clean_batch,
    )

    assert stats.segments_changed == 1
    assert corrected.segments[0].text == "cerebrolysin"


def test_llm_cleanup_retries_invalid_sparse_patch() -> None:
    batch = [
        TranscriptSegment(segment_id="s1", sequence=10, start_ms=0, end_ms=1000, text="cerebral lysine"),
    ]
    validation_errors: list[str | None] = []

    def generate_patch(validation_error: str | None) -> TranscriptCorrectionResponse:
        validation_errors.append(validation_error)
        if len(validation_errors) == 1:
            return TranscriptCorrectionResponse(
                corrections=[TranscriptCorrection(sequence=999, text="cerebrolysin")]
            )
        return TranscriptCorrectionResponse(
            corrections=[TranscriptCorrection(sequence=10, text="cerebrolysin")]
        )

    response = _cleanup_batch_with_generator(
        batch=batch,
        generate_patch=generate_patch,
        max_change_ratio=0.35,
        max_patch_retries=2,
    )

    assert validation_errors == [None, "unexpected sequence 999"]
    assert response.corrections == [TranscriptCorrection(sequence=10, text="cerebrolysin")]


def test_quality_upgrade_creates_new_active_transcript_version(monkeypatch, tmp_path: Path) -> None:
    paths = _sample_project(tmp_path)

    def fake_cleanup(transcript, *, config, context, batch_segments, concurrency, max_change_ratio, max_patch_retries):
        assert context.video_title == "How I Overcame Crohn's Disease"
        assert context.video_description == "Long description text"
        assert context.channel_title == "Leo and Longevity"
        assert max_patch_retries == 2
        segments = [
            TranscriptSegment(
                segment_id=segment.segment_id,
                sequence=segment.sequence,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                text=segment.text.replace("Crohn", "cerebrolysin"),
            )
            for segment in transcript.segments
        ]
        from yutome.quality import derived_transcript

        stats = type("Stats", (), {"segments_changed": 1, "requests": 1})()
        return (
            derived_transcript(
                transcript,
                segments=segments,
                source=f"{transcript.source}+llm-cleanup:{config.model}",
            ),
            stats,
        )

    monkeypatch.setattr(quality_upgrade_module, "cleanup_transcript_with_gemini", fake_cleanup)

    stats = upgrade_active_transcripts(config=default_config(), paths=paths)

    assert stats.upgraded == 1
    with connect_catalog(paths.catalog_db) as connection:
        row = connection.execute(
            "SELECT source, normalized_path FROM transcript_versions WHERE video_id = 'vid123' AND active = 1"
        ).fetchone()
    assert stats.failed == 0
    assert row["source"] == "youtube-transcript-api+llm-cleanup:gemini-3.1-flash-lite"
    assert "tx123" not in row["normalized_path"]


def test_quality_upgrade_quality_gate_skips_clean_transcript(monkeypatch, tmp_path: Path) -> None:
    paths = _sample_project(tmp_path)

    def fail_cleanup(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("clean transcript should not call LLM cleanup")

    monkeypatch.setattr(quality_upgrade_module, "cleanup_transcript_with_gemini", fail_cleanup)

    stats = upgrade_active_transcripts(config=default_config(), paths=paths, quality_gate=True)

    assert stats.scanned == 1
    assert stats.upgraded == 0
    assert stats.skipped_quality == 1
    assert stats.failed == 0


def test_hybrid_retrieval_reports_stale_lancedb_table(tmp_path: Path) -> None:
    pytest.importorskip("lancedb")
    import lancedb

    paths = _sample_project(tmp_path)
    db = lancedb.connect(paths.lancedb_dir)
    db.create_table(
        "chunks",
        data=[
            {
                "chunk_id": "chunk-b",
                "video_id": "vid123",
                "text": "Crohn probiotics",
                "vector": [0.0, 0.0],
            }
        ],
        mode="overwrite",
    )

    with pytest.raises(RuntimeError, match="LanceDB chunks table is stale"):
        api_find(
            config=default_config(),
            paths=paths,
            text="Crohn probiotics",
            mode="hybrid",
            project="thin",
            limit=1,
        )
