from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from yutome.chunking import CHUNKER_VERSION, build_chunks
from yutome.config import AppConfig
from yutome.paths import ProjectPaths, resolve_under
from yutome.quality_heuristics import assess_transcript_quality
from yutome.quality_llm import TranscriptCleanupContext, cleanup_transcript_with_gemini
from yutome.store import upsert_transcript_and_chunks
from yutome.transcripts import NormalizedTranscript, TranscriptSegment, read_normalized_segments, write_transcript_artifacts


class SqlConnection(Protocol):
    def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> Any:
        ...


@dataclass(frozen=True)
class QualityUpgradeStats:
    scanned: int
    upgraded: int
    skipped_unchanged: int
    skipped_missing: int
    skipped_quality: int
    failed: int
    chunks_saved: int


@dataclass(frozen=True)
class _UpgradeResult:
    video_id: str
    channel_id: str | None
    transcript: NormalizedTranscript | None
    skipped_missing: bool
    skipped_unchanged: bool
    skipped_quality: bool
    failed: bool
    messages: list[str]


def upgrade_active_transcripts(
    *,
    connection: SqlConnection,
    workspace_id: str,
    config: AppConfig,
    paths: ProjectPaths,
    limit: int | None = None,
    video_id: str | None = None,
    video_ids: list[str] | None = None,
    source_filters: list[str] | None = None,
    exclude_llm_cleanup: bool = True,
    quality_gate: bool = False,
    progress: Callable[[str], None] | None = None,
) -> QualityUpgradeStats:
    if video_id and video_ids:
        raise ValueError("Pass either video_id or video_ids, not both.")
    if video_ids is not None:
        video_ids = sorted(set(video_ids))
        if not video_ids:
            return QualityUpgradeStats(
                scanned=0,
                upgraded=0,
                skipped_unchanged=0,
                skipped_missing=0,
                skipped_quality=0,
                failed=0,
                chunks_saved=0,
            )
    scanned = upgraded = skipped_unchanged = skipped_missing = skipped_quality = failed = chunks_saved = 0
    paths.ensure_base_dirs()
    rows = _candidate_rows(
        connection,
        workspace_id=workspace_id,
        limit=limit,
        video_id=video_id,
        video_ids=video_ids,
        source_filters=source_filters,
        exclude_llm_cleanup=exclude_llm_cleanup,
    )
    scanned = len(rows)
    for result in _upgrade_rows(rows=rows, config=config, paths=paths, quality_gate=quality_gate):
        for message in result.messages:
            if progress:
                progress(message)
        if result.skipped_missing:
            skipped_missing += 1
            continue
        if result.skipped_quality:
            skipped_quality += 1
            continue
        if result.skipped_unchanged:
            skipped_unchanged += 1
            continue
        if result.failed:
            failed += 1
            continue
        if result.transcript is None:
            continue
        saved_chunks = _persist_upgrade(
            connection=connection,
            workspace_id=workspace_id,
            paths=paths,
            transcript=result.transcript,
            channel_id=result.channel_id,
        )
        upgraded += 1
        chunks_saved += saved_chunks
    _commit_if_supported(connection)
    return QualityUpgradeStats(
        scanned=scanned,
        upgraded=upgraded,
        skipped_unchanged=skipped_unchanged,
        skipped_missing=skipped_missing,
        skipped_quality=skipped_quality,
        failed=failed,
        chunks_saved=chunks_saved,
    )


def _upgrade_rows(
    *,
    rows: list[dict[str, object]],
    config: AppConfig,
    paths: ProjectPaths,
    quality_gate: bool,
) -> list[_UpgradeResult]:
    if not rows:
        return []
    video_workers = min(config.transcript_cleanup.video_workers, len(rows))
    if video_workers == 1:
        return [_upgrade_row(row=row, config=config, paths=paths, quality_gate=quality_gate) for row in rows]
    results: list[_UpgradeResult] = []
    with ThreadPoolExecutor(max_workers=video_workers) as executor:
        futures = [
            executor.submit(_upgrade_row, row=row, config=config, paths=paths, quality_gate=quality_gate)
            for row in rows
        ]
        for future in as_completed(futures):
            results.append(future.result())
    return results


