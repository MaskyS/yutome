#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from yutome.config import AppConfig, DEFAULT_CONFIG_FILENAME, ProxyConfig, load_config
from yutome.env import apply_env_to_config, load_dotenv
from yutome.youtube import proxy_url_for_ytdlp, redact_proxy_secrets, redact_proxy_url


WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
CORE_METADATA_FIELDS = ("id", "title", "duration", "upload_date", "timestamp", "channel_id", "live_status")
FAILURE_TAIL_CHARS = 2_000
VARIANT_ARGS = {
    "current": (),
    "python-no-js": ("--no-js-runtimes", "--no-remote-components"),
    "player-skip-js": ("--extractor-args", "youtube:player_skip=js"),
}


@dataclass(frozen=True)
class BenchmarkCase:
    operation: str
    variant: str
    proxy_mode: str
    video_id: str
    run_order: int
    warmup: bool
    language: str = "en"
    target: str | None = None


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(Path(args.env_file))
    config = _load_app_config(Path(args.config))
    if args.production_webshare:
        _proxy_modes("webshare", config)
        proxy_modes = ["webshare"]
    else:
        proxy_modes = _proxy_modes(args.proxy_mode, config)
    cases = build_cases(
        video_ids=args.video_id,
        operations=args.operation,
        proxy_modes=proxy_modes,
        variants=args.variant,
        repetitions=args.repetitions,
        warmups=args.warmups,
        language=args.language,
        discovery_target=args.discovery_target,
        seed=args.seed,
    )
    output = Path(args.output) if args.output else None
    sink = output.open("w", encoding="utf-8") if output else sys.stdout
    try:
        for case in cases:
            if args.production_webshare:
                metric = run_production_case(
                    case,
                    config=config,
                    max_attempts=args.production_attempts or (config.yt_dlp.retries_when_blocked + 1),
                    retry_sleep_seconds=args.production_retry_sleep_seconds,
                    timeout_seconds=args.timeout_seconds,
                )
            else:
                metric = run_case(case, config=config, timeout_seconds=args.timeout_seconds)
            print(json.dumps(metric, sort_keys=True, default=str), file=sink, flush=True)
    finally:
        if output:
            sink.close()
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark yt-dlp metadata/subtitle variants for Yutome.")
    parser.add_argument("--config", default=DEFAULT_CONFIG_FILENAME)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--video-id", action="append", default=None)
    parser.add_argument("--operation", choices=("metadata", "subtitles", "discovery"), action="append", default=None)
    parser.add_argument("--variant", choices=tuple(VARIANT_ARGS), action="append", default=None)
    parser.add_argument("--proxy-mode", choices=("auto", "direct", "webshare", "both"), default="auto")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--language", default="en")
    parser.add_argument("--discovery-target", default="https://www.youtube.com/@leoandlongevity/videos")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--timeout-seconds", type=float, default=None)
    parser.add_argument("--output", help="Write JSONL metrics to this path instead of stdout.")
    parser.add_argument(
        "--production-webshare",
        action="store_true",
        help="Run Webshare-only production-style attempts with bounded retry and usability criteria.",
    )
    parser.add_argument(
        "--production-attempts",
        type=int,
        default=None,
        help="Maximum subprocess attempts per production case. Defaults to yt-dlp retry policy + 1.",
    )
    parser.add_argument(
        "--production-retry-sleep-seconds",
        type=float,
        default=0.0,
        help="Sleep between unsuccessful production attempts.",
    )
    parsed = parser.parse_args(argv)
    parsed.video_id = parsed.video_id or ["OEDoJyhQhXs"]
    parsed.operation = parsed.operation or ["metadata", "subtitles"]
    parsed.variant = parsed.variant or list(VARIANT_ARGS)
    return parsed


def build_cases(
    *,
    video_ids: Sequence[str],
    operations: Sequence[str],
    proxy_modes: Sequence[str],
    variants: Sequence[str],
    repetitions: int,
    warmups: int,
    language: str,
    discovery_target: str,
    seed: int | None,
) -> list[BenchmarkCase]:
    rng = random.Random(seed)
    cases: list[BenchmarkCase] = []
    run_order = 0
    for video_id in video_ids:
        base = [
            BenchmarkCase(
                operation=operation,
                variant=variant,
                proxy_mode=proxy_mode,
                video_id=video_id,
                run_order=0,
                warmup=False,
                language=language,
                target=discovery_target if operation == "discovery" else None,
            )
            for operation in operations
            for proxy_mode in proxy_modes
            for variant in variants
        ]
        for iteration in range(warmups + repetitions):
            shuffled = list(base)
            rng.shuffle(shuffled)
            for case in shuffled:
                run_order += 1
                cases.append(
                    BenchmarkCase(
                        operation=case.operation,
                        variant=case.variant,
                        proxy_mode=case.proxy_mode,
                        video_id=case.video_id,
                        run_order=run_order,
                        warmup=iteration < warmups,
                        language=case.language,
                        target=case.target,
                    )
                )
    return cases


