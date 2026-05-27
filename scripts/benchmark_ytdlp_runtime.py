#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yutome.config import AppConfig, DEFAULT_CONFIG_FILENAME, ProxyConfig, load_config
from yutome.env import apply_env_to_config, load_dotenv
from yutome.youtube import proxy_url_for_ytdlp, redact_proxy_url


WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
CORE_METADATA_FIELDS = ("id", "title", "duration", "upload_date", "timestamp", "channel_id", "live_status")
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
    language: str = "en-orig"
    target: str | None = None


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(Path(args.env_file))
    config = _load_app_config(Path(args.config))
    cases = build_cases(
        video_ids=args.video_id,
        operations=args.operation,
        proxy_modes=_proxy_modes(args.proxy_mode, config),
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
    parser.add_argument("--language", default="en-orig")
    parser.add_argument("--discovery-target", default="https://www.youtube.com/@leoandlongevity/videos")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--timeout-seconds", type=float, default=None)
    parser.add_argument("--output", help="Write JSONL metrics to this path instead of stdout.")
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
        if case.operation == "metadata":
            metric.update(metadata_metrics(result.stdout))
        elif case.operation == "subtitles":
            metric.update(subtitle_metrics(Path(temp_dir), case.video_id))
        elif case.operation == "discovery":
            metric.update(discovery_metrics(result.stdout))
        return metric


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
    return "yt_dlp_failed"


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
