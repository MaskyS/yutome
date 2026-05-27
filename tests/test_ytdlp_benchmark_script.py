from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from yutome.config import AppConfig


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_ytdlp_runtime.py"


def _load_script_module():
    spec = importlib.util.spec_from_file_location("benchmark_ytdlp_runtime", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_metadata_case_extracts_completeness_without_live_youtube() -> None:
    bench = _load_script_module()

    def fake_runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert "--no-js-runtimes" in command
        payload = {
            "id": "OEDoJyhQhXs",
            "title": "Benchmark metadata",
            "duration": 60,
            "upload_date": "20220201",
            "timestamp": 1643750702,
            "channel_id": "UCleo",
            "live_status": "not_live",
            "formats": [{"format_id": "313"}],
            "automatic_captions": {"en": []},
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    metric = bench.run_case(
        bench.BenchmarkCase(
            operation="metadata",
            variant="python-no-js",
            proxy_mode="direct",
            video_id="OEDoJyhQhXs",
            run_order=1,
            warmup=False,
        ),
        config=AppConfig(),
        runner=fake_runner,
    )

    assert metric["returncode"] == 0
    assert metric["error_class"] is None
    assert metric["metadata_complete_core"] is True
    assert metric["published_date_present"] is True
    assert metric["formats_count"] == 1
    assert metric["automatic_captions_count"] == 1


def test_subtitle_case_counts_json3_segments_without_live_youtube() -> None:
    bench = _load_script_module()

    def fake_runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        output_dir = Path(command[command.index("--paths") + 1])
        payload = {
            "events": [
                {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "hello"}]},
                {"tStartMs": 1000, "dDurationMs": 1000, "segs": [{"utf8": " "}]},
                {"tStartMs": 2000, "dDurationMs": 1000, "segs": [{"utf8": "world"}]},
            ]
        }
        (output_dir / "OEDoJyhQhXs.en-orig.json3").write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    metric = bench.run_case(
        bench.BenchmarkCase(
            operation="subtitles",
            variant="current",
            proxy_mode="direct",
            video_id="OEDoJyhQhXs",
            run_order=2,
            warmup=False,
        ),
        config=AppConfig(),
        runner=fake_runner,
    )

    assert metric["returncode"] == 0
    assert metric["subtitle_json3_files"] == 1
    assert metric["subtitle_segments"] == 2
    assert metric["output_file_bytes"] > 0


def test_discovery_metrics_count_flat_rows_without_live_youtube() -> None:
    bench = _load_script_module()
    stdout = "\n".join(
        [
            json.dumps({"_type": "url", "id": "one", "title": "One", "timestamp": 1643750702}),
            json.dumps({"_type": "url", "id": "two", "title": "Two", "upload_date": None}),
        ]
    )

    metric = bench.discovery_metrics(stdout)

    assert metric["discovery_rows"] == 2
    assert metric["discovery_rows_with_id"] == 2
    assert metric["discovery_rows_with_title"] == 2
    assert metric["discovery_rows_with_published_date"] == 1


def test_error_classifier_marks_proxy_payment_required() -> None:
    bench = _load_script_module()
    result = subprocess.CompletedProcess(["yt-dlp"], 1, stdout="", stderr="CONNECT tunnel failed, response 402")

    assert bench.classify_error(result) == "proxy_payment_required"