def _upgrade_row(
    *,
    row: dict[str, object],
    config: AppConfig,
    paths: ProjectPaths,
    quality_gate: bool,
) -> _UpgradeResult:
    messages = [f"Upgrading {row['video_id']}: {row['title'] or '(untitled)'}"]
    normalized_path = resolve_under(paths.root, Path(str(row["normalized_path"])))
    if not normalized_path.exists():
        messages.append(f"Skipping {row['video_id']}: missing normalized transcript")
        return _UpgradeResult(
            video_id=str(row["video_id"]),
            channel_id=str(row["channel_id"]) if row["channel_id"] else None,
            transcript=None,
            skipped_missing=True,
            skipped_unchanged=False,
            skipped_quality=False,
            failed=False,
            messages=messages,
        )
    transcript = NormalizedTranscript(
        version_id=str(row["transcript_version_id"]),
        video_id=str(row["video_id"]),
        source=str(row["source"]),
        language=str(row["language"]) if row["language"] else None,
        is_generated=bool(row["is_generated"]),
        segments=read_normalized_segments(normalized_path),
        text_hash=str(row["text_hash"]),
    )
    if quality_gate:
        quality = assess_transcript_quality(
            transcript.segments,
            language=transcript.language or "en",
            metadata_text=_quality_metadata_text(row),
        )
        messages.append(
            "  Quality heuristic: "
            f"{quality.label}, {quality.flagged_segment_count} flagged segment(s), "
            f"density {quality.flagged_density:.3f}"
        )
        if not quality.needs_cleanup:
            messages.append(f"  Skipping {row['video_id']}: transcript quality does not need cleanup")
            return _UpgradeResult(
                video_id=transcript.video_id,
                channel_id=str(row["channel_id"]) if row["channel_id"] else None,
                transcript=None,
                skipped_missing=False,
                skipped_unchanged=False,
                skipped_quality=True,
                failed=False,
                messages=messages,
            )
    try:
        upgraded_transcript = _upgrade_one(
            transcript,
            config=config,
            context=_cleanup_context(row),
            progress=messages.append,
        )
    except Exception as exc:  # noqa: BLE001 - one slow/bad cleanup should not stop a library upgrade.
        messages.append(f"  Gemini cleanup failed: {type(exc).__name__}: {exc}")
        return _UpgradeResult(
            video_id=transcript.video_id,
            channel_id=str(row["channel_id"]) if row["channel_id"] else None,
            transcript=None,
            skipped_missing=False,
            skipped_unchanged=False,
            skipped_quality=False,
            failed=True,
            messages=messages,
        )
    return _UpgradeResult(
        video_id=transcript.video_id,
        channel_id=str(row["channel_id"]) if row["channel_id"] else None,
        transcript=upgraded_transcript if upgraded_transcript.version_id != transcript.version_id else None,
        skipped_missing=False,
        skipped_unchanged=upgraded_transcript.version_id == transcript.version_id,
        skipped_quality=False,
        failed=False,
        messages=messages,
    )


def _candidate_rows(
    connection: SqlConnection,
    *,
    workspace_id: str,
    limit: int | None,
    video_id: str | None,
    video_ids: list[str] | None,
    source_filters: list[str] | None,
    exclude_llm_cleanup: bool,
) -> list[dict[str, Any]]:
    sql = """
        SELECT
            v.youtube_video_id AS video_id,
            v.channel_id,
            v.title,
            v.description,
            v.metadata_json->>'channel_title' AS channel_title,
            v.metadata_json->>'channel_handle' AS channel_handle,
            NULL::text AS channel_description,
            tv.id AS transcript_version_id,
            tv.source,
            tv.language_code AS language,
            COALESCE((tv.metadata_json->>'is_generated')::boolean, false) AS is_generated,
            tv.metadata_json->>'normalized_path' AS normalized_path,
            tv.content_hash AS text_hash
        FROM transcript_versions tv
        JOIN videos v
          ON v.id = tv.video_id
         AND v.workspace_id = tv.workspace_id
        WHERE tv.workspace_id = %(workspace_id)s
          AND v.active_transcript_version_id = tv.id
    """
    params: dict[str, Any] = {"workspace_id": workspace_id}
    if video_id:
        sql += " AND (v.id = %(video_id)s OR v.youtube_video_id = %(video_id)s)"
        params["video_id"] = video_id
    if video_ids:
        sql += " AND (v.id = ANY(%(video_ids)s::text[]) OR v.youtube_video_id = ANY(%(video_ids)s::text[]))"
        params["video_ids"] = video_ids
    if source_filters:
        sql += """
          AND EXISTS (
              SELECT 1
              FROM unnest(%(source_filters)s::text[]) AS source_filter(value)
              WHERE tv.source = source_filter.value
                 OR tv.source LIKE source_filter.value || '%%'
          )
        """
        params["source_filters"] = source_filters
    if exclude_llm_cleanup:
        sql += " AND tv.source NOT LIKE %(cleanup_source_pattern)s"
        params["cleanup_source_pattern"] = "%+llm-cleanup:%"
    sql += " ORDER BY v.video_id"
    if limit is not None:
        sql += " LIMIT %(limit)s"
        params["limit"] = limit
    return _rows_from_result(connection.execute(sql, params))


