from __future__ import annotations

import json
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Callable

from ytkb.asr import transcribe_with_faster_whisper
from ytkb.chunking import CHUNKER_VERSION, build_chunks
from ytkb.config import AppConfig
from ytkb.db import bootstrap_catalog, connect_catalog
from ytkb.embeddings import EmbeddingStats, embed_pending_chunks
from ytkb.gemini import transcribe_youtube_url_with_gemini
from ytkb.hashing import sha256_json
from ytkb.paths import ProjectPaths
from ytkb.store import (
    active_transcript_source,
    list_catalog_videos,
    mark_video_deferred,
    mark_video_failed,
    record_transcript_attempt,
    rebuild_fts,
    transcript_exists,
    upsert_channel_from_discovery,
    upsert_discovered_video,
    upsert_transcript_and_chunks,
    upsert_video_metadata,
    video_ingest_status,
    write_json,
)
from ytkb.transcripts import normalize_transcript, write_transcript_artifacts
from ytkb.youtube import (
    DiscoveredVideo,
    TranscriptFetchResult,
    describe_proxy,
    discover_videos,
    fetch_subtitle_transcript_with_ytdlp,
    fetch_transcript,
    fetch_video_metadata,
    is_youtube_block_error,
    non_preferred_generated_transcripts,
)


is_rate_limit_error = is_youtube_block_error


def _elapsed(started_at: float) -> str:
    return f"{time.monotonic() - started_at:.1f}s"


