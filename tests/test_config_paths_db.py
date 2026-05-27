import io
import json
import plistlib
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import zipfile
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from yutome import setup_prompts
from yutome.cli import (
    BACK_CHOICE,
    MCPB_MANIFEST_VERSION,
    _build_yutome_mcpb,
    _channel_picker_labels,
    _prompt_channels_to_select,
    _prompt_public_subscription_target,
    _yutome_mcpb_manifest,
    app,
    _parse_channel_selection,
)
from yutome.cli._bridge import (
    _bridge_connection_error_message,
    _bridge_pid_path,
    _bridge_start_detached,
    _finalize_remote_bridge_setup,
    _installed_bridge_config_path,
    _launchd_plist_content,
    _read_bridge_pid,
    _restart_bridge_after_deploy,
    _stop_bridge_pid,
    _systemd_unit_content,
)
from yutome.cli._worker_deploy import (
    _active_oauth_kv_id,
    _cloudflare_deploy_runtime_problem,
    _deploy_tracked_worker,
    _ensure_oauth_kv_namespace,
    _ensure_workers_dev_subdomain,
    _ensure_wrangler_authenticated,
    _parse_node_version,
    _push_wrangler_secret,
    _run_command_streamed,
    _strip_oauth_kv_binding,
    _tracked_worker_path,
    _wrangler_whoami_authenticated,
    _write_generated_wrangler_config,
)
from yutome.channels import (
    channel_from_input,
    import_channels_from_file,
    list_library_channels,
    upsert_library_channel,
)
from yutome.config import load_config, write_default_config
from yutome.db import bootstrap_catalog, catalog_is_initialized, catalog_tables, connect_catalog, fts5_available
from yutome.embeddings import _lancedb_table_names
from yutome.env import apply_env_to_config
from yutome.gemini import _drop_blank_segment_text, _window_bounds
from yutome.indexer import (
    _fallback_only_for_status,
    _matches_source_filters,
    _matches_status_filters,
    _metadata_artifact_payload,
    classify_transcript_error,
    is_rate_limit_error,
)
from yutome.paths import ProjectPaths
from yutome.store import list_catalog_videos
from yutome.sources import (
    import_sources_from_file,
    list_library_sources,
    source_from_input,
    upsert_library_source,
)
from yutome.youtube import _is_ytdlp_block_error, proxy_url_for_ytdlp, redact_proxy_url
from yutome.youtube import format_ytdlp_failure, is_proxy_payment_error
from yutome.youtube_oauth import OAuthClient, _authorization_url, _token_is_valid, load_oauth_client


def test_default_config_round_trip(tmp_path: Path) -> None:
    config_path = tmp_path / "yutome.toml"

    written = write_default_config(config_path)
    config = load_config(config_path)

    assert written is True
    assert config.backfill.workers == 2
    assert config.backfill.batch_size == 25
    assert config.scheduler.cadence_hours == 3
    assert config.youtube.api_key_env == "YUTOME_YOUTUBE_API_KEY"
    assert "chrome" in config.youtube.browser_cookie_browsers
    assert config.asr.provider == "faster-whisper"
    assert config.asr.model == "small.en"
    assert config.transcripts.allow_translated_captions is False
    assert config.transcripts.request_timeout_seconds == 30.0
    assert config.transcripts.prefer_ytdlp_subtitles is False
    assert config.transcript_cleanup.enabled is True
    assert config.transcript_cleanup.auto_after_sync is True
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
    assert config.yt_dlp.retries_when_blocked == 3
    assert config.yt_dlp.subtitle_retries_when_blocked == 3
    assert config.yt_dlp.profile == "python-no-js"
    assert config.yt_dlp.fallback_profile == "current"
    assert config.yt_dlp.profile_fallback_enabled is True
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
    assert config.hosted.enabled is False
    assert config.hosted.workspace_id == ""
    assert str(config.hosted.usage_ledger_path) == "data/hosted/usage_events.jsonl"
    assert config.hosted.postgres_url_env == "YUTOME_POSTGRES_URL"


def test_project_paths_include_timestamped_and_plain_transcript_artifacts(tmp_path: Path) -> None:
    config_path = tmp_path / "yutome.toml"
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


def test_metadata_artifact_payload_omits_bulky_ytdlp_fields() -> None:
    compacted = _metadata_artifact_payload(
        {
            "id": "video123",
            "title": "Example",
            "duration": 120,
            "automatic_captions": {"en": [{"url": "https://example.com/caption.json3"}]},
            "subtitles": {"en": [{"url": "https://example.com/manual.vtt"}]},
            "formats": [{"format_id": "18", "url": "https://example.com/video.mp4"}],
            "heatmap": [{"start_time": 0, "value": 0.5}],
        }
    )

    assert compacted["id"] == "video123"
    assert compacted["title"] == "Example"
    assert "automatic_captions" not in compacted
    assert "subtitles" not in compacted
    assert "formats" not in compacted
    assert "heatmap" not in compacted
    assert compacted["_yutome_artifact"]["compacted"] is True
    assert compacted["_yutome_artifact"]["omitted_fields"] == [
        "automatic_captions",
        "formats",
        "heatmap",
        "subtitles",
    ]
    assert compacted["_yutome_artifact"]["source_metadata_hash"]


def test_bootstrap_catalog_creates_expected_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "data/indexes/catalog.sqlite"

    bootstrap_catalog(db_path)

    tables = catalog_tables(db_path)
    assert catalog_is_initialized(db_path)
    assert fts5_available()
    assert {
        "channels",
        "library_channels",
        "library_sources",
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


def test_source_inputs_detect_channels_and_videos() -> None:
    channel = source_from_input("@LeoandLongevity", title="Leo and Longevity")
    video = source_from_input("https://youtu.be/UTuuTTnjxMQ")
    short = source_from_input("https://www.youtube.com/shorts/abcdefghijk")

    assert channel is not None
    assert channel.source_type == "youtube_channel"
    assert channel.handle == "LeoandLongevity"
    assert video is not None
    assert video.source_type == "youtube_video"
    assert video.video_id == "UTuuTTnjxMQ"
    assert video.source_url == "https://www.youtube.com/watch?v=UTuuTTnjxMQ"
    assert short is not None
    assert short.source_type == "youtube_video"
    assert short.video_id == "abcdefghijk"


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


def test_import_sources_csv_supports_takeout_channels(tmp_path: Path) -> None:
    csv_path = tmp_path / "subscriptions.csv"
    csv_path.write_text(
        "Channel Id,Channel Url,Channel Title\n"
        "UCabc12345678901234567890,https://www.youtube.com/channel/UCabc12345678901234567890,Example Channel\n",
        encoding="utf-8",
    )

    imported = import_sources_from_file(csv_path)

    assert len(imported) == 1
    assert imported[0].source_type == "youtube_channel"
    assert imported[0].channel_id == "UCabc12345678901234567890"


def test_add_cli_adds_source_and_internal_channel(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)

    add_result = runner.invoke(app, ["--config", str(config_path), "corpus", "add", "@LeoandLongevity"])

    assert add_result.exit_code == 0
    with connect_catalog(tmp_path / "data/indexes/catalog.sqlite") as connection:
        sources = list_library_sources(connection)
        channels = list_library_channels(connection)
    assert len(sources) == 1
    assert sources[0].source_type == "youtube_channel"
    assert len(channels) == 1
    assert channels[0].handle == "LeoandLongevity"


def test_channels_namespace_is_not_public_cli() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["channels", "--help"])

    assert result.exit_code != 0


def test_add_cli_accepts_video_source(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)

    add_result = runner.invoke(
        app,
        ["--config", str(config_path), "corpus", "add", "https://www.youtube.com/watch?v=UTuuTTnjxMQ"],
    )

    assert add_result.exit_code == 0
    with connect_catalog(tmp_path / "data/indexes/catalog.sqlite") as connection:
        sources = list_library_sources(connection)
    assert len(sources) == 1
    assert sources[0].source_type == "youtube_video"
    assert sources[0].video_id == "UTuuTTnjxMQ"


