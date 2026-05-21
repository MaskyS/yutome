from pathlib import Path

from typer.testing import CliRunner

from ytkb.cli import app
from ytkb.channels import channel_from_input, import_channels_from_file, list_library_channels
from ytkb.config import load_config, write_default_config
from ytkb.db import bootstrap_catalog, catalog_is_initialized, catalog_tables, connect_catalog, fts5_available
from ytkb.embeddings import _lancedb_table_names
from ytkb.env import apply_env_to_config
from ytkb.gemini import _drop_blank_segment_text, _window_bounds
from ytkb.indexer import (
    _fallback_only_for_status,
    _matches_source_filters,
    _matches_status_filters,
    classify_transcript_error,
    is_rate_limit_error,
)
from ytkb.paths import ProjectPaths
from ytkb.youtube import _is_ytdlp_block_error, proxy_url_for_ytdlp, redact_proxy_url


def test_default_config_round_trip(tmp_path: Path) -> None:
    config_path = tmp_path / "ytkb.toml"

    written = write_default_config(config_path)
    config = load_config(config_path)

    assert written is True
    assert config.backfill.workers == 2
    assert config.backfill.batch_size == 25
    assert config.scheduler.cadence_hours == 3
    assert config.asr.provider == "faster-whisper"
    assert config.asr.model == "small.en"
    assert config.transcripts.allow_translated_captions is False
    assert config.transcripts.request_timeout_seconds == 30.0
    assert config.transcripts.prefer_ytdlp_subtitles is False
    assert config.transcript_cleanup.video_workers == 1
    assert config.transcript_cleanup.batch_segments == 80
    assert config.transcript_cleanup.concurrency == 4
    assert config.transcript_cleanup.max_change_ratio == 0.35
    assert config.transcript_cleanup.max_patch_retries == 2
    assert config.embeddings.provider == "voyage"
    assert config.embeddings.model == "voyage-4-lite"
    assert config.embeddings.dimension == 1024
    assert config.embeddings.batch_size == 128
    assert config.embeddings.concurrency == 4
    assert config.embeddings.max_retries == 5
    assert config.vectors.backend == "lancedb"
    assert config.yt_dlp.subtitle_retries_when_blocked == 3
    assert config.yt_dlp.subprocess_timeout_seconds == 300.0
    assert config.proxy.use_for_discovery is False
    assert config.proxy.use_for_metadata is False
    assert config.proxy.use_for_asr_audio is False
    assert config.proxy.webshare_domain == "p.webshare.io"
    assert config.proxy.webshare_port == 80
    assert config.gemini.model == "gemini-3.1-flash-lite"
    assert config.gemini.cleanup_max_output_tokens == 4096
    assert config.gemini.request_timeout_seconds == 90.0
    assert config.gemini.cleanup_thinking_level == "low"
    assert config.gemini.media_resolution == "low"
    assert config.gemini.window_seconds == 900


def test_project_paths_include_timestamped_and_plain_transcript_artifacts(tmp_path: Path) -> None:
    config_path = tmp_path / "ytkb.toml"
    write_default_config(config_path)
    config = load_config(config_path)
    paths = ProjectPaths.from_config(config, project_root=tmp_path)

    transcript_paths = paths.transcript_artifacts("video123", "transcript456")

    assert transcript_paths.raw_json == tmp_path / "data/artifacts/videos/video123/transcripts/transcript456/raw.json"
    assert transcript_paths.normalized_jsonl == tmp_path / "data/artifacts/videos/video123/transcripts/transcript456/normalized.jsonl"
    assert transcript_paths.transcript_txt == tmp_path / "data/artifacts/videos/video123/transcripts/transcript456/transcript.txt"
    assert transcript_paths.transcript_md == tmp_path / "data/artifacts/videos/video123/transcripts/transcript456/transcript.md"
    assert transcript_paths.transcript_vtt == tmp_path / "data/artifacts/videos/video123/transcripts/transcript456/transcript.vtt"
    assert transcript_paths.transcript_srt == tmp_path / "data/artifacts/videos/video123/transcripts/transcript456/transcript.srt"


