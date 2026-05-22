import json
import subprocess
import time
import urllib.parse
from pathlib import Path

from typer.testing import CliRunner

from yutome.cli import (
    _active_oauth_kv_id,
    _ensure_oauth_kv_namespace,
    _push_wrangler_secret,
    _strip_oauth_kv_binding,
    _write_generated_wrangler_config,
    app,
    _parse_channel_selection,
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

    add_result = runner.invoke(app, ["add", "@LeoandLongevity", "--config", str(config_path)])

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

    add_result = runner.invoke(app, ["add", "https://www.youtube.com/watch?v=UTuuTTnjxMQ", "--config", str(config_path)])

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

    monkeypatch.setattr("yutome.cli._run_sync_targets", fake_run_sync_targets)

    result = runner.invoke(app, ["sync", "https://youtu.be/UTuuTTnjxMQ", "--config", str(config_path)])

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

    result = runner.invoke(app, ["sync", "--help"])

    assert result.exit_code == 0
    assert "--staged-fallback" not in result.output
    assert "--yt-dlp-first" not in result.output
    assert "--no-yt-dlp-fallback" not in result.output
    assert "--defer-metadata" not in result.output


def test_init_command_creates_config_dirs_and_catalog(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    result = runner.invoke(app, ["init", "--config", str(config_path)])

    assert result.exit_code == 0
    assert config_path.exists()
    assert (tmp_path / "data/artifacts/channels").is_dir()
    assert (tmp_path / "data/artifacts/videos").is_dir()
    assert (tmp_path / "data/indexes/lancedb").is_dir()
    assert catalog_is_initialized(tmp_path / "data/indexes/catalog.sqlite")


def test_setup_command_creates_first_run_files_without_prompting(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    result = runner.invoke(app, ["setup", "--config", str(config_path), "--yes"])

    assert result.exit_code == 0
    assert config_path.exists()
    assert (tmp_path / ".env").exists()
    assert catalog_is_initialized(tmp_path / "data/indexes/catalog.sqlite")
    assert "Next steps:" in result.output
    assert "Optional semantic search" in result.output
    assert "Use Yutome from Claude/ChatGPT:" in result.output
    assert "Remote MCP means Claude/ChatGPT call one public Yutome connector URL" in result.output
    assert "this computer and `yutome remote bridge` must be on" in result.output
    assert "+ > More" in result.output
    assert "does not require Voyage, Webshare, Gemini, or proxy credentials" in result.output
    assert "Optional next step: yutome connect --app claude" in result.output


def test_setup_command_can_add_initial_channel(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    result = runner.invoke(app, ["setup", "@YesTheory", "--config", str(config_path), "--yes"])

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
        ["setup", "@YesTheory", "--config", str(config_path)],
        input="y\nproxy-user\nproxy-pass\n\n\nn\nn\nn\nn\nn\nn\n",
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
        ["setup", "--config", str(config_path)],
        input="n\nn\ny\nvoyage-test-key\nn\nn\nn\nn\n",
    )

    assert result.exit_code == 0
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    config = load_config(config_path)
    assert "VOYAGE_API_KEY=voyage-test-key" in env_text
    assert "voyage-test-key" not in result.output
    assert config.embeddings.enabled is True
    assert 'yutome find "topic I remember" --mode hybrid' in result.output


def test_setup_imports_subscriptions_then_selects_channels(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    def fake_fetch(**kwargs):  # noqa: ANN003
        return [
            channel_from_input("UC1111111111111111111111", title="Alpha", import_source="youtube-browser-cookies"),
            channel_from_input("UC2222222222222222222222", title="Beta", import_source="youtube-browser-cookies"),
            channel_from_input("UC3333333333333333333333", title="Gamma", import_source="youtube-browser-cookies"),
        ]

    monkeypatch.setattr("yutome.cli.fetch_user_subscription_channels_from_browser", fake_fetch)

    result = runner.invoke(
        app,
        ["setup", "--config", str(config_path)],
        input="n\nn\nn\ny\nn\nn\n1,3\nn\nn\n",
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

    monkeypatch.setattr("yutome.cli.fetch_user_subscription_channels_from_browser", fake_fetch)
    monkeypatch.setattr("yutome.cli._run_sync_targets", fake_run_sync_targets)

    result = runner.invoke(
        app,
        ["setup", "--config", str(config_path)],
        input="n\nn\nn\ny\nn\nn\nall\ny\n1-2\nn\n",
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

    monkeypatch.setattr("yutome.cli.fetch_user_subscription_channels_from_browser", fake_fetch)

    result = runner.invoke(app, ["import-youtube", "--config", str(config_path)])

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

    monkeypatch.setattr("yutome.cli.fetch_public_subscription_channels_from_api", fake_api)

    result = runner.invoke(app, ["import-youtube", "@source", "--config", str(config_path)])

    assert result.exit_code == 0
    assert calls == [("@source", "test-api-key")]
    assert "youtube-public-api" in result.output


def test_remote_prepare_generates_http_token_without_printing_by_default(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    result = runner.invoke(app, ["remote", "prepare", "--config", str(config_path)])

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

    result = runner.invoke(app, ["connect", "--config", str(config_path), "--endpoint", "https://example.workers.dev"])

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


def test_connect_with_endpoint_and_tokens_writes_usable_remote_state(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    result = runner.invoke(
        app,
        [
            "connect",
            "--config",
            str(config_path),
            "--endpoint",
            "https://example.workers.dev",
            "--relay-token",
            "relay-secret",
            "--pairing-code",
            "PAIR12",
        ],
    )

    assert result.exit_code == 0
    state = json.loads((tmp_path / "data/remote/connection.json").read_text(encoding="utf-8"))
    assert state["relay_token"] == "relay-secret"
    assert state["pairing_code"] == "PAIR12"
    assert "Code: PAIR12" in result.output
    assert "yutome remote bridge" in result.output


def test_connect_without_endpoint_prints_deploy_instructions_without_provider_keys(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    result = runner.invoke(app, ["connect", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "yutome connect --deploy" in result.output
    assert "basic laptop-backed connector is designed for Cloudflare's free Workers plan" in result.output
    assert "Always-on/offline search is a later mode and may require enabling Cloudflare billing" in result.output
    assert "Tracked TypeScript Worker subproject lives at:" in result.output
    # The new connect flow does NOT generate files under data/remote — the
    # tracked TypeScript project at cloudflare/yutome-capsule/ is the source.
    assert not (tmp_path / "data/remote/cloudflare-worker").exists()
    assert not (tmp_path / "data/remote/connection.json").exists()
    assert "VOYAGE_API_KEY" not in result.output
    assert "YUTOME_WEBSHARE" not in result.output


def test_connect_deploy_invokes_tracked_capsule(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    monkeypatch.setattr(
        "yutome.cli._deploy_tracked_capsule",
        lambda paths, refresh_contract=True, relay_token=None, pairing_code=None: (
            "https://example.workers.dev",
            "yutome-remote-mcp",
            "fake-relay-token",
            "ABCD12",
        ),
    )

    result = runner.invoke(app, ["connect", "--config", str(config_path), "--deploy"])

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


def test_status_reports_remote_unconfigured_and_configured(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    write_default_config(config_path)

    missing = runner.invoke(app, ["status", "--config", str(config_path)])
    assert missing.exit_code == 0
    assert "Remote connector:" in missing.output
    assert "not configured" in missing.output

    connect_result = runner.invoke(app, ["connect", "--config", str(config_path), "--endpoint", "https://example.workers.dev/mcp"])
    assert connect_result.exit_code == 0
    configured = runner.invoke(app, ["status", "--config", str(config_path)])
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

    missing = runner.invoke(app, ["remote", "status", "--config", str(config_path)])
    assert missing.exit_code == 0
    assert "Remote connector is not configured" in missing.output

    connect_result = runner.invoke(app, ["connect", "--config", str(config_path), "--endpoint", "https://example.workers.dev"])
    assert connect_result.exit_code == 0
    configured = runner.invoke(app, ["remote", "status", "--config", str(config_path), "--json"])
    assert configured.exit_code == 0
    payload = json.loads(configured.output)
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
    connect_result = runner.invoke(
        app,
        [
            "connect",
            "--config",
            str(config_path),
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
                getattr(request, "full_url"),
                request.get_header("Authorization"),  # type: ignore[attr-defined]
                timeout,
            )
        )
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = runner.invoke(app, ["remote", "status", "--config", str(config_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert seen == [("https://example.workers.dev/relay/status", "Bearer relay-secret", 2.0)]
    assert payload["desktop_connection"] == "online (live)"
    assert payload["desktop_connection_source"] == "worker"
    assert payload["last_worker_seen_at"] == "2026-05-22T16:30:00+00:00"
    assert payload["relay_status_error"] is None


def test_generated_wrangler_config_keeps_oauth_kv_account_local(tmp_path: Path) -> None:
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    (capsule / "wrangler.toml").write_text(
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

    stripped = _strip_oauth_kv_binding((capsule / "wrangler.toml").read_text(encoding="utf-8"))
    assert 'binding = "OAUTH_KV"' not in stripped

    generated = _write_generated_wrangler_config(capsule, paths, "22222222222222222222222222222222")
    assert generated == tmp_path / "data/remote/cloudflare/wrangler.generated.toml"
    generated_text = generated.read_text(encoding="utf-8")
    assert _active_oauth_kv_id(generated_text) == "22222222222222222222222222222222"
    assert f'main = "{capsule.resolve()}/src/index.ts"' in generated_text
    assert _active_oauth_kv_id((capsule / "wrangler.toml").read_text(encoding="utf-8")) == "11111111111111111111111111111111"


def test_connect_deploy_reuses_existing_oauth_kv_namespace(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    (capsule / "wrangler.toml").write_text(
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
        assert kwargs["cwd"] == capsule
        if command[-1] == "list":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps([{"id": "33333333333333333333333333333333", "title": "OAUTH_KV"}]),
                stderr="",
            )
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr("subprocess.run", fake_run)

    generated = _ensure_oauth_kv_namespace(capsule, paths)

    assert commands == [["npx", "--yes", "wrangler", "kv", "namespace", "list"]]
    assert _active_oauth_kv_id(generated.read_text(encoding="utf-8")) == "33333333333333333333333333333333"


def test_push_wrangler_secret_sends_newline_terminated_value(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    wrangler_config = tmp_path / "wrangler.generated.toml"
    calls: list[dict[str, object]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    _push_wrangler_secret(capsule, "YUTOME_RELAY_TOKEN", "relay-secret", wrangler_config=wrangler_config)

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
    assert calls[0]["cwd"] == capsule
    assert calls[0]["input"] == "relay-secret\n"


def test_disconnect_dry_run_reports_worker_without_removing_state(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    connect_result = runner.invoke(
        app,
        ["connect", "--config", str(config_path), "--endpoint", "https://example.workers.dev", "--worker-name", "worker-to-delete"],
    )
    assert connect_result.exit_code == 0

    result = runner.invoke(app, ["disconnect", "--config", str(config_path), "--dry-run"])

    assert result.exit_code == 0
    assert "Cloudflare Worker: worker-to-delete" in result.output
    assert "Dry run only" in result.output
    assert (tmp_path / "data/remote/connection.json").exists()


def test_disconnect_can_remove_local_state_after_cloud_delete(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"
    deleted: list[str] = []

    monkeypatch.setattr("yutome.cli._delete_tracked_capsule", lambda name: deleted.append(name))
    connect_result = runner.invoke(
        app,
        ["connect", "--config", str(config_path), "--endpoint", "https://example.workers.dev", "--worker-name", "worker-to-delete"],
    )
    assert connect_result.exit_code == 0

    result = runner.invoke(app, ["disconnect", "--config", str(config_path), "--yes"])

    assert result.exit_code == 0
    assert deleted == ["worker-to-delete"]
    assert not (tmp_path / "data/remote/connection.json").exists()


def test_remote_disconnect_alias_uses_disconnect_language(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    monkeypatch.setattr("yutome.cli._delete_tracked_capsule", lambda _name: None)
    connect_result = runner.invoke(
        app,
        ["connect", "--config", str(config_path), "--endpoint", "https://example.workers.dev", "--worker-name", "worker-to-delete"],
    )
    assert connect_result.exit_code == 0

    result = runner.invoke(app, ["remote", "disconnect", "--config", str(config_path), "--yes"])

    assert result.exit_code == 0
    assert "Disconnect complete" in result.output


def test_setup_yes_skips_remote_prompt_and_points_to_connect(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    result = runner.invoke(app, ["setup", "--config", str(config_path), "--yes"])

    assert result.exit_code == 0
    assert "Use Yutome from Claude/ChatGPT:" in result.output
    assert "ask your normal assistant about your YouTube library" in result.output
    assert "Remote MCP means Claude/ChatGPT call one public Yutome connector URL" in result.output
    assert "+ > More" in result.output
    assert "this computer and `yutome remote bridge` must be on" in result.output
    assert "does not require Voyage, Webshare, Gemini, or proxy credentials" in result.output
    assert "basic laptop-backed connector is designed for Cloudflare's free Workers plan" in result.output
    assert "Always-on/offline search is a later mode and may require enabling Cloudflare billing" in result.output
    assert "Optional next step: yutome connect --app claude" in result.output
    assert "Connect Yutome to Claude/ChatGPT now?" not in result.output
    assert not (tmp_path / "data/remote/connection.json").exists()


def test_setup_can_prepare_remote_mcp_worker_project_interactively(tmp_path: Path) -> None:
    runner = CliRunner()
    config_path = tmp_path / "yutome.toml"

    result = runner.invoke(
        app,
        ["setup", "--config", str(config_path)],
        input="n\nn\nn\nn\nn\nn\ny\nclaude\n\nn\n",
    )

    assert result.exit_code == 0
    assert "Use Yutome from Claude/ChatGPT:" in result.output
    assert "basic laptop-backed connector is designed for Cloudflare's free Workers plan" in result.output
    assert "Always-on/offline search is a later mode and may require enabling Cloudflare billing" in result.output
    assert "Which assistant app do you want help connecting?" in result.output
    assert "claude   Claude web/Desktop/mobile" in result.output
    # The TS Worker subproject is tracked under cloudflare/yutome-capsule/.
    # `setup` no longer generates JS source files into data/remote/.
    assert "Tracked TypeScript Worker subproject lives at:" in result.output
    assert "cloudflare/yutome-capsule" in result.output
    assert not (tmp_path / "data/remote/cloudflare-worker").exists()
    assert not (tmp_path / "data/remote/connection.json").exists()


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
            "show",
            "context",
            "--config",
            str(config_path),
            "--id",
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

    result = runner.invoke(app, ["remote", "sync", "--dry-run", "--json", "--config", str(config_path)])

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
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("YUTOME_GEMINI_MODEL", "gemini-test-model")
    monkeypatch.setenv("YUTOME_GEMINI_MEDIA_RESOLUTION", "medium")
    monkeypatch.setenv("YUTOME_GEMINI_REQUEST_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("YUTOME_GEMINI_WINDOW_SECONDS", "600")

    config = apply_env_to_config(load_config(config_path))

    assert config.proxy.enabled is True
    assert config.proxy.kind == "webshare"
    assert proxy_url_for_ytdlp(config.proxy, key="video123") == "http://proxy-user-rotate:proxy-pass@p.webshare.io:80/"
    assert config.gemini.enabled is True
    assert config.gemini.model == "gemini-test-model"
    assert config.gemini.media_resolution == "medium"
    assert config.gemini.request_timeout_seconds == 45.0
    assert config.gemini.window_seconds == 600


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