def _upgrade_one(
    transcript: NormalizedTranscript,
    *,
    config: AppConfig,
    context: TranscriptCleanupContext,
    progress: Callable[[str], None] | None,
) -> NormalizedTranscript:
    corrected, stats = cleanup_transcript_with_gemini(
        transcript,
        config=config.gemini,
        context=context,
        batch_segments=config.transcript_cleanup.batch_segments,
        concurrency=config.transcript_cleanup.concurrency,
        max_change_ratio=config.transcript_cleanup.max_change_ratio,
        max_patch_retries=config.transcript_cleanup.max_patch_retries,
    )
    if progress:
        progress(f"  Gemini cleanup: {stats.segments_changed} segment(s), {stats.requests} request(s)")
    return corrected


def _cleanup_context(row: dict[str, object]) -> TranscriptCleanupContext:
    return TranscriptCleanupContext(
        video_title=str(row["title"]) if row["title"] else None,
        video_description=str(row["description"]) if row["description"] else None,
        channel_title=str(row["channel_title"]) if row["channel_title"] else None,
        channel_handle=str(row["channel_handle"]) if row["channel_handle"] else None,
        channel_description=str(row["channel_description"]) if row["channel_description"] else None,
    )


def _quality_metadata_text(row: dict[str, object]) -> str:
    return "\n".join(
        str(value)
        for value in (
            row.get("title"),
            row.get("description"),
            row.get("channel_title"),
            row.get("channel_handle"),
            row.get("channel_description"),
        )
        if value
    )


def _persist_upgrade(
    *,
    connection: SqlConnection,
    workspace_id: str,
    paths: ProjectPaths,
    transcript: NormalizedTranscript,
    channel_id: str | None,
) -> int:
    transcript_paths = paths.transcript_artifacts(transcript.video_id, transcript.version_id)
    raw_snippets = [_raw_snippet(segment) for segment in transcript.segments]
    write_transcript_artifacts(
        paths_root=transcript_paths.root,
        raw_snippets=raw_snippets,
        transcript=transcript,
        include_markdown=True,
        include_srt=True,
    )
    chunks = build_chunks(
        video_id=transcript.video_id,
        transcript_version_id=transcript.version_id,
        segments=transcript.segments,
    )
    chunks_path = paths.chunks_path(transcript.video_id, CHUNKER_VERSION)
    chunks_path.parent.mkdir(parents=True, exist_ok=True)
    with chunks_path.open("w", encoding="utf-8") as chunks_file:
        for chunk in chunks:
            chunks_file.write(
                json.dumps(
                    {
                        "chunk_id": chunk.chunk_id,
                        "sequence": chunk.sequence,
                        "start_ms": chunk.start_ms,
                        "end_ms": chunk.end_ms,
                        "text": chunk.text,
                        "token_count": chunk.token_count,
                        "text_hash": chunk.text_hash,
                        "segment_ids": chunk.segment_ids,
                        "forced_split": chunk.forced_split,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    upsert_transcript_and_chunks(
        connection,
        workspace_id=workspace_id,
        transcript_version_id=transcript.version_id,
        video_id=transcript.video_id,
        channel_id=channel_id,
        source=transcript.source,
        language=transcript.language,
        is_generated=transcript.is_generated,
        raw_path=transcript_paths.raw_json,
        normalized_path=transcript_paths.normalized_jsonl,
        text_hash=transcript.text_hash,
        segment_count=len(transcript.segments),
        chunks=chunks,
    )
    return len(chunks)


def _raw_snippet(segment: TranscriptSegment) -> dict[str, float | str]:
    return {
        "text": segment.text,
        "start": segment.start_ms / 1000,
        "duration": max(0, segment.end_ms - segment.start_ms) / 1000,
    }


def _rows_from_result(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if hasattr(result, "mappings"):
        return [dict(row) for row in result.mappings()]
    if hasattr(result, "fetchall"):
        rows = result.fetchall()
    elif isinstance(result, list):
        rows = result
    else:
        rows = list(result)
    return [dict(row) for row in rows]


def _commit_if_supported(connection: SqlConnection) -> None:
    commit = getattr(connection, "commit", None)
    if callable(commit):
        commit()