def test_bootstrap_catalog_creates_expected_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "data/indexes/catalog.sqlite"

    bootstrap_catalog(db_path)

    tables = catalog_tables(db_path)
    assert catalog_is_initialized(db_path)
    assert fts5_available()
    assert {
        "channels",
        "library_channels",
        "videos",
        "transcript_versions",
        "chunks",
        "chunks_fts",
        "embeddings",
        "transcript_attempts",
        "jobs",
    }.issubset(tables)


def test_channel_inputs_normalize_to_library_channels() -> None:
    channel = channel_from_input("@LeoandLongevity", title="Leo and Longevity")

    assert channel is not None
    assert channel.handle == "LeoandLongevity"
    assert channel.source_url == "https://www.youtube.com/@LeoandLongevity"
    assert channel.title == "Leo and Longevity"


def test_import_takeout_subscriptions_csv(tmp_path: Path) -> None:
    csv_path = tmp_path / "subscriptions.csv"
    csv_path.write_text(
        "Channel Id,Channel Url,Channel Title\n"
        "UCabc12345678901234567890,https://www.youtube.com/channel/UCabc12345678901234567890,Example Channel\n",
        encoding="utf-8",
    )

    imported = import_channels_from_file(csv_path)

    assert len(imported) == 1
    assert imported[0].channel_id == "UCabc12345678901234567890"
    assert imported[0].title == "Example Channel"