def run_case(
    case: BenchmarkCase,
    *,
    config: AppConfig,
    timeout_seconds: float | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="yutome-ytdlp-bench-") as temp_dir:
        command, proxy_url = build_command(case, config=config, output_dir=Path(temp_dir))
        started = time.perf_counter()
        try:
            result = runner(
                command,
                cwd=Path.cwd(),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds or config.yt_dlp.subprocess_timeout_seconds,
            )
            elapsed = time.perf_counter() - started
        except subprocess.TimeoutExpired as exc:
            elapsed = time.perf_counter() - started
            result = subprocess.CompletedProcess(
                command,
                124,
                _decode_timeout_stream(exc.stdout),
                _decode_timeout_stream(exc.stderr),
            )
        metric = {
            "environment": environment_metrics(),
            "operation": case.operation,
            "variant": case.variant,
            "proxy_mode": case.proxy_mode,
            "video_id": case.video_id,
            "run_order": case.run_order,
            "warmup": case.warmup,
            "wall_time_seconds": elapsed,
            "returncode": result.returncode,
            "error_class": classify_error(result),
            "stdout_bytes": len(result.stdout.encode("utf-8")),
            "stderr_bytes": len(result.stderr.encode("utf-8")),
            "command": redact_command(command),
            "proxy_applied": proxy_url is not None,
        }
        metric.update(
            failure_diagnostics(
                result,
                proxy=_proxy_for_mode(case.proxy_mode, config),
                key=case.video_id,
            )
        )
        if case.operation == "metadata":
            metric.update(metadata_metrics(result.stdout))
        elif case.operation == "subtitles":
            metric.update(subtitle_metrics(Path(temp_dir), case.video_id))
        elif case.operation == "discovery":
            metric.update(discovery_metrics(result.stdout))
        return metric


