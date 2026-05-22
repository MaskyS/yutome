from __future__ import annotations

from yutome.config import default_config
from yutome.indexer import VideoProcessResult, sync_channel, sync_video
import yutome.indexer as indexer_module
from yutome.paths import ProjectPaths
from yutome.youtube import DiscoveredVideo


def _video(video_id: str = "vid-stage") -> DiscoveredVideo:
    return DiscoveredVideo(
        video_id=video_id,
        title="staged fallback test",
        url=f"https://www.youtube.com/watch?v={video_id}",
        channel_id="chan-stage",
        channel_title="Stage Channel",
        channel_handle="@stage",
        duration_seconds=120,
        playlist_tab="videos",
        raw={"id": video_id},
    )


def test_staged_sync_runs_fallback_and_metadata_after_stage_one_rate_limit(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    paths = ProjectPaths.from_config(default_config(), project_root=tmp_path)
    calls: list[str] = []
    logs: list[str] = []

    monkeypatch.setattr(indexer_module, "discover_videos", lambda **kwargs: [_video()])

    def fake_process_video(**kwargs):  # noqa: ANN003
        if kwargs["ytdlp_fallback"]:
            calls.append("stage2-ytdlp")
            return VideoProcessResult(video_id=kwargs["video"].video_id, transcripts_saved=1, chunks_saved=1)
        calls.append("stage1-api")
        return VideoProcessResult(video_id=kwargs["video"].video_id, deferred=1, rate_limited=True)

    def fake_process_metadata(**kwargs):  # noqa: ANN003
        calls.append("stage3-metadata")
        return 1, 0

    monkeypatch.setattr(indexer_module, "_process_video", fake_process_video)
    monkeypatch.setattr(indexer_module, "_process_metadata", fake_process_metadata)

    stats = sync_channel(
        target="https://www.youtube.com/@stage",
        config=default_config(),
        paths=paths,
        workers=1,
        embed=False,
        stop_on_rate_limit=True,
        progress=logs.append,
    )

    assert calls == ["stage1-api", "stage2-ytdlp", "stage3-metadata"]
    assert stats.transcripts_saved == 1
    assert stats.metadata_saved == 1
    assert stats.stopped_early is True
    assert any("Stage 1 hit the rate-limit guard" in message for message in logs)
    assert any("Stage 2 retrying 1 unresolved video" in message for message in logs)
    assert any("Stage 4/heuristic transcript cleanup skipped" in message for message in logs)
    assert not any("Stage 2 skipped because" in message for message in logs)
    assert not any("Stage 3 skipped because" in message for message in logs)


def test_sync_runs_heuristic_cleanup_for_touched_videos_when_gemini_configured(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    config = default_config()
    config = config.model_copy(update={"gemini": config.gemini.model_copy(update={"enabled": True})})
    paths = ProjectPaths.from_config(config, project_root=tmp_path)
    logs: list[str] = []
    calls: list[dict[str, object]] = []

    monkeypatch.setattr(indexer_module, "discover_videos", lambda **kwargs: [_video("needs-cleanup")])

    def fake_process_video(**kwargs):  # noqa: ANN003
        return VideoProcessResult(video_id=kwargs["video"].video_id, transcripts_saved=1, chunks_saved=2)

    def fake_upgrade_active_transcripts(**kwargs):  # noqa: ANN003
        calls.append(kwargs)
        return indexer_module.QualityUpgradeStats(
            scanned=1,
            upgraded=1,
            skipped_unchanged=0,
            skipped_missing=0,
            skipped_quality=0,
            failed=0,
            chunks_saved=3,
        )

    monkeypatch.setattr(indexer_module, "_process_video", fake_process_video)
    monkeypatch.setattr(indexer_module, "upgrade_active_transcripts", fake_upgrade_active_transcripts)

    stats = sync_channel(
        target="https://www.youtube.com/@stage",
        config=config,
        paths=paths,
        workers=1,
        embed=False,
        fetch_metadata=False,
        progress=logs.append,
    )

    assert len(calls) == 1
    assert calls[0]["video_ids"] == ["needs-cleanup"]
    assert calls[0]["quality_gate"] is True
    assert calls[0]["exclude_llm_cleanup"] is True
    assert stats.cleanup_scanned == 1
    assert stats.cleanup_upgraded == 1
    assert stats.cleanup_chunks_saved == 3
    assert any("Stage 4/heuristic transcript cleanup: scanning 1 touched video" in message for message in logs)


def test_sync_video_reuses_staged_channel_pipeline(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    paths = ProjectPaths.from_config(default_config(), project_root=tmp_path)
    logs: list[str] = []
    calls: list[str] = []

    monkeypatch.setattr(indexer_module, "discover_video", lambda **kwargs: _video("exact-video"))

    def fake_process_video(**kwargs):  # noqa: ANN003
        calls.append(kwargs["video"].video_id)
        return VideoProcessResult(video_id=kwargs["video"].video_id, transcripts_saved=1, chunks_saved=4)

    monkeypatch.setattr(indexer_module, "_process_video", fake_process_video)

    stats = sync_video(
        target="https://www.youtube.com/watch?v=exact-video",
        config=default_config(),
        paths=paths,
        workers=1,
        embed=False,
        progress=logs.append,
    )

    assert calls == ["exact-video"]
    assert stats.discovered == 1
    assert stats.transcripts_saved == 1
    assert stats.chunks_saved == 4
    assert any("Candidate videos: 1" in message for message in logs)
