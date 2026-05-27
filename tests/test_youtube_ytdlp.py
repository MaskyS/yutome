from __future__ import annotations

from pathlib import Path
from typing import Any

from yutome.youtube import TranscriptFetchResult, fetch_subtitle_transcript_with_ytdlp


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
