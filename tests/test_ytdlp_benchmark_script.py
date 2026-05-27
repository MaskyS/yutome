from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from yutome.config import AppConfig, ProxyConfig


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


def test_error_classifier_marks_youtube_page_reload() -> None:
    bench = _load_script_module()
    result = subprocess.CompletedProcess(
        ["yt-dlp"],
        1,
        stdout="",
        stderr="ERROR: [youtube] video123: The page needs to be reloaded.",
    )

    assert bench.classify_error(result) == "youtube_page_reload"
    assert bench.diagnostic_markers(result) == ["youtube_page_reload"]


def test_failed_case_records_redacted_diagnostic_tails_without_live_youtube() -> None:
    bench = _load_script_module()
    config = AppConfig(
        proxy=ProxyConfig(
            enabled=True,
            kind="webshare",
            webshare_username="proxy-user",
            webshare_password="proxy-pass",
            webshare_domain="p.webshare.io",
            webshare_port=80,
        )
    )

    def fake_runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        proxy_url = command[command.index("--proxy") + 1]
        stderr = (
            f"ERROR: [youtube] OEDoJyhQhXs: {proxy_url} "
            "CONNECT tunnel failed, response 402; Sign in to confirm you're not a bot"
        )
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=stderr)

    metric = bench.run_case(
        bench.BenchmarkCase(
            operation="metadata",
            variant="current",
            proxy_mode="webshare",
            video_id="OEDoJyhQhXs",
            run_order=3,
            warmup=False,
        ),
        config=config,
        runner=fake_runner,
    )

    assert metric["error_class"] == "proxy_payment_required"
    assert "stderr_tail" in metric
    assert "proxy-user" not in metric["stderr_tail"]
    assert "proxy-pass" not in metric["stderr_tail"]
    assert "http://***:***@p.webshare.io:80/" in metric["stderr_tail"]
    assert "proxy_payment_required" in metric["diagnostic_markers"]
    assert "youtube_block" in metric["diagnostic_markers"]


def test_signal_failure_records_signal_name_and_empty_output_marker() -> None:
    bench = _load_script_module()
    result = subprocess.CompletedProcess(["yt-dlp"], -6, stdout="", stderr="")

    diagnostics = bench.failure_diagnostics(result, proxy=None, key=None)

    assert diagnostics["signal_name"] == "SIGABRT"
    assert diagnostics["diagnostic_markers"] == ["process_signal", "signal_without_output"]