def test_sync_video_url_dispatches_as_video_source(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    captured: dict[str, object] = {}

    def fake_run_sync_targets(**kwargs):  # noqa: ANN003
        captured.update(kwargs)

    monkeypatch.setattr("yutome.cli._legacy._run_sync_targets", fake_run_sync_targets)

    result = runner.invoke(app, ["--config", str(config_path), "corpus", "sync", "https://youtu.be/UTuuTTnjxMQ"])

    assert result.exit_code == 0
    assert captured["sync_targets"] == [
        ("https://www.youtube.com/watch?v=UTuuTTnjxMQ", "UTuuTTnjxMQ", "youtube_video")
    ]


def test_upsert_library_channel_with_channel_id_creates_catalog_placeholder(tmp_path: Path) -> None:
    db_path = tmp_path / "data/indexes/catalog.sqlite"
    bootstrap_catalog(db_path)
    channel = channel_from_input(
        "UCabc12345678901234567890",
        title="Example Channel",
        import_source="youtube-oauth",
    )

    assert channel is not None
    with connect_catalog(db_path) as connection:
        upsert_library_channel(connection, channel)
        connection.commit()
        library_rows = connection.execute(
            "SELECT channel_id, title, import_source FROM library_channels"
        ).fetchall()
        catalog_rows = connection.execute(
            "SELECT channel_id, title, source_url FROM channels"
        ).fetchall()

    assert [dict(row) for row in library_rows] == [
        {
            "channel_id": "UCabc12345678901234567890",
            "title": "Example Channel",
            "import_source": "youtube-oauth",
        }
    ]
    assert [dict(row) for row in catalog_rows] == [
        {
            "channel_id": "UCabc12345678901234567890",
            "title": "Example Channel",
            "source_url": "https://www.youtube.com/channel/UCabc12345678901234567890",
        }
    ]


def test_upsert_library_source_mirrors_channel_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "data/indexes/catalog.sqlite"
    bootstrap_catalog(db_path)
    source = source_from_input("UCabc12345678901234567890", title="Example Channel", import_source="manual")

    assert source is not None
    with connect_catalog(db_path) as connection:
        upsert_library_source(connection, source)
        connection.commit()
        sources = list_library_sources(connection)
        channels = list_library_channels(connection)

    assert sources[0].channel_id == "UCabc12345678901234567890"
    assert channels[0].channel_id == "UCabc12345678901234567890"


def test_bootstrap_migrates_library_channels_into_sources(tmp_path: Path) -> None:
    db_path = tmp_path / "data/indexes/catalog.sqlite"
    bootstrap_catalog(db_path)
    with connect_catalog(db_path) as connection:
        connection.execute("DELETE FROM library_sources")
        connection.execute(
            """
            INSERT INTO library_channels(
                library_channel_id, source, source_url, channel_id, handle, title, selected, import_source
            )
            VALUES ('legacy', 'youtube:handle:legacy', 'https://www.youtube.com/@legacy',
                    NULL, 'legacy', 'Legacy', 1, 'manual')
            """
        )
        connection.commit()

    bootstrap_catalog(db_path)

    with connect_catalog(db_path) as connection:
        sources = list_library_sources(connection)
    assert len(sources) == 1
    assert sources[0].source_type == "youtube_channel"
    assert sources[0].handle == "legacy"


def test_sync_help_hides_provider_policy_flags() -> None:
    runner = CliRunner()

    result = runner.invoke(app, ["corpus", "sync", "--help"])

    assert result.exit_code == 0
    assert "--staged-fallback" not in result.output
    assert "--yt-dlp-first" not in result.output
    assert "--no-yt-dlp-fallback" not in result.output
    assert "--defer-metadata" not in result.output


def test_init_command_is_removed_from_public_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    result = runner.invoke(app, ["--config", str(config_path), "init"])

    assert result.exit_code != 0
    assert "No such command" in result.output


def test_setup_command_creates_first_run_files_without_prompting(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    result = runner.invoke(app, ["--config", str(config_path), "setup", "--yes"])

    assert result.exit_code == 0
    assert config_path.exists()
    assert (tmp_path / ".env").exists()
    assert catalog_is_initialized(tmp_path / "data/indexes/catalog.sqlite")
    assert "Next steps:" in result.output
    assert "Optional semantic search" in result.output
    assert "Use Yutome from your AI assistant:" in result.output
    assert "Claude Desktop" in result.output
    assert "Cursor" in result.output
    assert "transcripts stay on this" in result.output
    assert "Yutome Desktop offline" in result.output
    assert "Optional next step: yutome connect --app claude" in result.output


def test_setup_command_can_add_initial_channel(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    result = runner.invoke(app, ["--config", str(config_path), "setup", "@YesTheory", "--yes"])

    assert result.exit_code == 0
    with connect_catalog(tmp_path / "data/indexes/catalog.sqlite") as connection:
        channels = list_library_channels(connection)
    assert len(channels) == 1
    assert channels[0].handle == "YesTheory"


def test_setup_command_can_write_webshare_credentials_interactively(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    result = runner.invoke(
        app,
        ["--config", str(config_path), "setup", "@YesTheory"],
        input="y\nproxy-user\nproxy-pass\n\n\nn\nn\n3\nn\nn\n4\n",
    )

    assert result.exit_code == 0
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "YUTOME_WEBSHARE_USERNAME=proxy-user" in env_text
    assert "YUTOME_WEBSHARE_PASSWORD=proxy-pass" in env_text
    assert "YUTOME_WEBSHARE_DOMAIN=p.webshare.io" in env_text
    assert "YUTOME_WEBSHARE_PORT=80" in env_text
    assert "proxy-pass" not in result.output


def test_setup_command_can_enable_semantic_search_interactively(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    result = runner.invoke(
        app,
        ["--config", str(config_path), "setup"],
        input="n\nn\ny\nvoyage-test-key\n3\nn\nn\n4\n",
    )

    assert result.exit_code == 0
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    config = load_config(config_path)
    assert "VOYAGE_API_KEY=voyage-test-key" in env_text
    assert "voyage-test-key" not in result.output
    assert config.embeddings.enabled is True
    assert 'yutome search find "topic I remember" --mode hybrid' in result.output


def test_setup_imports_subscriptions_then_selects_channels(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    def fake_fetch(**kwargs):  # noqa: ANN003
        return [
            channel_from_input("UC1111111111111111111111", title="Alpha", import_source="youtube-browser-cookies"),
            channel_from_input("UC2222222222222222222222", title="Beta", import_source="youtube-browser-cookies"),
            channel_from_input("UC3333333333333333333333", title="Gamma", import_source="youtube-browser-cookies"),
        ]

    monkeypatch.setattr("yutome.cli._legacy.fetch_user_subscription_channels_from_browser", fake_fetch)

    result = runner.invoke(
        app,
        ["--config", str(config_path), "setup"],
        input="n\nn\nn\n1\nn\n1,3\nn\n4\n",
    )

    assert result.exit_code == 0
    assert "Added 2 selected channels to the library; skipped 1" in result.output
    with connect_catalog(tmp_path / "data/indexes/catalog.sqlite") as connection:
        channels = list_library_channels(connection)
    selected = {channel.title for channel in channels if channel.selected}
    unselected = {channel.title for channel in channels if not channel.selected}
    assert selected == {"Alpha", "Gamma"}
    assert unselected == set()
    assert {channel.title for channel in channels} == {"Alpha", "Gamma"}


def test_setup_can_index_subset_of_added_channels(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    captured: dict[str, object] = {}

    def fake_fetch(**kwargs):  # noqa: ANN003
        return [
            channel_from_input("UC1111111111111111111111", title="Alpha", import_source="youtube-browser-cookies"),
            channel_from_input("UC2222222222222222222222", title="Beta", import_source="youtube-browser-cookies"),
            channel_from_input("UC3333333333333333333333", title="Gamma", import_source="youtube-browser-cookies"),
        ]

    def fake_run_sync_targets(**kwargs):  # noqa: ANN003
        captured.update(kwargs)

    monkeypatch.setattr("yutome.cli._legacy.fetch_user_subscription_channels_from_browser", fake_fetch)
    monkeypatch.setattr("yutome.cli._legacy._run_sync_targets", fake_run_sync_targets)

    result = runner.invoke(
        app,
        ["--config", str(config_path), "setup"],
        input="n\nn\nn\n1\nn\nall\ny\n1-2\n50\n4\n",
    )

    assert result.exit_code == 0
    with connect_catalog(tmp_path / "data/indexes/catalog.sqlite") as connection:
        channels = list_library_channels(connection)
    assert {channel.title for channel in channels} == {"Alpha", "Beta", "Gamma"}
    assert all(channel.selected for channel in channels)
    assert captured["sync_targets"] == [
        ("https://www.youtube.com/channel/UC1111111111111111111111", "Alpha", "youtube_channel"),
        ("https://www.youtube.com/channel/UC2222222222222222222222", "Beta", "youtube_channel"),
    ]
    assert captured["effective_max_process"] == 50


def test_channel_selection_parser_supports_ranges_and_sentinels() -> None:
    assert _parse_channel_selection("1,3-4", 5) == {0, 2, 3}
    assert _parse_channel_selection("all", 3) == {0, 1, 2}
    assert _parse_channel_selection("none", 3) == set()


def test_channel_selection_parser_rejects_invalid_indexes() -> None:
    try:
        _parse_channel_selection("0", 3)
    except ValueError as exc:
        assert "out of range" in str(exc)
    else:  # pragma: no cover - assertion guard.
        raise AssertionError("expected invalid selection")


def test_channel_picker_labels_hide_ids_and_sources() -> None:
    channels = [
        channel_from_input("UC1111111111111111111111", title="Alpha", import_source="youtube-browser-cookies"),
        channel_from_input("UC2222222222222222222222", title="Alpha", import_source="youtube-browser-cookies"),
        channel_from_input("@beta", title="Beta", import_source="youtube-browser-cookies"),
    ]

    labels = _channel_picker_labels(channels)

    assert labels[0] == "Alpha - duplicate 1"
    assert labels[1] == "Alpha - duplicate 2"
    assert labels[2] == "Beta"
    rendered = " ".join(labels.values())
    assert "UC1111111111111111111111" not in rendered
    assert "youtube-browser-cookies" not in rendered


def test_interactive_channel_picker_uses_searchable_checkbox(monkeypatch) -> None:  # noqa: ANN001
    channels = [
        channel_from_input("UC2222222222222222222222", title="Beta", import_source="youtube-browser-cookies"),
        channel_from_input("UC1111111111111111111111", title="Alpha", import_source="youtube-browser-cookies"),
    ]
    captured: dict[str, object] = {}
    cleared: list[bool] = []

    def fake_checkbox(message: str, choices: list[str], **kwargs):  # noqa: ANN003
        captured.update({"message": message, "choices": choices, **kwargs})
        return ["All channels"]

    monkeypatch.setattr("yutome.setup_prompts.is_interactive", lambda: True)
    monkeypatch.setattr("yutome.setup_prompts.checkbox", fake_checkbox)
    monkeypatch.setattr("yutome.cli._legacy.typer.clear", lambda: cleared.append(True))

    selected = _prompt_channels_to_select(channels, title="Choose channels", allow_back=True)

    assert [channel.title for channel in selected or []] == ["Alpha", "Beta"]
    assert cleared == [True]
    assert captured["message"] == "Choose channels"
    assert captured["use_search_filter"] is True
    assert captured["erase_when_done"] is True
    assert captured["validate"]([]) == "Select at least one channel, All channels, or Back."
    assert captured["validate"](["All channels"]) is True
    assert BACK_CHOICE in captured["choices"]
    rendered = " ".join(captured["choices"])
    assert "UC1111111111111111111111" not in rendered
    assert "youtube-browser-cookies" not in rendered


def test_interactive_channel_picker_prints_compact_context(monkeypatch, capsys) -> None:  # noqa: ANN001
    channels = [
        channel_from_input("UC1111111111111111111111", title="Alpha", import_source="youtube-browser-cookies"),
    ]

    monkeypatch.setattr("yutome.setup_prompts.is_interactive", lambda: True)
    monkeypatch.setattr("yutome.setup_prompts.checkbox", lambda *args, **kwargs: [])
    monkeypatch.setattr("yutome.cli._legacy.typer.clear", lambda: None)

    _prompt_channels_to_select(channels, title="Choose channels", allow_back=True)

    output = capsys.readouterr().out
    assert "Found 1 channel." in output
    assert "Type to filter; press space to select; press enter to confirm." in output
    assert "Choose Back to return to the previous step." in output


def test_interactive_channel_picker_omits_back_context_without_back(monkeypatch, capsys) -> None:  # noqa: ANN001
    channels = [
        channel_from_input("UC1111111111111111111111", title="Alpha", import_source="youtube-browser-cookies"),
        channel_from_input("UC2222222222222222222222", title="Beta", import_source="youtube-browser-cookies"),
    ]

    monkeypatch.setattr("yutome.setup_prompts.is_interactive", lambda: True)
    monkeypatch.setattr("yutome.setup_prompts.checkbox", lambda *args, **kwargs: [])
    monkeypatch.setattr("yutome.cli._legacy.typer.clear", lambda: None)

    _prompt_channels_to_select(channels, title="Choose channels", allow_back=False)

    output = capsys.readouterr().out
    assert "Found 2 channels." in output
    assert "Choose Back to return to the previous step." not in output


def test_interactive_channel_picker_requires_selection_without_back(monkeypatch) -> None:  # noqa: ANN001
    channels = [
        channel_from_input("UC1111111111111111111111", title="Alpha", import_source="youtube-browser-cookies"),
    ]
    captured: dict[str, object] = {}

    def fake_checkbox(message: str, choices: list[str], **kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return ["Alpha"]

    monkeypatch.setattr("yutome.setup_prompts.is_interactive", lambda: True)
    monkeypatch.setattr("yutome.setup_prompts.checkbox", fake_checkbox)
    monkeypatch.setattr("yutome.cli._legacy.typer.clear", lambda: None)

    _prompt_channels_to_select(channels, title="Choose channels", allow_back=False)

    assert captured["validate"]([]) == "Select at least one channel or All channels."
    assert captured["validate"](["Alpha"]) is True


def test_searchable_checkbox_disables_jk_navigation(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, object] = {}

    class FakeQuestion:
        def ask(self) -> list[str]:
            return ["Alpha"]

    def fake_checkbox(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return FakeQuestion()

    monkeypatch.setattr("yutome.setup_prompts._is_tty", lambda: True)
    monkeypatch.setattr("questionary.checkbox", fake_checkbox)

    selected = setup_prompts.checkbox(
        "Choose",
        ["Alpha"],
        use_search_filter=True,
        erase_when_done=True,
        validate=lambda selected: bool(selected) or "Choose something",
    )

    assert selected == ["Alpha"]
    assert captured["use_search_filter"] is True
    assert captured["use_jk_keys"] is False
    assert captured["erase_when_done"] is True
    assert captured["validate"]([]) == "Choose something"


def test_public_subscription_prompt_can_go_back_from_choice(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr("yutome.setup_prompts.is_interactive", lambda: True)
    monkeypatch.setattr(
        "yutome.setup_prompts.select",
        lambda message, choices, default=None: "Back - choose a different import method",
    )

    assert _prompt_public_subscription_target() == BACK_CHOICE


def test_public_subscription_prompt_can_go_back_from_target(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr("yutome.setup_prompts.is_interactive", lambda: True)
    monkeypatch.setattr(
        "yutome.setup_prompts.select",
        lambda message, choices, default=None: "Yes - stack another channel's public subscriptions",
    )
    monkeypatch.setattr("yutome.setup_prompts.text", lambda message: "b")

    assert _prompt_public_subscription_target() == BACK_CHOICE


def test_yutome_mcpb_manifest_shape() -> None:
    manifest = _yutome_mcpb_manifest("/usr/local/bin/yutome", Path("/tmp/yutome.toml"))

    assert manifest["manifest_version"] == MCPB_MANIFEST_VERSION
    assert manifest["name"] == "yutome"
    assert manifest["display_name"] == "Yutome"
    assert manifest["server"]["type"] == "binary"
    assert manifest["server"]["entry_point"] == "/usr/local/bin/yutome"
    assert manifest["server"]["mcp_config"]["command"] == "/usr/local/bin/yutome"
    assert manifest["server"]["mcp_config"]["args"] == [
        "--config",
        "/tmp/yutome.toml",
        "serve",
        "mcp",
    ]
    # Version must come from the installed package, not be empty.
    assert manifest["version"]


def test_build_yutome_mcpb_writes_zip_with_manifest(tmp_path: Path) -> None:
    config_path = tmp_path / "yutome.toml"
    config_path.write_text("# stub\n", encoding="utf-8")
    output_path = tmp_path / "yutome.mcpb"

    bundle = _build_yutome_mcpb(config_path, output_path=output_path)

    assert bundle == output_path
    assert bundle.exists()
    with zipfile.ZipFile(bundle) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        manifest = json.loads(zf.read("manifest.json"))
    assert manifest["server"]["type"] == "binary"
    # Bundled config path must be absolute so Claude Desktop can launch the binary
    # from anywhere — relative paths break the install on macOS sandbox launches.
    assert Path(manifest["server"]["mcp_config"]["args"][1]).is_absolute()


def test_node_version_parser() -> None:
    assert _parse_node_version("v22.12.0") == (22, 12, 0)
    assert _parse_node_version("/opt/node (v20.19.5)") == (20, 19, 5)
    assert _parse_node_version("not node") is None


def test_cloudflare_deploy_runtime_rejects_node_below_wrangler_floor(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr("shutil.which", lambda command: f"/bin/{command}")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == ["/bin/node", "--version"]
        return subprocess.CompletedProcess(command, 0, stdout="v20.19.5\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert "Node.js 22.0.0+ is required" in (_cloudflare_deploy_runtime_problem() or "")


def test_streamed_command_uses_pty_for_interactive_terminal(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    calls: list[tuple[list[str], Path]] = []
    monkeypatch.setattr("yutome.cli._worker_deploy._should_use_interactive_command_stream", lambda: True)

    def fake_pty(command: list[str], *, cwd: Path) -> tuple[int, str]:
        calls.append((command, cwd))
        return 0, "done\n"

    monkeypatch.setattr("yutome.cli._worker_deploy._run_command_streamed_pty", fake_pty)

    assert _run_command_streamed(["wrangler", "deploy"], cwd=tmp_path) == (0, "done\n")
    assert calls == [(["wrangler", "deploy"], tmp_path)]


def test_import_youtube_uses_browser_cookie_source(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)

    def fake_fetch(**kwargs):  # noqa: ANN003
        return [
            channel_from_input(
                "UC9999999999999999999999",
                title="Cookie Channel",
                import_source="youtube-browser-cookies",
            )
        ]

    monkeypatch.setattr("yutome.cli._legacy.fetch_user_subscription_channels_from_browser", fake_fetch)

    result = runner.invoke(app, ["--config", str(config_path), "corpus", "import-youtube"])

    assert result.exit_code == 0
    assert "youtube-browser-cookies" in result.output
    with connect_catalog(tmp_path / "data/indexes/catalog.sqlite") as connection:
        channels = list_library_channels(connection)
    assert len(channels) == 1
    assert channels[0].title == "Cookie Channel"
    assert channels[0].selected is True


def test_import_youtube_public_target_uses_api_when_key_exists(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    (tmp_path / ".env").write_text("YUTOME_YOUTUBE_API_KEY=test-api-key\n", encoding="utf-8")
    calls: list[tuple[str, str]] = []

    def fake_api(target: str, *, api_key: str):  # noqa: ANN001
        calls.append((target, api_key))
        return [
            channel_from_input(
                "UC8888888888888888888888",
                title="Public API Channel",
                import_source="youtube-public-api",
            )
        ]

    monkeypatch.setattr("yutome.cli._legacy.fetch_public_subscription_channels_from_api", fake_api)

    result = runner.invoke(app, ["--config", str(config_path), "corpus", "import-youtube", "@source"])

    assert result.exit_code == 0
    assert calls == [("@source", "test-api-key")]
    assert "youtube-public-api" in result.output


def test_remote_prepare_generates_http_token_without_printing_by_default(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    result = runner.invoke(app, ["--config", str(config_path), "serve", "remote", "prepare"])

    assert result.exit_code == 0
    env_values = {}
    for line in (tmp_path / ".env").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env_values[key] = value
    assert env_values["YUTOME_HTTP_TOKEN"]
    assert env_values["YUTOME_HTTP_TOKEN"] not in result.output


def test_connect_with_endpoint_writes_remote_state(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    result = runner.invoke(app, ["--config", str(config_path), "connect", "--endpoint", "https://example.workers.dev"])

    assert result.exit_code == 0
    state_path = tmp_path / "data/remote/connection.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["provider"] == "cloudflare"
    assert state["mode"] == "connector_only"
    assert state["endpoint_url"] == "https://example.workers.dev"
    assert state["mcp_url"] == "https://example.workers.dev/mcp"
    assert state["cloud_resources"] == {}
    assert "Connect this MCP URL" in result.output
    assert "across your devices" in result.output
    assert "same Claude account" in result.output
    assert "Customize > Connectors" in result.output
    assert "support.claude.com" in result.output
    assert "Apps" in result.output
    assert "developers.openai.com/api/docs/guides/developer-mode" in result.output
    assert "developers.openai.com/api/docs/mcp" in result.output
    assert "Settings > Apps > Advanced settings" in result.output
    assert "Create app" in result.output
    assert "/mcp URL" in result.output
    assert "OAuth/authenticated" in result.output
    assert "+ > More" in result.output
    assert "modelcontextprotocol.io/docs/concepts/transports" in result.output
    assert "developers.cloudflare.com/agents/guides/test-remote-mcp-server" in result.output
    assert "Streamable HTTP" in result.output
    assert "cannot answer assistant requests until you save the Worker secrets locally" in result.output
    assert "yutome connect --endpoint <url> --relay-token <token> --pairing-code <code>" in result.output
    assert "Yutome Desktop offline" in result.output
    assert "No Yutome account, Auth0, Clerk, or Cloudflare Access" in result.output
    assert "VOYAGE_API_KEY" not in result.output
    assert "YUTOME_WEBSHARE" not in result.output


def test_connect_with_endpoint_and_tokens_writes_usable_remote_state(
    monkeypatch, tmp_path: Path
) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    finalized: list[Path] = []

    def fake_finalize(*, config_path: Path, paths: ProjectPaths, before_persistence=None) -> None:  # noqa: ANN001
        finalized.append(config_path)
        if before_persistence is not None:
            before_persistence()

    monkeypatch.setattr("yutome.cli._bridge._finalize_remote_bridge_setup", fake_finalize)

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "connect",
            "--endpoint",
            "https://example.workers.dev",
            "--relay-token",
            " relay-secret \n",
            "--pairing-code",
            " pair12 ",
        ],
    )

    assert result.exit_code == 0
    state = json.loads((tmp_path / "data/remote/connection.json").read_text(encoding="utf-8"))
    assert state["relay_token"] == "relay-secret"
    assert state["pairing_code"] == "PAIR12"
    assert "Code: PAIR12" in result.output
    assert "yutome serve bridge" in result.output
    assert finalized == [config_path]


def test_connect_with_endpoint_without_relay_token_does_not_finalize_bridge(
    monkeypatch, tmp_path: Path
) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    def fail_finalize(**_kwargs: object) -> None:
        raise AssertionError("bridge finalization should require a relay token")

    monkeypatch.setattr("yutome.cli._bridge._finalize_remote_bridge_setup", fail_finalize)

    result = runner.invoke(
        app,
        ["--config", str(config_path), "connect", "--endpoint", "https://example.workers.dev"],
    )

    assert result.exit_code == 0
    state = json.loads((tmp_path / "data/remote/connection.json").read_text(encoding="utf-8"))
    assert state.get("relay_token") is None


def test_connect_without_endpoint_prints_deploy_instructions_without_provider_keys(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    result = runner.invoke(app, ["--config", str(config_path), "connect"])

    assert result.exit_code == 0
    assert "yutome connect --deploy" in result.output
    assert "basic laptop-backed connector is designed for Cloudflare's free Workers plan" in result.output
    assert "Always-on/offline search is a later mode and may require enabling Cloudflare billing" in result.output
    assert "Tracked TypeScript Worker project lives at:" in result.output
    # The new connect flow does NOT generate files under data/remote — the
    # tracked TypeScript Worker project at cloudflare/yutome-capsule/ is the source.
    assert not (tmp_path / "data/remote/cloudflare-worker").exists()
    assert not (tmp_path / "data/remote/connection.json").exists()
    assert "VOYAGE_API_KEY" not in result.output
    assert "YUTOME_WEBSHARE" not in result.output


def test_tracked_worker_path_uses_packaged_bundle_when_repo_sibling_missing(
    monkeypatch, tmp_path: Path
) -> None:  # noqa: ANN001
    package_dir = tmp_path / "tools" / "yutome" / "lib" / "python3.13" / "site-packages" / "yutome"
    worker_project = package_dir / "cloudflare" / "yutome-capsule"
    worker_project.mkdir(parents=True)
    fake_cli_module = package_dir / "cli" / "_worker_deploy.py"
    fake_cli_module.parent.mkdir()
    fake_cli_module.write_text("", encoding="utf-8")

    monkeypatch.setattr("yutome.cli._worker_deploy.__file__", str(fake_cli_module))

    assert _tracked_worker_path() == worker_project


def test_connect_deploy_invokes_tracked_worker(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    finalized: list[Path] = []

    monkeypatch.setattr(
        "yutome.cli._worker_deploy._deploy_tracked_worker",
        lambda paths, refresh_contract=True, relay_token=None, pairing_code=None: (
            "https://example.workers.dev",
            "yutome-remote-mcp",
            "fake-relay-token",
            "ABCD12",
        ),
    )
    def fake_finalize(*, config_path: Path, paths: ProjectPaths, before_persistence=None) -> None:  # noqa: ANN001
        finalized.append(config_path)
        if before_persistence is not None:
            before_persistence()

    monkeypatch.setattr("yutome.cli._bridge._finalize_remote_bridge_setup", fake_finalize)

    result = runner.invoke(app, ["--config", str(config_path), "connect", "--deploy"])

    assert result.exit_code == 0
    state = json.loads((tmp_path / "data/remote/connection.json").read_text(encoding="utf-8"))
    assert state["endpoint_url"] == "https://example.workers.dev"
    assert state["mcp_url"] == "https://example.workers.dev/mcp"
    assert state["cloud_resources"]["cloudflare_worker_name"] == "yutome-remote-mcp"
    assert state["relay_token"] == "fake-relay-token"
    assert state["pairing_code"] == "ABCD12"
    # The pairing prose must print the code so the user knows what to paste
    # when Claude/ChatGPT opens the OAuth browser.
    assert "ABCD12" in result.output
    assert finalized == [config_path]


def test_status_reports_remote_unconfigured_and_configured(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)

    missing = runner.invoke(app, ["--config", str(config_path), "status"])
    assert missing.exit_code == 0
    assert "Remote connector:" in missing.output
    assert "not configured" in missing.output

    connect_result = runner.invoke(app, ["--config", str(config_path), "connect", "--endpoint", "https://example.workers.dev/mcp"])
    assert connect_result.exit_code == 0
    configured = runner.invoke(app, ["--config", str(config_path), "status"])
    assert configured.exit_code == 0
    assert "mcp_url=https://example.workers.dev/mcp" in configured.output
    assert "assistant_oauth=client-managed" in configured.output
    assert "bridge_token=missing" in configured.output
    assert "pairing_code=missing" in configured.output
    assert "oauth_storage=worker OAUTH_KV" in configured.output
    assert "offline_search=disabled" in configured.output


def test_remote_status_reports_unconfigured_and_configured(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)

    missing = runner.invoke(app, ["--config", str(config_path), "status"])
    assert missing.exit_code == 0
    assert "Remote connector:" in missing.output
    assert "not configured" in missing.output

    connect_result = runner.invoke(app, ["--config", str(config_path), "connect", "--endpoint", "https://example.workers.dev"])
    assert connect_result.exit_code == 0
    configured = runner.invoke(app, ["--config", str(config_path), "status", "--json"])
    assert configured.exit_code == 0
    payload = json.loads(configured.output)["remote"]
    assert payload["configured"] is True
    assert payload["mcp_url"] == "https://example.workers.dev/mcp"
    assert payload["relay_token_configured"] is False
    assert payload["pairing_code_configured"] is False
    assert payload["token_secret_configured"] is False
    assert payload["assistant_oauth_status"] == "client-managed"
    assert payload["oauth_storage"] == "worker OAUTH_KV"


def test_remote_status_uses_live_worker_relay_status(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    monkeypatch.setattr(
        "yutome.cli._bridge._bridge_start_detached",
        lambda _cfg, paths: (1234, paths.logs_dir / "bridge.log"),
    )
    connect_result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "connect",
            "--endpoint",
            "https://example.workers.dev",
            "--relay-token",
            "relay-secret",
            "--pairing-code",
            "PAIR12",
        ],
    )
    assert connect_result.exit_code == 0
    seen: list[tuple[str, str | None, float | None]] = []

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "bridge_online": True,
                    "last_seen_at": "2026-05-22T16:30:00+00:00",
                }
            ).encode("utf-8")

    def fake_urlopen(request: object, timeout: float | None = None) -> FakeResponse:
        seen.append(
            (
                request.full_url,  # type: ignore[attr-defined]
                request.get_header("Authorization"),  # type: ignore[attr-defined]
                timeout,
            )
        )
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = runner.invoke(app, ["--config", str(config_path), "status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)["remote"]
    assert seen == [("https://example.workers.dev/relay/status", "Bearer relay-secret", 2.0)]
    assert payload["desktop_connection"] == "online (live)"
    assert payload["desktop_connection_source"] == "worker"
    assert payload["last_worker_seen_at"] == "2026-05-22T16:30:00+00:00"
    assert payload["relay_status_error"] is None


def test_remote_status_401_reports_relay_token_mismatch(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    monkeypatch.setattr(
        "yutome.cli._bridge._bridge_start_detached",
        lambda _cfg, paths: (1234, paths.logs_dir / "bridge.log"),
    )
    connect_result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "connect",
            "--endpoint",
            "https://example.workers.dev",
            "--relay-token",
            "relay-secret",
            "--pairing-code",
            "PAIR12",
        ],
    )
    assert connect_result.exit_code == 0

    def fake_urlopen(_request: object, timeout: float | None = None) -> object:
        raise urllib.error.HTTPError(
            "https://example.workers.dev/relay/status",
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b"Unauthorized"),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = runner.invoke(app, ["--config", str(config_path), "status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)["remote"]
    assert "saved relay token was rejected by the deployed Worker" in payload["relay_status_error"]
    assert "YUTOME_RELAY_TOKEN" in payload["relay_status_error"]


def test_bridge_connection_error_message_explains_401() -> None:
    class Response:
        status_code = 401

    exc = RuntimeError("server rejected WebSocket connection: HTTP 401")
    exc.response = Response()  # type: ignore[attr-defined]

    message = _bridge_connection_error_message(exc)

    assert "saved relay token was rejected by the deployed Worker" in message
    assert "YUTOME_RELAY_TOKEN" in message


def test_generated_wrangler_config_keeps_oauth_kv_account_local(tmp_path: Path) -> None:
    worker_project = tmp_path / "worker_project"
    worker_project.mkdir()
    (worker_project / "wrangler.toml").write_text(
        "\n".join(
            [
                'name = "yutome-remote-mcp"',
                'main = "src/index.ts"',
                'compatibility_flags = ["nodejs_compat", "global_fetch_strictly_public"]',
                "[[kv_namespaces]]",
                'binding = "OAUTH_KV"',
                'id = "11111111111111111111111111111111"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    paths = ProjectPaths.from_config(load_config(config_path), project_root=tmp_path)

    stripped = _strip_oauth_kv_binding((worker_project / "wrangler.toml").read_text(encoding="utf-8"))
    assert 'binding = "OAUTH_KV"' not in stripped

    generated = _write_generated_wrangler_config(worker_project, paths, "22222222222222222222222222222222")
    assert generated == tmp_path / "data/remote/cloudflare/wrangler.generated.toml"
    generated_text = generated.read_text(encoding="utf-8")
    assert _active_oauth_kv_id(generated_text) == "22222222222222222222222222222222"
    assert f'main = "{worker_project.resolve()}/src/index.ts"' in generated_text
    assert _active_oauth_kv_id((worker_project / "wrangler.toml").read_text(encoding="utf-8")) == "11111111111111111111111111111111"


def test_connect_deploy_reuses_existing_oauth_kv_namespace(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    worker_project = tmp_path / "worker_project"
    worker_project.mkdir()
    (worker_project / "wrangler.toml").write_text(
        "\n".join(
            [
                'name = "yutome-remote-mcp"',
                'main = "src/index.ts"',
                'compatibility_flags = ["nodejs_compat", "global_fetch_strictly_public"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    paths = ProjectPaths.from_config(load_config(config_path), project_root=tmp_path)
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert kwargs["cwd"] == worker_project
        if command[-1] == "list":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps([{"id": "33333333333333333333333333333333", "title": "OAUTH_KV"}]),
                stderr="",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr("subprocess.run", fake_run)

    generated = _ensure_oauth_kv_namespace(worker_project, paths)

    assert commands == [["npx", "--yes", "wrangler", "kv", "namespace", "list"]]
    assert _active_oauth_kv_id(generated.read_text(encoding="utf-8")) == "33333333333333333333333333333333"


def test_connect_deploy_refreshes_stale_generated_oauth_kv_namespace(
    monkeypatch, tmp_path: Path
) -> None:  # noqa: ANN001
    worker_project = tmp_path / "worker_project"
    worker_project.mkdir()
    (worker_project / "wrangler.toml").write_text(
        "\n".join(
            [
                'name = "yutome-remote-mcp"',
                'main = "src/index.ts"',
                'compatibility_flags = ["nodejs_compat", "global_fetch_strictly_public"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    paths = ProjectPaths.from_config(load_config(config_path), project_root=tmp_path)
    stale = _write_generated_wrangler_config(worker_project, paths, "22222222222222222222222222222222")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert kwargs["cwd"] == worker_project
        if command[-1] == "list":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps([{"id": "33333333333333333333333333333333", "title": "OAUTH_KV"}]),
                stderr="",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr("subprocess.run", fake_run)

    generated = _ensure_oauth_kv_namespace(worker_project, paths)

    assert generated == stale
    assert commands == [["npx", "--yes", "wrangler", "kv", "namespace", "list"]]
    assert _active_oauth_kv_id(generated.read_text(encoding="utf-8")) == "33333333333333333333333333333333"


def test_connect_deploy_explains_missing_workers_dev_subdomain(
    monkeypatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:  # noqa: ANN001
    account_id = "440c16ec06f5321075e4eadf38d4cc6d"
    worker_project = tmp_path / "worker_project"
    worker_project.mkdir()
    (worker_project / "wrangler.toml").write_text('name = "yutome-remote-mcp"\nmain = "src/index.ts"\n', encoding="utf-8")
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    paths = ProjectPaths.from_config(load_config(config_path), project_root=tmp_path)
    generated = tmp_path / "wrangler.generated.toml"

    monkeypatch.setattr("yutome.cli._worker_deploy._tracked_worker_path", lambda: worker_project)
    monkeypatch.setattr("yutome.cli._worker_deploy._require_cloudflare_deploy_runtime", lambda: None)
    monkeypatch.setattr("yutome.cli._worker_deploy._ensure_worker_node_modules", lambda _worker_project: None)
    monkeypatch.setattr("yutome.cli._worker_deploy._ensure_wrangler_authenticated", lambda _worker_project: None)
    monkeypatch.setattr("yutome.cli._worker_deploy._ensure_oauth_kv_namespace", lambda _worker_project, _paths: generated)

    def fake_stream(command: list[str], *, cwd: Path) -> tuple[int, str]:
        assert command == ["npx", "--yes", "wrangler", "deploy", "--config", str(generated)]
        assert cwd == worker_project
        return (
            1,
            "\n".join(
                [
                    f"A request to the Cloudflare API (/accounts/{account_id}/workers/scripts/yutome-remote-mcp) failed.",
                    "You need a workers.dev subdomain in order to proceed. [code: 10063]",
                ]
            ),
        )

    monkeypatch.setattr("yutome.cli._worker_deploy._run_command_streamed", fake_stream)

    with pytest.raises(typer.Exit) as exc:
        _deploy_tracked_worker(paths=paths, refresh_contract=False, relay_token="relay", pairing_code="pair")

    assert exc.value.exit_code == 1
    captured = capsys.readouterr()
    assert "This Cloudflare account has not finished Workers setup yet." in captured.err
    assert f"https://dash.cloudflare.com/{account_id}/workers/onboarding" in captured.err
    assert "rerun `yutome connect --deploy`" in captured.err


def test_connect_deploy_retries_after_recoverable_error_when_interactive(
    monkeypatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:  # noqa: ANN001
    account_id = "1d653327475a6351fab5cbea055baf7c"
    worker_project = tmp_path / "worker_project"
    worker_project.mkdir()
    (worker_project / "wrangler.toml").write_text('name = "yutome-remote-mcp"\nmain = "src/index.ts"\n', encoding="utf-8")
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    paths = ProjectPaths.from_config(load_config(config_path), project_root=tmp_path)
    generated = tmp_path / "wrangler.generated.toml"

    monkeypatch.setattr("yutome.cli._worker_deploy._tracked_worker_path", lambda: worker_project)
    monkeypatch.setattr("yutome.cli._worker_deploy._require_cloudflare_deploy_runtime", lambda: None)
    monkeypatch.setattr("yutome.cli._worker_deploy._ensure_worker_node_modules", lambda _worker_project: None)
    monkeypatch.setattr("yutome.cli._worker_deploy._ensure_wrangler_authenticated", lambda _worker_project: None)
    monkeypatch.setattr("yutome.cli._worker_deploy._ensure_oauth_kv_namespace", lambda _worker_project, _paths: generated)
    monkeypatch.setattr("yutome.cli._worker_deploy._push_wrangler_secret", lambda *a, **k: None)
    # Skip the post-deploy /healthz probe — example.workers.dev doesn't
    # exist, so the real probe would loop for ~60s waiting for DNS.
    monkeypatch.setattr("yutome.cli._worker_deploy._wait_for_worker_online", lambda *a, **k: True)
    monkeypatch.setattr("yutome.setup_prompts.is_interactive", lambda: True)
    monkeypatch.setattr("yutome.setup_prompts.confirm", lambda *_a, **_k: True)

    fail_then_succeed = iter([
        (
            1,
            "\n".join(
                [
                    f"A request to the Cloudflare API (/accounts/{account_id}/workers/scripts/yutome-remote-mcp) failed.",
                    "You need to verify your email address to use Workers. [code: 10034]",
                ]
            ),
        ),
        (0, "Published yutome-remote-mcp https://yutome-remote-mcp.example.workers.dev"),
    ])
    call_count = {"n": 0}

    def fake_stream(command: list[str], *, cwd: Path) -> tuple[int, str]:
        call_count["n"] += 1
        return next(fail_then_succeed)

    monkeypatch.setattr("yutome.cli._worker_deploy._run_command_streamed", fake_stream)

    deployed_url, worker_name, _relay, _pair = _deploy_tracked_worker(
        paths=paths, refresh_contract=False, relay_token="relay", pairing_code="pair"
    )

    assert call_count["n"] == 2
    assert deployed_url == "https://yutome-remote-mcp.example.workers.dev"
    assert worker_name == "yutome-remote-mcp"
    captured = capsys.readouterr()
    assert "hasn't verified its email address yet" in captured.err


def test_connect_deploy_explains_unverified_email(
    monkeypatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:  # noqa: ANN001
    account_id = "1d653327475a6351fab5cbea055baf7c"
    worker_project = tmp_path / "worker_project"
    worker_project.mkdir()
    (worker_project / "wrangler.toml").write_text('name = "yutome-remote-mcp"\nmain = "src/index.ts"\n', encoding="utf-8")
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    paths = ProjectPaths.from_config(load_config(config_path), project_root=tmp_path)
    generated = tmp_path / "wrangler.generated.toml"

    monkeypatch.setattr("yutome.cli._worker_deploy._tracked_worker_path", lambda: worker_project)
    monkeypatch.setattr("yutome.cli._worker_deploy._require_cloudflare_deploy_runtime", lambda: None)
    monkeypatch.setattr("yutome.cli._worker_deploy._ensure_worker_node_modules", lambda _worker_project: None)
    monkeypatch.setattr("yutome.cli._worker_deploy._ensure_wrangler_authenticated", lambda _worker_project: None)
    monkeypatch.setattr("yutome.cli._worker_deploy._ensure_oauth_kv_namespace", lambda _worker_project, _paths: generated)

    def fake_stream(command: list[str], *, cwd: Path) -> tuple[int, str]:
        assert command == ["npx", "--yes", "wrangler", "deploy", "--config", str(generated)]
        assert cwd == worker_project
        return (
            1,
            "\n".join(
                [
                    f"A request to the Cloudflare API (/accounts/{account_id}/workers/scripts/yutome-remote-mcp) failed.",
                    "You need to verify your email address to use Workers. [code: 10034]",
                ]
            ),
        )

    monkeypatch.setattr("yutome.cli._worker_deploy._run_command_streamed", fake_stream)

    with pytest.raises(typer.Exit) as exc:
        _deploy_tracked_worker(paths=paths, refresh_contract=False, relay_token="relay", pairing_code="pair")

    assert exc.value.exit_code == 1
    captured = capsys.readouterr()
    assert "hasn't verified its email address yet" in captured.err
    assert "verification email" in captured.err
    assert "rerun `yutome connect --deploy`" in captured.err
    assert "developers.cloudflare.com/fundamentals/setup/account/verify-email-address" in captured.err


def test_ensure_workers_dev_subdomain_creates_when_missing(
    monkeypatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:  # noqa: ANN001
    account_id = "1d653327475a6351fab5cbea055baf7c"
    worker_project = tmp_path / "worker_project"
    worker_project.mkdir()
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", account_id)

    calls: list[tuple[str, str, dict | None]] = []

    def fake_api(method: str, path: str, token: str, payload: dict | None = None, **_kwargs: object) -> tuple[int, dict]:
        assert token == "test-token"
        calls.append((method, path, payload))
        if method == "GET":
            return 404, {"success": False, "errors": [{"code": 10007, "message": "no subdomain"}]}
        if method == "PUT":
            return 200, {"success": True, "result": {"subdomain": payload["subdomain"]}}
        raise AssertionError(f"unexpected method {method}")

    monkeypatch.setattr("yutome.cli._worker_deploy._cloudflare_api_call", fake_api)

    _ensure_workers_dev_subdomain(worker_project)

    assert calls[0] == ("GET", f"/accounts/{account_id}/workers/subdomain", None)
    assert calls[1][0] == "PUT"
    assert calls[1][1] == f"/accounts/{account_id}/workers/subdomain"
    assert calls[1][2]["subdomain"].startswith("yutome-")
    captured = capsys.readouterr()
    assert "Created workers.dev subdomain" in captured.out


def test_ensure_workers_dev_subdomain_skips_when_already_exists(
    monkeypatch, tmp_path: Path
) -> None:  # noqa: ANN001
    worker_project = tmp_path / "worker_project"
    worker_project.mkdir()
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "1d653327475a6351fab5cbea055baf7c")

    methods: list[str] = []

    def fake_api(method: str, path: str, token: str, payload: dict | None = None, **_kwargs: object) -> tuple[int, dict]:
        methods.append(method)
        return 200, {"success": True, "result": {"subdomain": "existing-name"}}

    monkeypatch.setattr("yutome.cli._worker_deploy._cloudflare_api_call", fake_api)

    _ensure_workers_dev_subdomain(worker_project)

    assert methods == ["GET"]


def test_ensure_workers_dev_subdomain_silent_when_no_token(
    monkeypatch, tmp_path: Path
) -> None:  # noqa: ANN001
    worker_project = tmp_path / "worker_project"
    worker_project.mkdir()
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.setattr("yutome.cli._worker_deploy._read_wrangler_oauth_token", lambda: None)

    called = {"hit": False}

    def fake_api(*_args: object, **_kwargs: object) -> tuple[int, dict]:
        called["hit"] = True
        return 0, {}

    monkeypatch.setattr("yutome.cli._worker_deploy._cloudflare_api_call", fake_api)

    _ensure_workers_dev_subdomain(worker_project)

    assert called["hit"] is False


def test_ensure_workers_dev_subdomain_retries_on_name_taken(
    monkeypatch, tmp_path: Path
) -> None:  # noqa: ANN001
    worker_project = tmp_path / "worker_project"
    worker_project.mkdir()
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "1d653327475a6351fab5cbea055baf7c")

    put_attempts: list[str] = []

    def fake_api(method: str, path: str, token: str, payload: dict | None = None, **_kwargs: object) -> tuple[int, dict]:
        if method == "GET":
            return 404, {"success": False, "errors": [{"code": 10007}]}
        put_attempts.append(payload["subdomain"])
        if len(put_attempts) < 2:
            return 409, {"success": False, "errors": [{"message": "subdomain is already taken"}]}
        return 200, {"success": True, "result": {"subdomain": payload["subdomain"]}}

    monkeypatch.setattr("yutome.cli._worker_deploy._cloudflare_api_call", fake_api)

    _ensure_workers_dev_subdomain(worker_project)

    assert len(put_attempts) == 2
    assert put_attempts[0] != put_attempts[1]  # different names on retry


def test_bridge_start_detached_writes_pid_and_kills_prior(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    paths = ProjectPaths.from_config(load_config(config_path), project_root=tmp_path)

    stale_pid = 999_991
    pid_path = _bridge_pid_path(paths)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(stale_pid), encoding="utf-8")

    killed: list[int] = []
    monkeypatch.setattr("yutome.cli._bridge._pid_is_alive", lambda pid: pid == stale_pid)
    monkeypatch.setattr("yutome.cli._bridge._stop_bridge_pid", lambda pid, **_kw: killed.append(pid) or True)

    popen_calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, cmd: list[str], **_kwargs: object) -> None:
            popen_calls.append(cmd)
            self.pid = 424242

    monkeypatch.setattr("yutome.cli._bridge.subprocess.Popen", FakePopen)
    monkeypatch.setattr("yutome.cli._bridge._bridge_binary_args", lambda: ["/fake/yutome"])

    pid, log_path = _bridge_start_detached(config_path, paths)

    assert killed == [stale_pid]
    assert pid == 424242
    assert log_path == paths.logs_dir / "bridge.log"
    assert _read_bridge_pid(paths) == 424242
    assert popen_calls and popen_calls[0][:1] == ["/fake/yutome"]
    assert "bridge" in popen_calls[0] and "start" in popen_calls[0] and "--foreground" in popen_calls[0]


def test_stop_bridge_pid_sends_sigterm_then_returns_true(monkeypatch) -> None:  # noqa: ANN001
    killed_with: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        killed_with.append((pid, sig))

    alive = {"value": True}

    def fake_alive(_pid: int) -> bool:
        # First check after SIGTERM: still alive; second check: dead
        was = alive["value"]
        alive["value"] = False
        return was

    monkeypatch.setattr("yutome.cli._bridge.os.kill", fake_kill)
    monkeypatch.setattr("yutome.cli._bridge._pid_is_alive", fake_alive)
    monkeypatch.setattr("yutome.cli._bridge.time.sleep", lambda _s: None)
    monkeypatch.setattr("yutome.cli._bridge.time.time", lambda: 0.0)

    assert _stop_bridge_pid(12345, timeout=0.5) is True
    assert killed_with[0][0] == 12345  # SIGTERM was sent first


def test_launchd_plist_content_has_expected_keys(tmp_path: Path) -> None:
    plist = _launchd_plist_content(
        ["/usr/local/bin/yutome"],
        tmp_path / "yutome.toml",
        tmp_path,
        tmp_path / "bridge.log",
    )
    parsed = plistlib.loads(plist.encode("utf-8"))
    assert parsed["Label"] == "ai.yutome.bridge"
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] is True
    assert parsed["ProgramArguments"] == [
        "/usr/local/bin/yutome",
        "--config",
        str(tmp_path / "yutome.toml"),
        "serve",
        "bridge",
        "start",
        "--foreground",
    ]
    assert parsed["StandardOutPath"] == str(tmp_path / "bridge.log")


def test_systemd_unit_content_has_expected_directives(tmp_path: Path) -> None:
    unit = _systemd_unit_content(
        ["/usr/local/bin/yutome"],
        tmp_path / "yutome.toml",
        tmp_path,
        tmp_path / "bridge.log",
    )
    exec_start = next(line.removeprefix("ExecStart=") for line in unit.splitlines() if line.startswith("ExecStart="))
    assert shlex.split(exec_start) == [
        "/usr/local/bin/yutome",
        "--config",
        str(tmp_path / "yutome.toml"),
        "serve",
        "bridge",
        "start",
        "--foreground",
    ]
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit
    assert f'StandardOutput=append:"{tmp_path / "bridge.log"}"' in unit


def test_service_files_preserve_paths_with_spaces_and_xml_chars(tmp_path: Path) -> None:
    root = tmp_path / "Yutome & Project"
    config_path = root / "yutome.toml"
    log_path = root / "logs" / "bridge.log"
    binary = str(root / "bin" / "yutome")

    plist = _launchd_plist_content([binary], config_path, root, log_path)
    parsed = plistlib.loads(plist.encode("utf-8"))
    assert parsed["ProgramArguments"] == [
        binary,
        "--config",
        str(config_path),
        "serve",
        "bridge",
        "start",
        "--foreground",
    ]
    assert parsed["WorkingDirectory"] == str(root)

    unit = _systemd_unit_content([binary], config_path, root, log_path)
    exec_start = next(line.removeprefix("ExecStart=") for line in unit.splitlines() if line.startswith("ExecStart="))
    assert shlex.split(exec_start) == [
        binary,
        "--config",
        str(config_path),
        "serve",
        "bridge",
        "start",
        "--foreground",
    ]
    assert f'WorkingDirectory="{root}"' in unit


def test_bridge_start_detached_uses_bridge_command_config_order(
    monkeypatch, tmp_path: Path
) -> None:  # noqa: ANN001
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    paths = ProjectPaths.from_config(load_config(config_path), project_root=tmp_path)
    captured: dict[str, object] = {}

    class FakePopen:
        pid = 12345

        def __init__(self, command: list[str], **kwargs: object) -> None:
            captured["command"] = command
            captured["kwargs"] = kwargs

    monkeypatch.setattr("yutome.cli._bridge._bridge_binary_args", lambda: ["/usr/local/bin/yutome"])
    monkeypatch.setattr("yutome.cli._bridge._read_bridge_pid", lambda _paths: None)
    monkeypatch.setattr("yutome.cli._bridge.subprocess.Popen", FakePopen)

    pid, _log_path = _bridge_start_detached(config_path, paths)

    assert pid == 12345
    assert captured["command"] == [
        "/usr/local/bin/yutome",
        "--config",
        str(config_path),
        "serve",
        "bridge",
        "start",
        "--foreground",
    ]


def test_installed_bridge_config_path_reads_launchd_plist(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    config_path = tmp_path / "current project" / "yutome.toml"
    plist_path = tmp_path / "ai.yutome.bridge.plist"
    plist_path.write_text(
        _launchd_plist_content(["/usr/local/bin/yutome"], config_path, config_path.parent, tmp_path / "bridge.log"),
        encoding="utf-8",
    )
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._launchd_plist_path", lambda: plist_path)

    assert _installed_bridge_config_path() == config_path


def test_bridge_install_cli_accepts_config_option(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    service_path = tmp_path / "ai.yutome.bridge.plist"
    monkeypatch.setattr(
        "yutome.cli._bridge._install_bridge_service",
        lambda cfg: (cfg == config_path, service_path, None),
    )

    result = runner.invoke(app, ["--config", str(config_path), "serve", "bridge", "install"])

    assert result.exit_code == 0
    assert str(service_path) in result.output


def test_bridge_stop_stops_launchd_service_for_matching_config(
    monkeypatch, tmp_path: Path
) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    calls: list[str] = []
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._installed_service_matches_config", lambda _cfg: True)
    monkeypatch.setattr("yutome.cli._bridge._launchd_bridge_pid", lambda: None)
    monkeypatch.setattr("yutome.cli._bridge._read_bridge_pid", lambda _paths: None)

    def fake_stop() -> subprocess.CompletedProcess[str]:
        calls.append("stop")
        return subprocess.CompletedProcess(["launchctl", "unload"], 0, stdout="", stderr="")

    monkeypatch.setattr("yutome.cli._bridge._stop_launchd_bridge_service", fake_stop)

    result = runner.invoke(app, ["--config", str(config_path), "serve", "bridge", "stop"])

    assert result.exit_code == 0
    assert calls == ["stop"]
    assert "Stopped launchd bridge service" in result.output


def test_bridge_stop_does_not_stop_service_for_different_config(
    monkeypatch, tmp_path: Path
) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._installed_service_matches_config", lambda _cfg: False)
    monkeypatch.setattr("yutome.cli._bridge._installed_bridge_config_path", lambda: tmp_path / "other.toml")
    monkeypatch.setattr("yutome.cli._bridge._read_bridge_pid", lambda _paths: None)

    def fail_stop() -> subprocess.CompletedProcess[str]:
        raise AssertionError("must not stop another config's service")

    monkeypatch.setattr("yutome.cli._bridge._stop_launchd_bridge_service", fail_stop)

    result = runner.invoke(app, ["--config", str(config_path), "serve", "bridge", "stop"])

    assert result.exit_code == 0
    assert "another config" in result.output
    assert "No bridge PID recorded" in result.output


def test_restart_bridge_after_deploy_spawns_detached_when_no_service(
    monkeypatch, tmp_path: Path
) -> None:  # noqa: ANN001
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    paths = ProjectPaths.from_config(load_config(config_path), project_root=tmp_path)

    # Pretend a connector state exists with a relay token
    fake_state = type("S", (), {"relay_token": "rt"})()
    monkeypatch.setattr("yutome.cli._bridge.load_remote_state", lambda _paths: fake_state)
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)

    called = {"hit": False}

    def fake_start(config_path: Path, paths: ProjectPaths) -> tuple[int, Path]:
        called["hit"] = True
        return 4242, paths.logs_dir / "bridge.log"

    monkeypatch.setattr("yutome.cli._bridge._bridge_start_detached", fake_start)

    _restart_bridge_after_deploy(config_path=config_path, paths=paths)

    assert called["hit"] is True


def test_restart_bridge_after_deploy_kicks_launchd_when_installed(
    monkeypatch, tmp_path: Path
) -> None:  # noqa: ANN001
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    paths = ProjectPaths.from_config(load_config(config_path), project_root=tmp_path)

    fake_state = type("S", (), {"relay_token": "rt"})()
    monkeypatch.setattr("yutome.cli._bridge.load_remote_state", lambda _paths: fake_state)
    monkeypatch.setattr("yutome.cli._bridge._launchd_installed", lambda: True)
    monkeypatch.setattr("yutome.cli._bridge._systemd_installed", lambda: False)
    monkeypatch.setattr("yutome.cli._bridge._installed_service_matches_config", lambda _cfg: True)
    spawn_called = {"hit": False}
    monkeypatch.setattr(
        "yutome.cli._bridge._bridge_start_detached",
        lambda *_a, **_k: (spawn_called.update(hit=True) or (0, Path("/dev/null"))),
    )

    subprocess_calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        subprocess_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("yutome.cli._bridge.subprocess.run", fake_run)

    _restart_bridge_after_deploy(config_path=config_path, paths=paths)

    assert spawn_called["hit"] is False
    assert any("launchctl" in cmd[0] and "kickstart" in cmd for cmd in subprocess_calls)


def test_finalize_remote_bridge_setup_starts_then_prompts_for_persistence(
    monkeypatch, tmp_path: Path
) -> None:  # noqa: ANN001
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    paths = ProjectPaths.from_config(load_config(config_path), project_root=tmp_path)
    fake_state = type("S", (), {"relay_token": "rt"})()
    events: list[str] = []

    monkeypatch.setattr("yutome.cli._bridge.load_remote_state", lambda _paths: fake_state)
    monkeypatch.setattr(
        "yutome.cli._bridge._restart_bridge_after_deploy",
        lambda **_kwargs: events.append("restart"),
    )
    monkeypatch.setattr(
        "yutome.cli._bridge._offer_bridge_persistence",
        lambda _config_path: events.append("offer"),
    )

    _finalize_remote_bridge_setup(
        config_path=config_path,
        paths=paths,
        before_persistence=lambda: events.append("callback"),
    )

    assert events == ["restart", "callback", "offer"]


def test_finalize_remote_bridge_setup_noops_without_relay_token(
    monkeypatch, tmp_path: Path
) -> None:  # noqa: ANN001
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    paths = ProjectPaths.from_config(load_config(config_path), project_root=tmp_path)
    fake_state = type("S", (), {"relay_token": None})()

    monkeypatch.setattr("yutome.cli._bridge.load_remote_state", lambda _paths: fake_state)
    monkeypatch.setattr(
        "yutome.cli._bridge._restart_bridge_after_deploy",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not restart")),
    )
    monkeypatch.setattr(
        "yutome.cli._bridge._offer_bridge_persistence",
        lambda _config_path: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )

    _finalize_remote_bridge_setup(config_path=config_path, paths=paths)


def test_wrangler_auth_skips_login_when_api_token_present(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    worker_project = tmp_path / "worker_project"
    worker_project.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "token")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    _ensure_wrangler_authenticated(worker_project)

    assert calls == []


def test_wrangler_auth_runs_interactive_login_when_whoami_fails(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    worker_project = tmp_path / "worker_project"
    worker_project.mkdir()
    calls: list[list[str]] = []
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.setattr("yutome.setup_prompts.is_interactive", lambda: True)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[-1] == "whoami" and calls.count(command) == 1:
            return subprocess.CompletedProcess(command, 1, stdout="not logged in", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="logged in", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    _ensure_wrangler_authenticated(worker_project)

    assert calls == [
        ["npx", "--yes", "wrangler", "whoami"],
        ["npx", "--yes", "wrangler", "login"],
        ["npx", "--yes", "wrangler", "whoami"],
    ]


def test_wrangler_auth_noninteractive_requires_api_token(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    worker_project = tmp_path / "worker_project"
    worker_project.mkdir()
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.setattr("yutome.setup_prompts.is_interactive", lambda: False)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == ["npx", "--yes", "wrangler", "whoami"]
        return subprocess.CompletedProcess(command, 1, stdout="needs token", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    with pytest.raises(typer.Exit):
        _ensure_wrangler_authenticated(worker_project)


def test_wrangler_whoami_zero_exit_can_still_be_unauthenticated() -> None:
    completed = subprocess.CompletedProcess(
        ["npx", "--yes", "wrangler", "whoami"],
        0,
        stdout="You are not authenticated. Please run `wrangler login`.\n",
        stderr="",
    )

    assert _wrangler_whoami_authenticated(completed) is False


def test_push_wrangler_secret_sends_newline_terminated_value(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    worker_project = tmp_path / "worker_project"
    worker_project.mkdir()
    wrangler_config = tmp_path / "wrangler.generated.toml"
    calls: list[dict[str, object]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    _push_wrangler_secret(worker_project, "YUTOME_RELAY_TOKEN", "relay-secret", wrangler_config=wrangler_config)

    assert len(calls) == 1
    assert calls[0]["command"] == [
        "npx",
        "--yes",
        "wrangler",
        "secret",
        "put",
        "YUTOME_RELAY_TOKEN",
        "--config",
        str(wrangler_config),
    ]
    assert calls[0]["cwd"] == worker_project
    assert calls[0]["input"] == "relay-secret\n"


def test_disconnect_dry_run_reports_worker_without_removing_state(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    connect_result = runner.invoke(
        app,
        ["--config", str(config_path), "connect", "--endpoint", "https://example.workers.dev", "--worker-name", "worker-to-delete"],
    )
    assert connect_result.exit_code == 0

    result = runner.invoke(app, ["--config", str(config_path), "disconnect", "--dry-run"])

    assert result.exit_code == 0
    assert "Cloudflare Worker: worker-to-delete" in result.output
    assert "Dry run only" in result.output
    assert (tmp_path / "data/remote/connection.json").exists()


def test_disconnect_can_remove_local_state_after_cloud_delete(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    deleted: list[str] = []

    monkeypatch.setattr("yutome.cli._worker_deploy._delete_tracked_worker", lambda name: deleted.append(name))
    connect_result = runner.invoke(
        app,
        ["--config", str(config_path), "connect", "--endpoint", "https://example.workers.dev", "--worker-name", "worker-to-delete"],
    )
    assert connect_result.exit_code == 0

    result = runner.invoke(app, ["--config", str(config_path), "disconnect", "--yes"])

    assert result.exit_code == 0
    assert deleted == ["worker-to-delete"]
    assert not (tmp_path / "data/remote/connection.json").exists()


def test_remote_disconnect_alias_uses_disconnect_language(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    monkeypatch.setattr("yutome.cli._worker_deploy._delete_tracked_worker", lambda _name: None)
    connect_result = runner.invoke(
        app,
        ["--config", str(config_path), "connect", "--endpoint", "https://example.workers.dev", "--worker-name", "worker-to-delete"],
    )
    assert connect_result.exit_code == 0

    result = runner.invoke(app, ["--config", str(config_path), "disconnect", "--yes"])

    assert result.exit_code == 0
    assert "Disconnect complete" in result.output


def test_setup_yes_skips_remote_prompt_and_points_to_connect(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    result = runner.invoke(app, ["--config", str(config_path), "setup", "--yes"])

    assert result.exit_code == 0
    assert "Use Yutome from your AI assistant:" in result.output
    assert "Claude Desktop" in result.output
    assert "Cloudflare" in result.output
    assert "Yutome Desktop offline" in result.output
    assert "Optional next step: yutome connect --app claude" in result.output
    assert "How do you want to connect Yutome to your assistant?" not in result.output
    assert not (tmp_path / "data/remote/connection.json").exists()


def test_setup_can_prepare_remote_mcp_worker_project_interactively(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    # Skip every step, pick "Web + mobile — deploy" at the connect select, then
    # Claude as the assistant app, then decline the deploy/dashboard confirm.
    # 1=webshare-no, 2=gemini-no, 3=voyage-no, 4=subs-skip,
    # 5=public-subs-no, 6=add-channel-no, 7=connect-deploy,
    # 8=assistant-claude, 9=continue-deploy-no
    result = runner.invoke(
        app,
        ["--config", str(config_path), "setup"],
        input="n\nn\nn\n3\nn\nn\n2\n1\nn\n",
    )

    assert result.exit_code == 0
    assert "Use Yutome from your AI assistant:" in result.output
    assert "Cloudflare" in result.output
    assert "Which assistant app do you want connector instructions for?" in result.output
    assert "Claude (web, Desktop, mobile)" in result.output
    assert not (tmp_path / "data/remote/cloudflare-worker").exists()
    assert not (tmp_path / "data/remote/connection.json").exists()


def test_setup_deploy_path_finalizes_bridge_after_saving_state(
    monkeypatch, tmp_path: Path
) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    finalized: list[Path] = []

    monkeypatch.setattr("yutome.cli._worker_deploy._can_run_cloudflare_deploy", lambda: True)
    monkeypatch.setattr(
        "yutome.cli._worker_deploy._deploy_tracked_worker",
        lambda paths, refresh_contract=True, relay_token=None, pairing_code=None: (
            "https://example.workers.dev",
            "yutome-remote-mcp",
            "fake-relay-token",
            "ABCD12",
        ),
    )

    def fake_finalize(*, config_path: Path, paths: ProjectPaths, before_persistence=None) -> None:  # noqa: ANN001
        finalized.append(config_path)
        if before_persistence is not None:
            before_persistence()

    monkeypatch.setattr("yutome.cli._bridge._finalize_remote_bridge_setup", fake_finalize)

    result = runner.invoke(
        app,
        ["--config", str(config_path), "setup"],
        input="n\nn\nn\n3\nn\nn\n2\n1\ny\n",
    )

    assert result.exit_code == 0
    assert finalized == [config_path]
    assert "ABCD12" in result.output


def test_setup_pasted_endpoint_with_relay_token_finalizes_bridge(
    monkeypatch, tmp_path: Path
) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    finalized: list[Path] = []

    def fake_finalize(*, config_path: Path, paths: ProjectPaths, before_persistence=None) -> None:  # noqa: ANN001
        finalized.append(config_path)
        if before_persistence is not None:
            before_persistence()

    monkeypatch.setattr("yutome.cli._bridge._finalize_remote_bridge_setup", fake_finalize)

    result = runner.invoke(
        app,
        ["--config", str(config_path), "setup"],
        input=(
            "n\nn\nn\n3\nn\nn\n3\n1\n"
            "https://example.workers.dev\nrelay-secret\nPAIR12\n"
        ),
    )

    assert result.exit_code == 0
    assert finalized == [config_path]
    state = json.loads((tmp_path / "data/remote/connection.json").read_text(encoding="utf-8"))
    assert state["relay_token"] == "relay-secret"
    assert state["pairing_code"] == "PAIR12"


def test_show_context_accepts_id_option(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    db_path = tmp_path / "data/indexes/catalog.sqlite"
    bootstrap_catalog(db_path)

    with connect_catalog(db_path) as connection:
        connection.execute("INSERT INTO channels(channel_id, title) VALUES ('UCcli', 'CLI Test')")
        connection.execute(
            """
            INSERT INTO videos(video_id, channel_id, title, ingest_status)
            VALUES ('vidcli', 'UCcli', 'CLI context test', 'indexed')
            """
        )
        connection.execute(
            """
            INSERT INTO transcript_versions(
                transcript_version_id, video_id, source, raw_path, normalized_path, text_hash, segment_count, active
            ) VALUES ('tvcli', 'vidcli', 'captions', 'raw.json', 'normalized.jsonl', 'hash', 1, 1)
            """
        )
        connection.execute(
            """
            INSERT INTO chunks(
                chunk_id, transcript_version_id, video_id, channel_id, sequence, start_ms, end_ms,
                text, token_count, text_hash, chunker_version
            ) VALUES ('chunk-cli', 'tvcli', 'vidcli', 'UCcli', 0, 0, 1000, 'agent-friendly context', 4, 'chunkhash', 'v1')
            """
        )
        connection.commit()

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "search",
            "show",
            "context",
            "chunk-cli",
            "--token-budget",
            "1000",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["anchor"]["chunk_id"] == "chunk-cli"
    assert "agent-friendly context" in payload["text"]


def test_remote_sync_dry_run_reports_manifest_and_excludes_secrets(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    db_path = tmp_path / "data/indexes/catalog.sqlite"
    bootstrap_catalog(db_path)
    with connect_catalog(db_path) as connection:
        connection.execute(
            "INSERT INTO channels(channel_id, title) VALUES ('UCyes', 'Yes Theory')"
        )
        connection.execute(
            "INSERT INTO videos(video_id, channel_id, title, ingest_status) VALUES ('yes1', 'UCyes', 'Yes video', 'indexed')"
        )
        connection.execute(
            """
            INSERT INTO transcript_versions(
                transcript_version_id, video_id, source, raw_path, normalized_path, text_hash, segment_count, active
            ) VALUES ('tv1', 'yes1', 'captions', 'raw.json', 'normalized.jsonl', 'hash', 1, 1)
            """
        )
        connection.execute(
            """
            INSERT INTO chunks(
                chunk_id, transcript_version_id, video_id, channel_id, sequence, start_ms, end_ms, text, text_hash, chunker_version
            ) VALUES ('c1', 'tv1', 'yes1', 'UCyes', 0, 0, 1000, 'hello world', 'chunkhash', 'v1')
            """
        )
        connection.execute(
            """
            INSERT INTO embeddings(chunk_id, provider, model, dimension, artifact_status, index_status)
            VALUES ('c1', 'voyage', 'voyage-4-lite', 1024, 'embedded', 'indexed')
            """
        )
        connection.commit()

    result = runner.invoke(app, ["--config", str(config_path), "serve", "remote", "sync", "--dry-run", "--json"])

    assert result.exit_code == 0
    manifest = json.loads(result.output)
    assert manifest["upload_performed"] is False
    assert manifest["would_sync"]["channels"] == 1
    assert manifest["would_sync"]["videos"] == 1
    assert manifest["would_sync"]["chunks"] == 1
    rendered = json.dumps(manifest)
    assert "YUTOME_WEBSHARE_PASSWORD" not in rendered
    assert "GEMINI_API_KEY" not in rendered
    assert "VOYAGE_API_KEY" not in rendered


def test_catalog_video_listing_can_filter_by_channel_selector(tmp_path: Path) -> None:
    db_path = tmp_path / "data/indexes/catalog.sqlite"
    bootstrap_catalog(db_path)
    with connect_catalog(db_path) as connection:
        connection.execute(
            "INSERT INTO channels(channel_id, handle, title, source_url) VALUES ('UCyes', '@YesTheory', 'Yes Theory', 'https://www.youtube.com/@YesTheory')"
        )
        connection.execute(
            "INSERT INTO channels(channel_id, handle, title, source_url) VALUES ('UCleo', '@LeoandLongevity', 'Leo', 'https://www.youtube.com/@LeoandLongevity')"
        )
        connection.execute(
            "INSERT INTO videos(video_id, channel_id, title, ingest_status) VALUES ('yes1', 'UCyes', 'Yes video', 'discovered')"
        )
        connection.execute(
            "INSERT INTO videos(video_id, channel_id, title, ingest_status) VALUES ('leo1', 'UCleo', 'Leo video', 'discovered')"
        )
        connection.commit()

        videos = list_catalog_videos(connection, channel_selector="https://www.youtube.com/@YesTheory")

    assert [video.video_id for video in videos] == ["yes1"]


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


def test_proxy_402_is_classified_as_proxy_payment_required() -> None:
    error_class, retryable = classify_transcript_error(
        "ProxyError: Tunnel connection failed: 402 Payment Required"
    )

    assert is_proxy_payment_error("CONNECT tunnel failed, response 402")
    assert error_class == "proxy_payment_required"
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


def test_ytdlp_proxy_402_failure_gets_actionable_message(tmp_path: Path) -> None:
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    proxy = load_config(config_path).proxy.model_copy(
        update={
            "enabled": True,
            "kind": "webshare",
            "webshare_username": "proxy-user",
            "webshare_password": "proxy-pass",
        }
    )
    completed = subprocess.CompletedProcess(
        ["yt-dlp"],
        -6,
        stdout="",
        stderr="CONNECT tunnel failed, response 402",
    )

    message = format_ytdlp_failure(
        completed,
        operation="metadata fetch",
        proxy=proxy,
        proxy_key="video123",
    )

    assert "Webshare proxy returned 402 Payment Required" in message
    assert "yt-dlp also aborted with SIGABRT" in message
    assert "proxy-user" not in message
    assert "proxy-pass" not in message


def test_ytdlp_sigabrt_with_proxy_gets_diagnostic_message(tmp_path: Path) -> None:
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    proxy = load_config(config_path).proxy.model_copy(
        update={
            "enabled": True,
            "kind": "webshare",
            "webshare_username": "proxy-user",
            "webshare_password": "proxy-pass",
        }
    )
    completed = subprocess.CompletedProcess(["yt-dlp"], -6, stdout="", stderr="")

    message = format_ytdlp_failure(
        completed,
        operation="metadata fetch",
        proxy=proxy,
        proxy_key="video123",
    )

    assert "yt-dlp subprocess aborted with SIGABRT" in message
    assert "run `yutome proxy-test`" in message


def test_generic_proxy_pool_is_selected_deterministically(tmp_path: Path) -> None:
    config_path = tmp_path / "yutome.toml"
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
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    monkeypatch.setenv("YUTOME_WEBSHARE_USERNAME", "proxy-user")
    monkeypatch.setenv("YUTOME_WEBSHARE_PASSWORD", "proxy-pass")
    monkeypatch.setenv("YUTOME_WEBSHARE_DOMAIN", "p.webshare.io")
    monkeypatch.setenv("YUTOME_WEBSHARE_PORT", "80")
    monkeypatch.setenv("YUTOME_PROXY_USE_FOR_DISCOVERY", "true")
    monkeypatch.setenv("YUTOME_PROXY_USE_FOR_METADATA", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("YUTOME_GEMINI_MODEL", "gemini-test-model")
    monkeypatch.setenv("YUTOME_GEMINI_MEDIA_RESOLUTION", "medium")
    monkeypatch.setenv("YUTOME_GEMINI_REQUEST_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("YUTOME_GEMINI_WINDOW_SECONDS", "600")
    monkeypatch.setenv("YUTOME_GEMINI_FALLBACK_ENABLED", "yes")
    monkeypatch.setenv("YUTOME_TRANSCRIPTS_PREFER_YTDLP_SUBTITLES", "true")
    monkeypatch.setenv("YUTOME_TRANSCRIPTS_REQUEST_TIMEOUT_SECONDS", "12.5")

    config = apply_env_to_config(load_config(config_path))

    assert config.proxy.enabled is True
    assert config.proxy.kind == "webshare"
    assert config.proxy.use_for_discovery is True
    assert config.proxy.use_for_metadata is True
    assert proxy_url_for_ytdlp(config.proxy, key="video123") == "http://proxy-user-rotate:proxy-pass@p.webshare.io:80/"
    assert config.gemini.enabled is True
    assert config.gemini.model == "gemini-test-model"
    assert config.gemini.media_resolution == "medium"
    assert config.gemini.request_timeout_seconds == 45.0
    assert config.gemini.window_seconds == 600
    assert config.gemini.fallback_enabled is True
    assert config.transcripts.prefer_ytdlp_subtitles is True
    assert config.transcripts.request_timeout_seconds == 12.5


def test_webshare_credentials_auto_enable_local_metadata_proxy(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    monkeypatch.setenv("YUTOME_WEBSHARE_USERNAME", "proxy-user")
    monkeypatch.setenv("YUTOME_WEBSHARE_PASSWORD", "proxy-pass")

    config = apply_env_to_config(load_config(config_path))

    assert config.proxy.enabled is True
    assert config.proxy.kind == "webshare"
    assert config.proxy.use_for_metadata is True
    assert config.proxy.use_for_discovery is False


def test_webshare_metadata_auto_enable_can_be_disabled(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    monkeypatch.setenv("YUTOME_WEBSHARE_USERNAME", "proxy-user")
    monkeypatch.setenv("YUTOME_WEBSHARE_PASSWORD", "proxy-pass")
    monkeypatch.setenv("YUTOME_PROXY_USE_FOR_METADATA", "false")

    config = apply_env_to_config(load_config(config_path))

    assert config.proxy.enabled is True
    assert config.proxy.kind == "webshare"
    assert config.proxy.use_for_metadata is False


def test_env_can_override_ytdlp_runtime_profile(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)
    monkeypatch.setenv("YUTOME_YT_DLP_PROFILE", "current")
    monkeypatch.setenv("YUTOME_YT_DLP_FALLBACK_PROFILE", "python-no-js")
    monkeypatch.setenv("YUTOME_YT_DLP_PROFILE_FALLBACK_ENABLED", "false")
    monkeypatch.setenv("YUTOME_YT_DLP_RETRIES_WHEN_BLOCKED", "5")

    config = apply_env_to_config(load_config(config_path))

    assert config.yt_dlp.profile == "current"
    assert config.yt_dlp.fallback_profile == "python-no-js"
    assert config.yt_dlp.profile_fallback_enabled is False
    assert config.yt_dlp.retries_when_blocked == 5


def test_oauth_client_secrets_loader_accepts_installed_shape(tmp_path: Path) -> None:
    secrets_path = tmp_path / "client_secret.json"
    secrets_path.write_text(
        '{"installed":{"client_id":"client-123","client_secret":"secret-abc"}}',
        encoding="utf-8",
    )

    client = load_oauth_client(secrets_path)

    assert client.client_id == "client-123"
    assert client.client_secret == "secret-abc"


def test_oauth_authorization_url_uses_readonly_scope_and_pkce() -> None:
    url = _authorization_url(
        client=OAuthClient(client_id="client-123"),
        redirect_uri="http://127.0.0.1:8765/",
        state="state-abc",
        challenge="challenge-xyz",
    )

    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

    assert params["client_id"] == ["client-123"]
    assert params["scope"] == ["https://www.googleapis.com/auth/youtube.readonly"]
    assert params["code_challenge"] == ["challenge-xyz"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["access_type"] == ["offline"]


def test_oauth_token_validity_requires_unexpired_access_token() -> None:
    assert _token_is_valid({"access_token": "token", "expires_at": time.time() + 3600})
    assert not _token_is_valid({"access_token": "token", "expires_at": time.time() - 1})
    assert not _token_is_valid({"expires_at": time.time() + 3600})