def test_channels_cli_add_and_list(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "ytkb.toml"
    write_default_config(config_path)

    add_result = runner.invoke(app, ["channels", "add", "@LeoandLongevity", "--config", str(config_path)])

    assert add_result.exit_code == 0
    with connect_catalog(tmp_path / "data/indexes/catalog.sqlite") as connection:
        channels = list_library_channels(connection)
    assert len(channels) == 1
    assert channels[0].handle == "LeoandLongevity"


def test_sync_rejects_staged_fallback_without_yt_dlp_fallback(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "ytkb.toml"
    write_default_config(config_path)

    result = runner.invoke(
        app,
        [
            "sync",
            "@Example",
            "--config",
            str(config_path),
            "--staged-fallback",
            "--no-yt-dlp-fallback",
        ],
    )

    assert result.exit_code != 0
    assert "--staged-fallback needs yt-dlp fallback enabled" in result.output


def test_sync_rejects_staged_fallback_with_yt_dlp_first(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "ytkb.toml"
    write_default_config(config_path)

    result = runner.invoke(
        app,
        [
            "sync",
            "@Example",
            "--config",
            str(config_path),
            "--staged-fallback",
            "--yt-dlp-first",
        ],
    )

    assert result.exit_code != 0
    assert "--staged-fallback is transcript-API-first" in result.output


def test_init_command_creates_config_dirs_and_catalog(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "ytkb.toml"

    result = runner.invoke(app, ["init", "--config", str(config_path)])

    assert result.exit_code == 0
    assert config_path.exists()
    assert (tmp_path / "data/artifacts/channels").is_dir()
    assert (tmp_path / "data/artifacts/videos").is_dir()
    assert (tmp_path / "data/indexes/lancedb").is_dir()
    assert catalog_is_initialized(tmp_path / "data/indexes/catalog.sqlite")


def test_rate_limit_detection_catches_common_youtube_block_messages() -> None:
    assert is_rate_limit_error("HTTP Error 429: Too Many Requests")
    assert is_rate_limit_error("YouTube is blocking requests from your IP")
    assert not is_rate_limit_error("No transcript found for requested language")


def test_connection_broken_transcript_errors_are_retryable_transient() -> None:
    error_class, retryable = classify_transcript_error(
        "Connection broken: IncompleteRead(1287 bytes read, 2781 more expected)"
    )

    assert error_class == "transient"
    assert retryable is True


def test_premature_provider_response_is_retryable_transient() -> None:
    error_class, retryable = classify_transcript_error("Response ended prematurely")

    assert error_class == "transient"
    assert retryable is True


def test_dns_resolution_errors_are_retryable_transient() -> None:
    error_class, retryable = classify_transcript_error("curl: (6) Could not resolve host: www.youtube.com")

    assert error_class == "transient"
    assert retryable is True


def test_status_filters_match_exact_or_prefix_values() -> None:
    assert _matches_status_filters("deferred: rate_limited", ["deferred: rate_limited"])
    assert _matches_status_filters("failed: long provider error", ["failed:"])
    assert _matches_status_filters(None, ["discovered"])
    assert not _matches_status_filters("indexed", ["deferred:"])


def test_source_filters_match_exact_or_prefix_values() -> None:
    assert _matches_source_filters("gemini:gemini-2.5-flash", ["gemini:"])
    assert _matches_source_filters("gemini:gemini-2.5-flash", ["gemini:gemini-2.5-flash"])
    assert not _matches_source_filters("youtube-transcript-api", ["gemini:"])
    assert not _matches_source_filters(None, ["gemini:"])


def test_fallback_only_applies_to_known_fallback_statuses() -> None:
    assert _fallback_only_for_status("deferred: needs_asr_no_captions", True)
    assert _fallback_only_for_status("deferred: needs_asr_bad_captions", True)
    assert _fallback_only_for_status("failed: provider error", True)
    assert not _fallback_only_for_status("deferred: rate_limited", True)
    assert not _fallback_only_for_status("deferred: needs_asr_no_captions", False)


def test_gemini_window_bounds_split_long_videos() -> None:
    assert _window_bounds(None, 900) == [(0, None)]
    assert _window_bounds(800, 900) == [(0, None)]
    assert _window_bounds(1900, 900) == [(0, 900), (900, 1800), (1800, 1900)]


def test_gemini_payload_drops_blank_segments() -> None:
    payload = {"segments": [{"start": 0, "duration": 1, "text": ""}, {"start": 1, "duration": 2, "text": "ok"}]}

    assert _drop_blank_segment_text(payload) == {
        "segments": [{"start": 1, "duration": 2, "text": "ok"}]
    }


def test_lancedb_table_names_supports_new_response_shape() -> None:
    class Response:
        tables = ["chunks"]

    class DB:
        def list_tables(self):
            return Response()

    assert _lancedb_table_names(DB()) == ["chunks"]


def test_ytdlp_bot_block_messages_are_retryable() -> None:
    assert _is_ytdlp_block_error("Sign in to confirm you’re not a bot")
    assert _is_ytdlp_block_error("HTTP Error 429: Too Many Requests")
    assert not _is_ytdlp_block_error("yt-dlp did not write json3 subtitles")


def test_generic_proxy_pool_is_selected_deterministically(tmp_path: Path) -> None:
    config_path = tmp_path / "ytkb.toml"
    write_default_config(config_path)
    config = load_config(config_path).model_copy(
        update={
            "proxy": load_config(config_path).proxy.model_copy(
                update={
                    "enabled": True,
                    "kind": "generic",
                    "urls": [
                        "http://user:pass@proxy-a.example:8080",
                        "http://user:pass@proxy-b.example:8080",
                    ],
                }
            )
        }
    )

    first = proxy_url_for_ytdlp(config.proxy, key="video123")
    second = proxy_url_for_ytdlp(config.proxy, key="video123")

    assert first == second
    assert first in config.proxy.urls
    assert redact_proxy_url(first).startswith("http://***:***@")


def test_env_can_enable_webshare_proxy_and_gemini(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "ytkb.toml"
    write_default_config(config_path)
    monkeypatch.setenv("YTKB_WEBSHARE_USERNAME", "proxy-user")
    monkeypatch.setenv("YTKB_WEBSHARE_PASSWORD", "proxy-pass")
    monkeypatch.setenv("YTKB_WEBSHARE_DOMAIN", "p.webshare.io")
    monkeypatch.setenv("YTKB_WEBSHARE_PORT", "80")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("YTKB_GEMINI_MODEL", "gemini-test-model")
    monkeypatch.setenv("YTKB_GEMINI_MEDIA_RESOLUTION", "medium")
    monkeypatch.setenv("YTKB_GEMINI_REQUEST_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("YTKB_GEMINI_WINDOW_SECONDS", "600")

    config = apply_env_to_config(load_config(config_path))

    assert config.proxy.enabled is True
    assert config.proxy.kind == "webshare"
    assert proxy_url_for_ytdlp(config.proxy, key="video123") == "http://proxy-user-rotate:proxy-pass@p.webshare.io:80/"
    assert config.gemini.enabled is True
    assert config.gemini.model == "gemini-test-model"
    assert config.gemini.media_resolution == "medium"
    assert config.gemini.request_timeout_seconds == 45.0
    assert config.gemini.window_seconds == 600