def run_production_case(
    case: BenchmarkCase,
    *,
    config: AppConfig,
    max_attempts: int,
    retry_sleep_seconds: float = 0.0,
    timeout_seconds: float | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    if case.proxy_mode != "webshare":
        raise ValueError("production benchmark only supports Webshare cases")
    if max_attempts < 1:
        raise ValueError("production benchmark requires at least one attempt")

    started = time.perf_counter()
    attempts: list[dict[str, Any]] = []
    success_reason: str | None = None
    subtitle_languages = _production_subtitle_language_candidates(case.language)
    subtitle_language_index = 0
    for attempt_number in range(1, max_attempts + 1):
        attempt_case = _production_attempt_case(
            case,
            subtitle_language=subtitle_languages[subtitle_language_index],
        )
        metric = run_case(
            attempt_case,
            config=config,
            timeout_seconds=timeout_seconds,
            runner=runner,
        )
        metric["attempt_number"] = attempt_number
        metric["success_reason"] = production_success_reason(metric)
        metric["failure_reason"] = production_failure_reason(metric)
        attempts.append(metric)
        success_reason = metric["success_reason"]
        if success_reason:
            break
        if (
            case.operation == "subtitles"
            and _should_try_next_subtitle_language(metric)
            and subtitle_language_index < len(subtitle_languages) - 1
        ):
            subtitle_language_index += 1
        if attempt_number < max_attempts and retry_sleep_seconds > 0:
            time.sleep(retry_sleep_seconds)

    elapsed = time.perf_counter() - started
    final = attempts[-1]
    succeeded = success_reason is not None
    summary = {
        "environment": final["environment"],
        "operation": case.operation,
        "variant": case.variant,
        "proxy_mode": case.proxy_mode,
        "video_id": case.video_id,
        "run_order": case.run_order,
        "warmup": case.warmup,
        "production_webshare": True,
        "production_max_attempts": max_attempts,
        "attempts_used": len(attempts),
        "succeeded": succeeded,
        "success_reason": success_reason,
        "failure_reason": None if succeeded else final.get("failure_reason"),
        "wall_time_seconds": elapsed,
        "attempt_wall_time_seconds": sum(float(attempt["wall_time_seconds"]) for attempt in attempts),
        "returncode": 0 if succeeded else final["returncode"],
        "error_class": None if succeeded else final["error_class"],
        "stdout_bytes": sum(int(attempt["stdout_bytes"]) for attempt in attempts),
        "stderr_bytes": sum(int(attempt["stderr_bytes"]) for attempt in attempts),
        "proxy_applied": True,
        "attempts": [_production_attempt_summary(attempt) for attempt in attempts],
    }
    summary.update(_final_operation_metrics(final))
    if not succeeded:
        summary.update(
            {
                key: final[key]
                for key in ("stderr_tail", "stdout_tail", "signal_name", "diagnostic_markers")
                if key in final
            }
        )
    return summary


def production_success_reason(metric: dict[str, Any]) -> str | None:
    if metric.get("returncode") != 0:
        return None
    if metric["operation"] == "metadata":
        if metric.get("metadata_complete_core") and metric.get("published_date_present"):
            return "metadata_complete_with_published_date"
        return None
    if metric["operation"] == "subtitles":
        if metric.get("subtitle_json3_files", 0) > 0 and metric.get("subtitle_segments", 0) > 0:
            return "subtitle_json3_with_segments"
        return None
    if metric["operation"] == "discovery":
        if metric.get("discovery_rows_with_id", 0) > 0:
            return "discovery_rows_with_id"
        return None
    return None


def production_failure_reason(metric: dict[str, Any]) -> str | None:
    if metric.get("returncode") != 0:
        error_class = metric.get("error_class") or "yt_dlp_failed"
        return f"process_failed:{error_class}"
    if metric["operation"] == "metadata":
        if not metric.get("metadata_complete_core"):
            return "metadata_missing_core_fields"
        if not metric.get("published_date_present"):
            return "metadata_missing_published_date"
    if metric["operation"] == "subtitles":
        if metric.get("subtitle_json3_files", 0) == 0:
            return "subtitle_missing_json3_file"
        if metric.get("subtitle_segments", 0) == 0:
            return "subtitle_missing_text_segments"
    if metric["operation"] == "discovery" and metric.get("discovery_rows_with_id", 0) == 0:
        return "discovery_missing_rows"
    return None


def build_command(case: BenchmarkCase, *, config: AppConfig, output_dir: Path) -> tuple[list[str], str | None]:
    proxy = _proxy_for_mode(case.proxy_mode, config)
    proxy_url = proxy_url_for_ytdlp(proxy, key=case.video_id)
    command = [*_yt_dlp_base_command(), "--ignore-config", "--no-warnings"]
    command.extend(_project_ytdlp_args(config, proxy_url=proxy_url))
    if proxy_url:
        command.extend(["--proxy", proxy_url])
    command.extend(VARIANT_ARGS[case.variant])
    if case.operation == "metadata":
        command.extend(["--skip-download", "--dump-json", WATCH_URL.format(video_id=case.video_id)])
    elif case.operation == "subtitles":
        sleep_subtitles = (
            config.yt_dlp.sleep_subtitles_seconds_with_proxy
            if proxy_url
            else config.yt_dlp.sleep_subtitles_seconds
        )
        command.extend(
            [
                "--skip-download",
                "--write-auto-subs",
                "--write-subs",
                "--sub-langs",
                case.language,
                "--sub-format",
                "json3",
                "--sleep-subtitles",
                str(sleep_subtitles),
                "--paths",
                str(output_dir),
                "-o",
                "%(id)s.%(ext)s",
                WATCH_URL.format(video_id=case.video_id),
            ]
        )
    elif case.operation == "discovery":
        command.extend(
            [
                "--flat-playlist",
                "--dump-json",
                "--playlist-end",
                "1",
                "--extractor-args",
                "youtubetab:approximate_date=1",
                case.target or WATCH_URL.format(video_id=case.video_id),
            ]
        )
    else:
        raise ValueError(f"unsupported operation: {case.operation}")
    return command, proxy_url


def _project_ytdlp_args(config: AppConfig, *, proxy_url: str | None) -> list[str]:
    sleep_requests = (
        config.yt_dlp.sleep_requests_seconds_with_proxy
        if proxy_url
        else config.yt_dlp.sleep_requests_seconds
    )
    args = ["--sleep-requests", str(sleep_requests), "--retry-sleep", config.yt_dlp.retry_sleep]
    if config.yt_dlp.impersonate:
        args.extend(["--impersonate", config.yt_dlp.impersonate])
    if config.yt_dlp.remote_components:
        args.extend(["--remote-components", "ejs:github"])
    return args


def metadata_metrics(stdout: str) -> dict[str, Any]:
    rows = _json_lines(stdout)
    row = rows[0] if rows and isinstance(rows[0], dict) else {}
    present = [field for field in CORE_METADATA_FIELDS if row.get(field) is not None]
    return {
        "metadata_present_fields": present,
        "metadata_complete_core": all(field in present for field in ("id", "title", "duration", "channel_id")),
        "published_date_present": any(row.get(field) is not None for field in ("upload_date", "release_date", "modified_date", "timestamp")),
        "metadata_type": row.get("_type"),
        "formats_count": len(row.get("formats") or []) if isinstance(row.get("formats"), list) else 0,
        "automatic_captions_count": len(row.get("automatic_captions") or {}) if isinstance(row.get("automatic_captions"), dict) else 0,
    }


def subtitle_metrics(output_dir: Path, video_id: str) -> dict[str, Any]:
    files = sorted(output_dir.glob(f"{video_id}*.json3"))
    output_bytes = sum(path.stat().st_size for path in files)
    segments = 0
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for event in payload.get("events", []):
            if not isinstance(event, dict):
                continue
            text = "".join(str(seg.get("utf8", "")) for seg in event.get("segs", []) if isinstance(seg, dict)).strip()
            if text:
                segments += 1
    return {"subtitle_json3_files": len(files), "subtitle_segments": segments, "output_file_bytes": output_bytes}


def discovery_metrics(stdout: str) -> dict[str, Any]:
    rows = [row for row in _json_lines(stdout) if isinstance(row, dict)]
    return {
        "discovery_rows": len(rows),
        "discovery_rows_with_id": sum(1 for row in rows if row.get("id")),
        "discovery_rows_with_title": sum(1 for row in rows if row.get("title")),
        "discovery_rows_with_published_date": sum(
            1 for row in rows if any(row.get(field) is not None for field in ("upload_date", "release_date", "modified_date", "timestamp"))
        ),
    }


def classify_error(result: subprocess.CompletedProcess[str]) -> str | None:
    if result.returncode == 0:
        return None
    if result.returncode == 124:
        return "timeout"
    if result.returncode < 0:
        return f"signal_{abs(result.returncode)}"
    text = f"{result.stdout}\n{result.stderr}".lower()
    if "402" in text or "payment required" in text:
        return "proxy_payment_required"
    if "429" in text or "too many requests" in text or "rate limit" in text:
        return "youtube_rate_limited"
    if "captcha" in text or "not a bot" in text or "sign in" in text or "/sorry/" in text:
        return "youtube_block"
    if "page needs to be reloaded" in text:
        return "youtube_page_reload"
    return "yt_dlp_failed"


def failure_diagnostics(
    result: subprocess.CompletedProcess[str],
    *,
    proxy: ProxyConfig | None,
    key: str | None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    name = signal_name(result.returncode)
    if name:
        diagnostics["signal_name"] = name
    if result.returncode == 0:
        return diagnostics

    stdout = redact_proxy_secrets(proxy, result.stdout, key=key)
    stderr = redact_proxy_secrets(proxy, result.stderr, key=key)
    if stdout:
        diagnostics["stdout_tail"] = _tail_text(stdout, FAILURE_TAIL_CHARS)
    if stderr:
        diagnostics["stderr_tail"] = _tail_text(stderr, FAILURE_TAIL_CHARS)
    diagnostics["diagnostic_markers"] = diagnostic_markers(result)
    return diagnostics


def diagnostic_markers(result: subprocess.CompletedProcess[str]) -> list[str]:
    markers: list[str] = []
    text = f"{result.stdout}\n{result.stderr}".lower()
    if result.returncode == 124:
        markers.append("timeout")
    if result.returncode < 0:
        markers.append("process_signal")
        if not result.stdout and not result.stderr:
            markers.append("signal_without_output")
    if "402" in text or "payment required" in text:
        markers.append("proxy_payment_required")
    if "407" in text or "proxy authentication" in text or "proxy auth" in text:
        markers.append("proxy_auth_failed")
    if "403" in text or "forbidden" in text:
        markers.append("http_403_or_forbidden")
    if "429" in text or "too many requests" in text or "rate limit" in text:
        markers.append("youtube_rate_limited")
    if "captcha" in text or "not a bot" in text or "sign in" in text or "/sorry/" in text:
        markers.append("youtube_block")
    if "page needs to be reloaded" in text:
        markers.append("youtube_page_reload")
    if "unable to download webpage" in text or "unable to download api page" in text:
        markers.append("youtube_download_failed")
    if "timed out" in text or "timeout" in text:
        markers.append("network_timeout")
    if "curl_cffi" in text or "curl" in text:
        markers.append("curl_transport")
    return markers


def signal_name(returncode: int) -> str | None:
    if returncode >= 0:
        return None
    signal_number = abs(returncode)
    try:
        return signal.Signals(signal_number).name
    except ValueError:
        return f"signal_{signal_number}"


def environment_metrics() -> dict[str, Any]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "yt_dlp": _yt_dlp_version(),
        "curl_cffi_available": importlib.util.find_spec("curl_cffi") is not None,
        "js_runtimes": {name: shutil.which(name) is not None for name in ("deno", "node", "quickjs", "bun")},
    }


def redact_command(command: Sequence[str]) -> list[str]:
    redacted = list(command)
    for index, value in enumerate(redacted):
        if index > 0 and redacted[index - 1] == "--proxy":
            redacted[index] = redact_proxy_url(value)
    return redacted


def _production_attempt_case(case: BenchmarkCase, *, subtitle_language: str) -> BenchmarkCase:
    if case.operation != "subtitles":
        return case
    return replace(case, language=subtitle_language)


def _production_subtitle_language_candidates(language: str) -> list[str]:
    if language == "en":
        return ["en", "en-orig"]
    return [language]


def _should_try_next_subtitle_language(metric: dict[str, Any]) -> bool:
    return metric.get("returncode") == 0 and metric.get("failure_reason") in {
        "subtitle_missing_json3_file",
        "subtitle_missing_text_segments",
    }


def _production_attempt_summary(metric: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "attempt_number",
        "operation",
        "variant",
        "proxy_mode",
        "video_id",
        "wall_time_seconds",
        "returncode",
        "error_class",
        "success_reason",
        "failure_reason",
        "stdout_bytes",
        "stderr_bytes",
        "command",
        "metadata_complete_core",
        "published_date_present",
        "subtitle_json3_files",
        "subtitle_segments",
        "output_file_bytes",
        "discovery_rows",
        "discovery_rows_with_id",
        "stderr_tail",
        "stdout_tail",
        "signal_name",
        "diagnostic_markers",
    ]
    return {key: metric[key] for key in keys if key in metric}


def _final_operation_metrics(metric: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "metadata_present_fields",
        "metadata_complete_core",
        "published_date_present",
        "metadata_type",
        "formats_count",
        "automatic_captions_count",
        "subtitle_json3_files",
        "subtitle_segments",
        "output_file_bytes",
        "discovery_rows",
        "discovery_rows_with_id",
        "discovery_rows_with_title",
        "discovery_rows_with_published_date",
    ]
    return {key: metric[key] for key in keys if key in metric}


def _tail_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"...{text[-max_chars:]}"


def _load_app_config(path: Path) -> AppConfig:
    config = load_config(path) if path.exists() else AppConfig()
    return apply_env_to_config(config)


def _proxy_modes(value: str, config: AppConfig) -> list[str]:
    has_webshare = bool(
        config.proxy.enabled
        and config.proxy.kind == "webshare"
        and config.proxy.webshare_username
        and config.proxy.webshare_password
    )
    if value == "auto":
        return ["direct", "webshare"] if has_webshare else ["direct"]
    if value == "both":
        if not has_webshare:
            raise SystemExit("proxy-mode=both requires Webshare credentials")
        return ["direct", "webshare"]
    if value == "webshare" and not has_webshare:
        raise SystemExit("proxy-mode=webshare requires Webshare credentials")
    return [value]


def _proxy_for_mode(proxy_mode: str, config: AppConfig) -> ProxyConfig | None:
    if proxy_mode == "direct":
        return None
    if proxy_mode == "webshare":
        return config.proxy
    raise ValueError(f"unsupported proxy mode: {proxy_mode}")


def _yt_dlp_base_command() -> list[str]:
    return [sys.executable, "-m", "yt_dlp"] if importlib.util.find_spec("yt_dlp") else ["yt-dlp"]


def _yt_dlp_version() -> str | None:
    try:
        import yt_dlp
    except ImportError:
        return None
    return getattr(yt_dlp.version, "__version__", None)


def _json_lines(stdout: str) -> list[Any]:
    rows: list[Any] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            rows.append(json.loads(stripped))
        except json.JSONDecodeError:
            continue
    return rows


def _decode_timeout_stream(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
