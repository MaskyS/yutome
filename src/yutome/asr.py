from __future__ import annotations

import shutil
import tempfile
from functools import lru_cache
from pathlib import Path

from yutome.config import AsrConfig, ProxyConfig, YtDlpConfig
from yutome.youtube import TranscriptFetchResult, download_audio_for_asr


@lru_cache(maxsize=4)
def _cached_whisper_model(model: str, device: str, compute_type: str):
    from faster_whisper import WhisperModel

    return WhisperModel(
        model,
        device=device,
        compute_type=compute_type,
    )


def transcribe_with_faster_whisper(
    *,
    video_id: str,
    cwd: Path,
    config: AsrConfig,
    proxy: ProxyConfig | None = None,
    ytdlp_config: YtDlpConfig | None = None,
    word_timestamps: bool = False,
) -> TranscriptFetchResult:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("faster-whisper is not installed; run `uv sync` or reinstall yutome") from exc
    del WhisperModel

    temp_dir = Path(tempfile.mkdtemp(prefix="yutome-asr-"))
    try:
        audio_path = download_audio_for_asr(
            video_id=video_id,
            cwd=cwd,
            output_dir=temp_dir,
            proxy=proxy,
            ytdlp_config=ytdlp_config,
        )
        model = _cached_whisper_model(config.model, config.device, config.compute_type)
        segments, _info = model.transcribe(
            str(audio_path),
            word_timestamps=word_timestamps,
            vad_filter=True,
        )
        snippets = []
        for segment in segments:
            text = segment.text.strip()
            if not text:
                continue
            snippets.append(
                {
                    "text": text,
                    "start": float(segment.start),
                    "duration": max(0.0, float(segment.end) - float(segment.start)),
                }
            )
        return TranscriptFetchResult(
            raw_snippets=snippets,
            source=f"faster-whisper:{config.model}",
            language="en" if config.model.endswith(".en") else None,
            is_generated=True,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
