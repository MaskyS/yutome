from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from yutome.config import ProxyConfig, YtDlpConfig
from yutome.youtube import TranscriptFetchResult, fetch_subtitle_transcript_with_ytdlp, fetch_video_metadata


def test_ytdlp_english_subtitle_fallback_tries_en_orig_after_plain_en(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_fetch_language(**kwargs: Any) -> TranscriptFetchResult:
        language = kwargs["language"]
        calls.append(language)
        if language == "en":
            raise RuntimeError("yt-dlp did not write json3 subtitles for video123")
        return TranscriptFetchResult(
            raw_snippets=[{"text": "hello", "start": 0.0, "duration": 1.0}],
            source=f"yt-dlp-json3:{language}",
            language=language,
            is_generated=True,
        )

    monkeypatch.setattr("yutome.youtube._fetch_subtitle_transcript_with_ytdlp_language", fake_fetch_language)

    result = fetch_subtitle_transcript_with_ytdlp(
        video_id="video123",
        cwd=tmp_path,
        language="en",
        allow_translated_captions=False,
    )

    assert calls == ["en", "en-orig"]
    assert result.language == "en-orig"
    assert result.raw_snippets == [{"text": "hello", "start": 0.0, "duration": 1.0}]


def test_ytdlp_requests_translated_captions_when_allowed(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def fake_fetch_language(**kwargs: Any) -> TranscriptFetchResult:
        calls.append(dict(kwargs))
        language = kwargs["language"]
        if language in {"en", "en-orig"}:
            raise RuntimeError("yt-dlp did not write json3 subtitles for video123")
        return TranscriptFetchResult(
            raw_snippets=[{"text": "translated hello", "start": 0.0, "duration": 1.0}],
            source=f"yt-dlp-json3:translated:{language}",
            language=language,
            is_generated=True,
        )

    monkeypatch.setattr("yutome.youtube._fetch_subtitle_transcript_with_ytdlp_language", fake_fetch_language)

    result = fetch_subtitle_transcript_with_ytdlp(
        video_id="video123",
        cwd=tmp_path,
        language="en",
        allow_translated_captions=True,
    )

    assert [call["language"] for call in calls] == ["en", "en-orig", "en-.*"]
    assert all(call["allow_translated_captions"] for call in calls)
    assert result.language == "en-.*"
    assert result.raw_snippets == [{"text": "translated hello", "start": 0.0, "duration": 1.0}]


def test_ytdlp_metadata_uses_python_no_js_profile_by_default(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        payload = {
            "id": "video123",
            "title": "Profile test",
            "duration": 30,
            "channel_id": "UC123",
            "upload_date": "20260527",
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr("yutome.youtube.subprocess.run", fake_run)

    metadata = fetch_video_metadata(video_id="video123", cwd=tmp_path)

    assert metadata["title"] == "Profile test"
    assert "--no-js-runtimes" in commands[0]
    assert "--no-remote-components" in commands[0]
    assert "--remote-components" not in commands[0]


def test_ytdlp_metadata_falls_back_to_current_profile_after_retryable_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if len(commands) == 1:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="HTTP Error 429: Too Many Requests")
        payload = {
            "id": "video123",
            "title": "Fallback test",
            "duration": 30,
            "channel_id": "UC123",
            "upload_date": "20260527",
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr("yutome.youtube.subprocess.run", fake_run)

    metadata = fetch_video_metadata(video_id="video123", cwd=tmp_path)

    assert metadata["title"] == "Fallback test"
    assert "--no-js-runtimes" in commands[0]
    assert "--no-js-runtimes" not in commands[1]
    assert "--no-remote-components" not in commands[1]


def test_ytdlp_metadata_retries_missing_published_date_before_fallback(
    monkeypatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr("yutome.youtube._sleep_before_ytdlp_retry", lambda attempt: None)

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        payload = {
            "id": "video123",
            "title": "Retry test",
            "duration": 30,
            "channel_id": "UC123",
        }
        if len(commands) == 2:
            payload["upload_date"] = "20260527"
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    monkeypatch.setattr("yutome.youtube.subprocess.run", fake_run)

    metadata = fetch_video_metadata(
        video_id="video123",
        cwd=tmp_path,
        proxy=ProxyConfig(enabled=True, kind="generic", http="http://proxy.example:8080"),
        ytdlp_config=YtDlpConfig(retries_when_blocked=1),
    )

    assert metadata["upload_date"] == "20260527"
    assert len(commands) == 2
    assert "--no-js-runtimes" in commands[0]
    assert "--no-js-runtimes" in commands[1]


def test_ytdlp_subtitle_retries_same_language_after_page_reload(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []
    monkeypatch.setattr("yutome.youtube._sleep_before_ytdlp_retry", lambda attempt: None)

    def fake_fetch_language(**kwargs: Any) -> TranscriptFetchResult:
        language = kwargs["language"]
        calls.append(language)
        if len(calls) == 1:
            raise RuntimeError("The page needs to be reloaded")
        return TranscriptFetchResult(
            raw_snippets=[{"text": "hello", "start": 0.0, "duration": 1.0}],
            source=f"yt-dlp-json3:{language}",
            language=language,
            is_generated=True,
        )

    monkeypatch.setattr("yutome.youtube._fetch_subtitle_transcript_with_ytdlp_language", fake_fetch_language)

    result = fetch_subtitle_transcript_with_ytdlp(
        video_id="video123",
        cwd=tmp_path,
        language="en",
        proxy=ProxyConfig(enabled=True, kind="generic", http="http://proxy.example:8080"),
        ytdlp_config=YtDlpConfig(retries_when_blocked=1),
    )

    assert calls == ["en", "en"]
    assert result.language == "en"