def test_production_webshare_uses_plain_english_subtitles_first_without_live_youtube() -> None:
    bench = _load_script_module()
    config = AppConfig(
        proxy=ProxyConfig(
            enabled=True,
            kind="webshare",
            webshare_username="proxy-user",
            webshare_password="proxy-pass",
            webshare_domain="p.webshare.io",
            webshare_port=80,
        )
    )
    languages: list[str] = []

    def fake_runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        language = command[command.index("--sub-langs") + 1]
        languages.append(language)
        if language == "en":
            output_dir = Path(command[command.index("--paths") + 1])
            payload = {"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "hello"}]}]}
            (output_dir / "OEDoJyhQhXs.en.json3").write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    metric = bench.run_production_case(
        bench.BenchmarkCase(
            operation="subtitles",
            variant="current",
            proxy_mode="webshare",
            video_id="OEDoJyhQhXs",
            run_order=4,
            warmup=False,
            language="en",
        ),
        config=config,
        max_attempts=3,
        runner=fake_runner,
    )

    assert metric["succeeded"] is True
    assert metric["attempts_used"] == 1
    assert metric["success_reason"] == "subtitle_json3_with_segments"
    assert metric["subtitle_segments"] == 1
    assert languages == ["en"]


def test_production_webshare_retries_same_subtitle_language_after_rate_limit() -> None:
    bench = _load_script_module()
    config = AppConfig(
        proxy=ProxyConfig(
            enabled=True,
            kind="webshare",
            webshare_username="proxy-user",
            webshare_password="proxy-pass",
        )
    )
    languages: list[str] = []

    def fake_runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        language = command[command.index("--sub-langs") + 1]
        languages.append(language)
        if len(languages) == 1:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="HTTP Error 429: Too Many Requests")
        output_dir = Path(command[command.index("--paths") + 1])
        payload = {"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "hello"}]}]}
        (output_dir / f"OEDoJyhQhXs.{language}.json3").write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    metric = bench.run_production_case(
        bench.BenchmarkCase(
            operation="subtitles",
            variant="python-no-js",
            proxy_mode="webshare",
            video_id="OEDoJyhQhXs",
            run_order=5,
            warmup=False,
            language="en",
        ),
        config=config,
        max_attempts=3,
        runner=fake_runner,
    )

    assert metric["succeeded"] is True
    assert metric["attempts_used"] == 2
    assert languages == ["en", "en"]
    assert metric["attempts"][0]["failure_reason"] == "process_failed:youtube_rate_limited"


def test_production_webshare_tries_next_subtitle_language_after_empty_output() -> None:
    bench = _load_script_module()
    config = AppConfig(
        proxy=ProxyConfig(
            enabled=True,
            kind="webshare",
            webshare_username="proxy-user",
            webshare_password="proxy-pass",
        )
    )
    languages: list[str] = []

    def fake_runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        language = command[command.index("--sub-langs") + 1]
        languages.append(language)
        if language == "en-orig":
            output_dir = Path(command[command.index("--paths") + 1])
            payload = {"events": [{"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "hello"}]}]}
            (output_dir / "OEDoJyhQhXs.en-orig.json3").write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    metric = bench.run_production_case(
        bench.BenchmarkCase(
            operation="subtitles",
            variant="current",
            proxy_mode="webshare",
            video_id="OEDoJyhQhXs",
            run_order=6,
            warmup=False,
            language="en",
        ),
        config=config,
        max_attempts=3,
        runner=fake_runner,
    )

    assert metric["succeeded"] is True
    assert metric["attempts_used"] == 2
    assert languages == ["en", "en-orig"]
    assert metric["attempts"][0]["failure_reason"] == "subtitle_missing_json3_file"


def test_production_webshare_retries_metadata_until_published_date_without_live_youtube() -> None:
    bench = _load_script_module()
    config = AppConfig(
        proxy=ProxyConfig(
            enabled=True,
            kind="webshare",
            webshare_username="proxy-user",
            webshare_password="proxy-pass",
        )
    )
    calls = 0

    def fake_runner(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        payload = {
            "id": "OEDoJyhQhXs",
            "title": "Benchmark metadata",
            "duration": 60,
            "channel_id": "UCleo",
        }
        if calls == 2:
            payload["upload_date"] = "20220201"
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload) + "\n", stderr="")

    metric = bench.run_production_case(
        bench.BenchmarkCase(
            operation="metadata",
            variant="current",
            proxy_mode="webshare",
            video_id="OEDoJyhQhXs",
            run_order=5,
            warmup=False,
        ),
        config=config,
        max_attempts=2,
        runner=fake_runner,
    )

    assert metric["succeeded"] is True
    assert metric["attempts_used"] == 2
    assert metric["success_reason"] == "metadata_complete_with_published_date"
    assert metric["attempts"][0]["failure_reason"] == "metadata_missing_published_date"


def test_production_webshare_rejects_direct_cases() -> None:
    bench = _load_script_module()

    try:
        bench.run_production_case(
            bench.BenchmarkCase(
                operation="metadata",
                variant="current",
                proxy_mode="direct",
                video_id="OEDoJyhQhXs",
                run_order=6,
                warmup=False,
            ),
            config=AppConfig(),
            max_attempts=1,
        )
    except ValueError as exc:
        assert "Webshare" in str(exc)
    else:
        raise AssertionError("expected direct production benchmark case to be rejected")