@dataclass(frozen=True)
class SyncStats:
    discovered: int = 0
    processed: int = 0
    metadata_saved: int = 0
    transcripts_saved: int = 0
    chunks_saved: int = 0
    skipped_existing: int = 0
    skipped_failed: int = 0
    deferred: int = 0
    failed: int = 0
    embedded_chunks: int = 0
    embedding_message: str = ""
    stopped_early: bool = False
    elapsed_seconds: float = 0.0

    @property
    def videos_per_minute(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return self.transcripts_saved / (self.elapsed_seconds / 60)


@dataclass
class VideoProcessResult:
    video_id: str
    processed: int = 1
    metadata_saved: int = 0
    transcripts_saved: int = 0
    chunks_saved: int = 0
    deferred: int = 0
    failed: int = 0
    rate_limited: bool = False
    messages_streamed: bool = False
    messages: list[str] = field(default_factory=list)


def classify_transcript_error(error: Exception | str) -> tuple[str, bool]:
    text = str(error).lower()
    if is_rate_limit_error(error):
        return "rate_limited", True
    if "transcript disabled" in text or "subtitles are disabled" in text:
        return "no_captions", False
    if "non-preferred generated captions" in text or "mislabeled/foreign caption track" in text:
        return "bad_captions", False
    if "no transcript" in text and "requested language" in text:
        return "language_unavailable", False
    if "video unavailable" in text or "private video" in text:
        return "video_unavailable", False
    if (
        "timed out" in text
        or "timeout" in text
        or "temporarily" in text
        or "max retries exceeded" in text
        or "ssl" in text
        or "unexpected_eof" in text
        or "eof occurred" in text
        or "incompleteread" in text
        or "incomplete read" in text
        or "connection broken" in text
        or "response ended prematurely" in text
        or "could not resolve host" in text
        or "temporary failure in name resolution" in text
        or "name or service not known" in text
        or "nodename nor servname provided" in text
        or "connectionpool" in text
        or "connection aborted" in text
    ):
        return "transient", True
    return "unknown", True


def _metadata_version_id(metadata: dict) -> str:
    return sha256_json(metadata)[:24]


def _matches_status_filters(status: str | None, status_filters: list[str] | None) -> bool:
    if not status_filters:
        return True
    normalized_status = status or "discovered"
    return any(
        normalized_status == status_filter or normalized_status.startswith(status_filter)
        for status_filter in status_filters
    )


def _matches_source_filters(source: str | None, source_filters: list[str] | None) -> bool:
    if not source_filters:
        return True
    if source is None:
        return False
    return any(source == source_filter or source.startswith(source_filter) for source_filter in source_filters)


def _within_max_duration(video: DiscoveredVideo, max_duration_seconds: int | None) -> bool:
    if max_duration_seconds is None:
        return True
    return video.duration_seconds is not None and video.duration_seconds <= max_duration_seconds


def _fallback_only_for_status(status: str | None, enabled: bool) -> bool:
    return enabled and status is not None and (
        status.startswith("deferred: needs_asr_") or status.startswith("failed:")
    )


def _record_attempt(
    paths: ProjectPaths,
    *,
    video_id: str,
    tool: str,
    status: str,
    exc: Exception | str | None = None,
) -> None:
    with connect_catalog(paths.catalog_db) as connection:
        if status == "success":
            record_transcript_attempt(connection, video_id=video_id, tool=tool, status="success")
        else:
            error_class, retryable = classify_transcript_error(exc) if exc is not None else ("unknown", True)
            record_transcript_attempt(
                connection,
                video_id=video_id,
                tool=tool,
                status="failed",
                error_class=error_class,
                error=str(exc) if exc is not None else None,
                retryable=retryable,
            )
        connection.commit()


def _fetch_metadata_block(
    *,
    video: DiscoveredVideo,
    config: AppConfig,
    paths: ProjectPaths,
    metadata_proxy,
    progress: Callable[[str], None],
) -> str | None:
    try:
        metadata = fetch_video_metadata(
            video_id=video.video_id,
            cwd=paths.root,
            proxy=metadata_proxy,
            ytdlp_config=config.yt_dlp,
        )
    except Exception as metadata_exc:  # noqa: BLE001 - retry blocked metadata through proxy.
        if metadata_proxy is not None or not config.proxy.enabled or not is_rate_limit_error(metadata_exc):
            raise
        progress("  metadata fetch hit a block; retrying through proxy")
        metadata = fetch_video_metadata(
            video_id=video.video_id,
            cwd=paths.root,
            proxy=config.proxy,
            ytdlp_config=config.yt_dlp,
        )
    metadata_version_id = _metadata_version_id(metadata)
    write_json(paths.video_metadata_dir(video.video_id) / f"{metadata_version_id}.json", metadata)
    channel_id = metadata.get("channel_id") or video.channel_id
    with connect_catalog(paths.catalog_db) as connection:
        upsert_video_metadata(
            connection,
            video_id=video.video_id,
            channel_id=channel_id,
            metadata=metadata,
        )
        connection.commit()
    return channel_id


def _acquire_transcript(
    *,
    video: DiscoveredVideo,
    config: AppConfig,
    paths: ProjectPaths,
    asr_fallback: bool,
    gemini_fallback: bool,
    fallback_only: bool,
    ytdlp_fallback: bool,
    progress: Callable[[str], None],
) -> TranscriptFetchResult:
    transcript_errors: list[str] = []
    transcript_result: TranscriptFetchResult | None = None
    ytdlp_already_tried = fallback_only
    if fallback_only:
        transcript_errors.append("caption providers skipped for known fallback candidate")
        progress("  caption providers skipped; using fallback")

    if config.transcripts.prefer_ytdlp_subtitles and not fallback_only:
        provider_started = time.monotonic()
        progress("  trying yt-dlp subtitles first")
        try:
            transcript_result = fetch_subtitle_transcript_with_ytdlp(
                video_id=video.video_id,
                cwd=paths.root,
                language=config.transcripts.preferred_languages[0],
                proxy=config.proxy,
                ytdlp_config=config.yt_dlp,
                allow_translated_captions=config.transcripts.allow_translated_captions,
            )
            ytdlp_already_tried = True
            _record_attempt(paths, video_id=video.video_id, tool="yt-dlp-json3", status="success")
            progress(f"  yt-dlp subtitles succeeded in {_elapsed(provider_started)}")
        except Exception as ytdlp_first_exc:  # noqa: BLE001 - fall back to transcript API.
            ytdlp_already_tried = True
            _record_attempt(paths, video_id=video.video_id, tool="yt-dlp-json3", status="failed", exc=ytdlp_first_exc)
            transcript_errors.append(f"yt-dlp-json3: {ytdlp_first_exc}")
            progress(f"  yt-dlp subtitles failed after {_elapsed(provider_started)}; trying transcript API")

    try:
        if fallback_only:
            raise RuntimeError("caption providers skipped")
        if transcript_result is None:
            provider_started = time.monotonic()
            progress("  trying transcript API")
            transcript_result = fetch_transcript(
                video_id=video.video_id,
                languages=config.transcripts.preferred_languages,
                proxy=config.proxy,
                timeout_seconds=config.transcripts.request_timeout_seconds,
            )
            _record_attempt(paths, video_id=video.video_id, tool="youtube-transcript-api", status="success")
            progress(f"  transcript API succeeded in {_elapsed(provider_started)}")
    except Exception as exc:  # noqa: BLE001 - fallback to yt-dlp subtitles.
        transcript_api_elapsed = _elapsed(provider_started) if "provider_started" in locals() else "0.0s"
        error_class, _ = classify_transcript_error(exc)
        if not fallback_only:
            _record_attempt(paths, video_id=video.video_id, tool="youtube-transcript-api", status="failed", exc=exc)
            transcript_errors.append(f"youtube-transcript-api: {exc}")
        skip_ytdlp_reason: str | None = None
        if error_class == "language_unavailable" and not config.transcripts.allow_translated_captions:
            try:
                alternates = non_preferred_generated_transcripts(
                    video_id=video.video_id,
                    preferred_languages=config.transcripts.preferred_languages,
                    proxy=config.proxy,
                    timeout_seconds=config.transcripts.request_timeout_seconds,
                )
            except Exception as list_exc:  # noqa: BLE001 - continue to normal subtitle fallback.
                _record_attempt(paths, video_id=video.video_id, tool="caption-language-check", status="failed", exc=list_exc)
            else:
                if alternates:
                    available = ", ".join(
                        f"{item.language_code} ({item.language})" for item in alternates[:6]
                    )
                    skip_ytdlp_reason = (
                        "non-preferred generated captions only: "
                        f"{available}; likely mislabeled/foreign caption track"
                    )
                    _record_attempt(
                        paths,
                        video_id=video.video_id,
                        tool="caption-language-check",
                        status="failed",
                        exc=skip_ytdlp_reason,
                    )
                    transcript_errors.append(skip_ytdlp_reason)
        if skip_ytdlp_reason:
            progress("  non-English caption label found; skipping translated captions")
        elif not ytdlp_fallback and not ytdlp_already_tried:
            progress(f"  transcript API failed after {transcript_api_elapsed}; yt-dlp fallback disabled")
        else:
            progress(
                f"  transcript API failed after {transcript_api_elapsed}; yt-dlp subtitles already tried"
                if ytdlp_already_tried
                else f"  transcript API failed after {transcript_api_elapsed}; trying yt-dlp subtitles"
            )
        try:
            retried_ytdlp = False
            if skip_ytdlp_reason:
                raise RuntimeError(skip_ytdlp_reason)
            if not ytdlp_fallback and not ytdlp_already_tried:
                raise RuntimeError("yt-dlp fallback disabled")
            if ytdlp_already_tried:
                raise RuntimeError(" | ".join(transcript_errors))
            provider_started = time.monotonic()
            retried_ytdlp = True
            transcript_result = fetch_subtitle_transcript_with_ytdlp(
                video_id=video.video_id,
                cwd=paths.root,
                language=config.transcripts.preferred_languages[0],
                proxy=config.proxy,
                ytdlp_config=config.yt_dlp,
                allow_translated_captions=config.transcripts.allow_translated_captions,
            )
            _record_attempt(paths, video_id=video.video_id, tool="yt-dlp-json3", status="success")
            progress(f"  yt-dlp subtitles succeeded in {_elapsed(provider_started)}")
        except Exception as ytdlp_exc:  # noqa: BLE001 - optional Gemini/ASR fallback.
            if retried_ytdlp:
                progress(f"  yt-dlp subtitles failed after {_elapsed(provider_started)}")
            if retried_ytdlp and not fallback_only:
                _record_attempt(paths, video_id=video.video_id, tool="yt-dlp-json3", status="failed", exc=ytdlp_exc)
                transcript_errors.append(f"yt-dlp-json3: {ytdlp_exc}")
            elif not retried_ytdlp and str(ytdlp_exc) not in transcript_errors:
                transcript_errors.append(str(ytdlp_exc))
            use_gemini_fallback = gemini_fallback or config.gemini.fallback_enabled
            fallback_succeeded = False
            if use_gemini_fallback:
                progress("  subtitle fallback failed; trying Gemini video understanding")
                try:
                    transcript_result = transcribe_youtube_url_with_gemini(
                        video_id=video.video_id,
                        config=config.gemini,
                        duration_seconds=video.duration_seconds,
                    )
                    _record_attempt(paths, video_id=video.video_id, tool=f"gemini:{config.gemini.model}", status="success")
                    fallback_succeeded = True
                except Exception as gemini_exc:  # noqa: BLE001 - optional ASR fallback.
                    _record_attempt(paths, video_id=video.video_id, tool=f"gemini:{config.gemini.model}", status="failed", exc=gemini_exc)
                    transcript_errors.append(f"gemini:{config.gemini.model}: {gemini_exc}")
            if not fallback_succeeded:
                if not asr_fallback:
                    raise RuntimeError(" | ".join(transcript_errors)) from ytdlp_exc
                progress("  subtitle fallback failed; running local ASR")
                transcript_result = transcribe_with_faster_whisper(
                    video_id=video.video_id,
                    cwd=paths.root,
                    config=config.asr,
                    proxy=config.proxy if config.proxy.use_for_asr_audio else None,
                    ytdlp_config=config.yt_dlp,
                    word_timestamps=config.transcripts.word_timestamps,
                )
                _record_attempt(paths, video_id=video.video_id, tool=f"faster-whisper:{config.asr.model}", status="success")

    if transcript_result is None:
        raise RuntimeError("No transcript provider returned a result")
    return transcript_result


def _persist_transcript(
    *,
    video: DiscoveredVideo,
    channel_id: str | None,
    config: AppConfig,
    paths: ProjectPaths,
    transcript_result: TranscriptFetchResult,
    progress: Callable[[str], None],
) -> int:
    normalized = normalize_transcript(
        video_id=video.video_id,
        raw_snippets=transcript_result.raw_snippets,
        source=transcript_result.source,
        language=transcript_result.language,
        is_generated=transcript_result.is_generated,
    )
    transcript_paths = paths.transcript_artifacts(video.video_id, normalized.version_id)
    write_transcript_artifacts(
        paths_root=transcript_paths.root,
        raw_snippets=transcript_result.raw_snippets,
        transcript=normalized,
        include_markdown=config.transcripts.include_markdown,
        include_srt=config.transcripts.include_srt,
    )
    chunks = build_chunks(
        video_id=video.video_id,
        transcript_version_id=normalized.version_id,
        segments=normalized.segments,
    )
    chunks_path = paths.chunks_path(video.video_id, CHUNKER_VERSION)
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
    with connect_catalog(paths.catalog_db) as connection:
        upsert_transcript_and_chunks(
            connection,
            transcript_version_id=normalized.version_id,
            video_id=video.video_id,
            channel_id=channel_id,
            source=normalized.source,
            language=normalized.language,
            is_generated=normalized.is_generated,
            raw_path=transcript_paths.raw_json,
            normalized_path=transcript_paths.normalized_jsonl,
            text_hash=normalized.text_hash,
            segment_count=len(normalized.segments),
            chunks=chunks,
        )
        connection.commit()
    progress(f"  indexed {len(normalized.segments)} segments into {len(chunks)} chunks")
    return len(chunks)


_DEFERRAL_FOR_ERROR_CLASS: dict[str, tuple[str, str]] = {
    "bad_captions": (
        "needs_asr_bad_captions",
        "  deferred: caption language appears wrong; retry with --asr-fallback or --gemini-fallback",
    ),
    "no_captions": (
        "needs_asr_no_captions",
        "  deferred: subtitles disabled; retry with --asr-fallback or --gemini-fallback",
    ),
    "transient": (
        "transient",
        "  deferred: transient transcript/provider error",
    ),
}


def _route_processing_error(
    *,
    exc: Exception,
    video: DiscoveredVideo,
    paths: ProjectPaths,
    progress: Callable[[str], None],
) -> tuple[int, int, bool]:
    """Map an exception to (deferred_inc, failed_inc, rate_limited)."""
    if is_rate_limit_error(exc):
        with connect_catalog(paths.catalog_db) as connection:
            mark_video_deferred(connection, video_id=video.video_id, reason="rate_limited")
            connection.commit()
        progress("  deferred due to rate limiting")
        return 1, 0, True
    error_class, _ = classify_transcript_error(exc)
    deferral = _DEFERRAL_FOR_ERROR_CLASS.get(error_class)
    if deferral is not None:
        reason, message = deferral
        with connect_catalog(paths.catalog_db) as connection:
            mark_video_deferred(connection, video_id=video.video_id, reason=reason)
            connection.commit()
        progress(message)
        return 1, 0, False
    with connect_catalog(paths.catalog_db) as connection:
        mark_video_failed(connection, video_id=video.video_id, error=str(exc))
        connection.commit()
    progress(f"  failed: {str(exc)[:200]}")
    return 0, 1, False


def _process_video(
    *,
    video: DiscoveredVideo,
    config: AppConfig,
    paths: ProjectPaths,
    metadata_proxy,
    asr_fallback: bool,
    gemini_fallback: bool,
    sleep_seconds: float,
    fetch_metadata: bool,
    fallback_only: bool,
    ytdlp_fallback: bool,
    stream_progress: Callable[[str], None] | None = None,
) -> VideoProcessResult:
    video_started = time.monotonic()
    result = VideoProcessResult(
        video_id=video.video_id,
        messages=[f"Processing {video.video_id}: {video.title or '(untitled)'}"],
    )
    if stream_progress is not None:
        result.messages_streamed = True
        stream_progress(result.messages[0])

    def progress(message: str) -> None:
        result.messages.append(message)
        if stream_progress is not None:
            stream_progress(f"[{video.video_id}] {message}")

    try:
        if fetch_metadata:
            metadata_started = time.monotonic()
            progress("  fetching metadata")
            channel_id = _fetch_metadata_block(
                video=video,
                config=config,
                paths=paths,
                metadata_proxy=metadata_proxy,
                progress=progress,
            )
            result.metadata_saved += 1
            progress(f"  metadata saved in {_elapsed(metadata_started)}")
        else:
            progress("  metadata fetch deferred")
            channel_id = video.channel_id
        transcript_started = time.monotonic()
        transcript_result = _acquire_transcript(
            video=video,
            config=config,
            paths=paths,
            asr_fallback=asr_fallback,
            gemini_fallback=gemini_fallback,
            fallback_only=fallback_only,
            ytdlp_fallback=ytdlp_fallback,
            progress=progress,
        )
        progress(f"  transcript acquired from {transcript_result.source} in {_elapsed(transcript_started)}")
        persist_started = time.monotonic()
        chunks_saved = _persist_transcript(
            video=video,
            channel_id=channel_id,
            config=config,
            paths=paths,
            transcript_result=transcript_result,
            progress=progress,
        )
        progress(f"  persisted transcript artifacts/catalog in {_elapsed(persist_started)}")
        result.transcripts_saved += 1
        result.chunks_saved += chunks_saved
    except Exception as exc:  # noqa: BLE001 - each video should fail independently.
        deferred, failed, rate_limited = _route_processing_error(
            exc=exc, video=video, paths=paths, progress=progress
        )
        result.deferred += deferred
        result.failed += failed
        if rate_limited:
            result.rate_limited = True

    if sleep_seconds:
        time.sleep(sleep_seconds)
    progress(f"  finished video in {_elapsed(video_started)}")
    return result


def sync_channel(
    *,
    target: str,
    config: AppConfig,
    paths: ProjectPaths,
    limit: int | None = None,
    embed: bool = False,
    sleep_seconds: float = 0.0,
    force: bool = False,
    asr_fallback: bool = False,
    gemini_fallback: bool = False,
    max_process: int | None = None,
    retry_failed: bool = False,
    stop_on_rate_limit: bool = True,
    refresh_discovery: bool = True,
    verbose_skips: bool = False,
    workers: int = 1,
    fetch_metadata: bool = True,
    status_filters: list[str] | None = None,
    source_filters: list[str] | None = None,
    max_duration_seconds: int | None = None,
    shortest_first: bool = False,
    fallback_only: bool = False,
    ytdlp_fallback: bool = True,
    staged_fallback: bool = False,
    progress: Callable[[str], None] | None = None,
) -> SyncStats:
    started_at = time.monotonic()
    paths.ensure_base_dirs()
    bootstrap_catalog(paths.catalog_db)
    if (
        config.proxy.enabled
        and config.proxy.kind == "webshare"
        and (not config.proxy.webshare_username or not config.proxy.webshare_password)
    ):
        raise ValueError("Webshare proxy is enabled but YTKB_WEBSHARE_USERNAME/YTKB_WEBSHARE_PASSWORD are missing.")
    discovery_proxy = config.proxy if config.proxy.use_for_discovery else None
    metadata_proxy = config.proxy if config.proxy.use_for_metadata else None

    if refresh_discovery:
        discovered = discover_videos(
            target=target,
            cwd=paths.root,
            limit=limit,
            proxy=discovery_proxy,
            ytdlp_config=config.yt_dlp,
        )
    else:
        with connect_catalog(paths.catalog_db) as connection:
            discovered = list_catalog_videos(connection)
    stats = {
        "discovered": len(discovered),
        "processed": 0,
        "metadata_saved": 0,
        "transcripts_saved": 0,
        "chunks_saved": 0,
        "skipped_existing": 0,
        "skipped_failed": 0,
        "deferred": 0,
        "failed": 0,
        "embedded_chunks": 0,
        "embedding_message": "",
        "stopped_early": False,
        "elapsed_seconds": 0.0,
    }
    attempted_video_ids: set[str] = set()
    staged_retry_video_ids: set[str] = set()

    if refresh_discovery:
        with connect_catalog(paths.catalog_db) as connection:
            channel_id = None
            for video in discovered:
                channel_id = upsert_channel_from_discovery(connection, video, source_url=target)
                upsert_discovered_video(connection, video, channel_id=channel_id)
                write_json(paths.video_metadata_dir(video.video_id) / "discovered.json", video.raw)
            connection.commit()

    candidates: list[tuple[DiscoveredVideo, bool]] = []
    if shortest_first:
        discovered = sorted(
            discovered,
            key=lambda video: (
                video.duration_seconds is None,
                video.duration_seconds or 0,
                video.video_id,
            ),
        )
    for video in discovered:
        if not force and transcript_exists(paths.catalog_db, video.video_id):
            stats["skipped_existing"] += 1
            if progress and verbose_skips:
                progress(f"Processing {video.video_id}: {video.title or '(untitled)'}")
                progress("  skipped existing transcript")
            continue
        if not _within_max_duration(video, max_duration_seconds):
            stats["skipped_failed"] += 1
            if progress and verbose_skips:
                progress(f"Processing {video.video_id}: {video.title or '(untitled)'}")
                progress("  skipped; longer than --max-duration-seconds")
            continue
        with connect_catalog(paths.catalog_db) as connection:
            current_status = video_ingest_status(connection, video_id=video.video_id)
            current_source = active_transcript_source(connection, video_id=video.video_id)
        if not _matches_status_filters(current_status, status_filters):
            stats["skipped_failed"] += 1
            if progress and verbose_skips:
                progress(f"Processing {video.video_id}: {video.title or '(untitled)'}")
                progress(f"  skipped {current_status or 'discovered'}; excluded by --status-filter")
            continue
        if not _matches_source_filters(current_source, source_filters):
            stats["skipped_failed"] += 1
            if progress and verbose_skips:
                progress(f"Processing {video.video_id}: {video.title or '(untitled)'}")
                progress(f"  skipped {current_source or 'no active source'}; excluded by --source-filter")
            continue
        if (
            current_status
            and (current_status.startswith("failed:") or current_status.startswith("deferred:"))
            and not retry_failed
            and not force
        ):
            stats["skipped_failed"] += 1
            if progress and verbose_skips:
                progress(f"Processing {video.video_id}: {video.title or '(untitled)'}")
                progress(f"  skipped {current_status}; pass --retry-failed to retry")
            continue
        if max_process is not None and len(candidates) >= max_process:
            stats["stopped_early"] = True
            if progress:
                progress(f"Reached max_process={max_process}; stopping cleanly")
            break
        candidates.append((video, _fallback_only_for_status(current_status, fallback_only)))

    if progress:
        progress(f"Proxy mode: {describe_proxy(config.proxy)}")
        progress(f"Discovery proxy: {'enabled' if discovery_proxy else 'disabled'}")
        progress(f"Metadata proxy: {'enabled' if metadata_proxy else 'disabled'}")
        progress(f"Transcript proxy: {'enabled' if config.proxy.enabled else 'disabled'}")
        progress(f"Candidate videos: {len(candidates)}")

    candidate_by_id = {video.video_id: (video, video_fallback_only) for video, video_fallback_only in candidates}

    def merge_result(video_result: VideoProcessResult, *, collect_staged_retry: bool = False) -> None:
        attempted_video_ids.add(video_result.video_id)
        stats["processed"] += video_result.processed
        stats["metadata_saved"] += video_result.metadata_saved
        stats["transcripts_saved"] += video_result.transcripts_saved
        stats["chunks_saved"] += video_result.chunks_saved
        stats["deferred"] += video_result.deferred
        stats["failed"] += video_result.failed
        if (
            collect_staged_retry
            and video_result.transcripts_saved == 0
            and (video_result.deferred or video_result.failed)
            and video_result.video_id in candidate_by_id
        ):
            staged_retry_video_ids.add(video_result.video_id)
        if video_result.rate_limited and stop_on_rate_limit:
            stats["stopped_early"] = True
        if progress:
            if not video_result.messages_streamed:
                for message in video_result.messages:
                    progress(message)

    def run_candidates(
        phase_candidates: list[tuple[DiscoveredVideo, bool]],
        *,
        phase_name: str | None,
        phase_config: AppConfig,
        phase_ytdlp_fallback: bool,
        collect_staged_retry: bool = False,
    ) -> None:
        if progress and phase_name:
            progress(
                f"{phase_name}: {len(phase_candidates)} candidate(s); "
                f"yt-dlp fallback {'enabled' if phase_ytdlp_fallback else 'deferred'}; "
                f"metadata {'enabled' if fetch_metadata else 'deferred'}"
            )
        if workers <= 1 or not phase_candidates:
            for video, video_fallback_only in phase_candidates:
                video_result = _process_video(
                    video=video,
                    config=phase_config,
                    paths=paths,
                    metadata_proxy=metadata_proxy,
                    asr_fallback=asr_fallback,
                    gemini_fallback=gemini_fallback,
                    sleep_seconds=sleep_seconds,
                    fetch_metadata=fetch_metadata,
                    fallback_only=video_fallback_only,
                    ytdlp_fallback=phase_ytdlp_fallback,
                    stream_progress=progress,
                )
                merge_result(video_result, collect_staged_retry=collect_staged_retry)
                if video_result.rate_limited and stop_on_rate_limit:
                    break
            return

        max_workers = min(workers, len(phase_candidates))
        candidate_iter = iter(phase_candidates)
        futures: dict[Future[VideoProcessResult], DiscoveredVideo] = {}
        stop_submitting = False
        progress_lock = Lock()

        def stream_thread_progress(message: str) -> None:
            if progress is None:
                return
            with progress_lock:
                progress(message)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for _ in range(max_workers):
                try:
                    video, video_fallback_only = next(candidate_iter)
                except StopIteration:
                    break
                futures[
                    executor.submit(
                        _process_video,
                        video=video,
                        config=phase_config,
                        paths=paths,
                        metadata_proxy=metadata_proxy,
                        asr_fallback=asr_fallback,
                        gemini_fallback=gemini_fallback,
                        sleep_seconds=sleep_seconds,
                        fetch_metadata=fetch_metadata,
                        fallback_only=video_fallback_only,
                        ytdlp_fallback=phase_ytdlp_fallback,
                        stream_progress=stream_thread_progress,
                    )
                ] = video
            while futures:
                done, _pending = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    futures.pop(future)
                    video_result = future.result()
                    merge_result(video_result, collect_staged_retry=collect_staged_retry)
                    if video_result.rate_limited and stop_on_rate_limit:
                        stop_submitting = True
                while not stop_submitting and len(futures) < max_workers:
                    try:
                        video, video_fallback_only = next(candidate_iter)
                    except StopIteration:
                        break
                    futures[
                        executor.submit(
                            _process_video,
                            video=video,
                            config=phase_config,
                            paths=paths,
                            metadata_proxy=metadata_proxy,
                            asr_fallback=asr_fallback,
                            gemini_fallback=gemini_fallback,
                            sleep_seconds=sleep_seconds,
                            fetch_metadata=fetch_metadata,
                            fallback_only=video_fallback_only,
                            ytdlp_fallback=phase_ytdlp_fallback,
                            stream_progress=stream_thread_progress,
                        )
                    ] = video

    first_phase_ytdlp_fallback = ytdlp_fallback and not staged_fallback
    run_candidates(
        candidates,
        phase_name="Stage 1/basic transcript pass" if staged_fallback else None,
        phase_config=config,
        phase_ytdlp_fallback=first_phase_ytdlp_fallback,
        collect_staged_retry=staged_fallback,
    )

    if staged_fallback and progress:
        progress(f"Stage 1 queued {len(staged_retry_video_ids)} unresolved video(s) for fallback retry")

    if staged_fallback and stats["stopped_early"] and stop_on_rate_limit:
        if progress:
            progress("Stage 2 skipped because --stop-on-rate-limit triggered")
    elif staged_fallback and staged_retry_video_ids:
        retry_config = config.model_copy(
            update={
                "transcripts": config.transcripts.model_copy(
                    update={"prefer_ytdlp_subtitles": True}
                )
            }
        )
        retry_candidates = [
            candidate_by_id[video_id]
            for video_id in sorted(staged_retry_video_ids)
            if not transcript_exists(paths.catalog_db, video_id)
        ]
        run_candidates(
            retry_candidates,
            phase_name="Stage 2/yt-dlp fallback retry",
            phase_config=retry_config,
            phase_ytdlp_fallback=True,
            collect_staged_retry=False,
        )
    elif staged_fallback and progress:
        progress("Stage 2 skipped: no unresolved videos queued")

    if staged_fallback and attempted_video_ids:
        placeholders = ",".join("?" for _ in attempted_video_ids)
        with connect_catalog(paths.catalog_db) as connection:
            rows = connection.execute(
                f"SELECT ingest_status FROM videos WHERE video_id IN ({placeholders})",
                tuple(attempted_video_ids),
            ).fetchall()
        stats["deferred"] = sum(1 for row in rows if str(row["ingest_status"]).startswith("deferred:"))
        stats["failed"] = sum(1 for row in rows if str(row["ingest_status"]).startswith("failed:"))

    with connect_catalog(paths.catalog_db) as connection:
        rebuild_fts(connection)
        connection.commit()
        if embed:
            embedding_stats: EmbeddingStats = embed_pending_chunks(
                connection=connection,
                config=config,
                lancedb_dir=paths.lancedb_dir,
            )
            stats["embedded_chunks"] = embedding_stats.embedded_chunks
            stats["embedding_message"] = embedding_stats.message

    stats["elapsed_seconds"] = time.monotonic() - started_at
    return SyncStats(**stats)
