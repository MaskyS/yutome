from __future__ import annotations

import hashlib
import json
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, TypeVar
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

from requests import Session
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig

from yutome.config import ProxyConfig, YtDlpConfig
from yutome.hosted.models import UsageNormalization
from yutome.hosted.normalizers import normalize_webshare_activity
from yutome.hosted.provider_wrappers import (
    ProviderCallContext,
    UsageReservationDenied,
    execute_provider_call,
)


T = TypeVar("T")


@dataclass(frozen=True)
class DiscoveredVideo:
    video_id: str
    title: str | None
    url: str
    channel_id: str | None
    channel_title: str | None
    channel_handle: str | None
    duration_seconds: int | None
    playlist_tab: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class TranscriptFetchResult:
    raw_snippets: list[dict[str, Any]]
    source: str
    language: str | None
    is_generated: bool


@dataclass(frozen=True)
class AvailableTranscript:
    language_code: str
    language: str
    is_generated: bool
    is_translatable: bool


class TimeoutSession(Session):
    def __init__(self, *, timeout_seconds: float):
        super().__init__()
        self.timeout_seconds = timeout_seconds

    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", self.timeout_seconds)
        return super().request(method, url, **kwargs)


YOUTUBE_BLOCK_MARKERS = (
    "429",
    "too many requests",
    "sign in to confirm",
    "not a bot",
    "captcha",
    "blocking requests from your ip",
    "ip blocked",
    "/sorry/",
    "sorry/index",
    "temporarily blocked",
)

YTDLP_RETRYABLE_TRANSIENT_MARKERS = (
    "the page needs to be reloaded",
    "timed out after",
    "response ended prematurely",
    "connection broken",
    "incompleteread",
    "could not resolve host",
    "temporary failure in name resolution",
    "connection reset",
    "connection aborted",
)

PROXY_PAYMENT_MARKERS = (
    "402 payment required",
    "connect tunnel failed, response 402",
    "tunnel connection failed: 402",
    "response 402",
)

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
YOUTUBE_VIDEO_ID_RE = r"[A-Za-z0-9_-]{11}"
YtDlpProfile = Literal["current", "python-no-js", "player-skip-js"]


class _YtDlpValidationError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        self.retryable = retryable
        super().__init__(message)


def is_youtube_block_error(error: Exception | str) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in YOUTUBE_BLOCK_MARKERS)


def is_proxy_payment_error(error: Exception | str) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in PROXY_PAYMENT_MARKERS)


_is_ytdlp_block_error = is_youtube_block_error


def _is_ytdlp_retryable_error(error: Exception | str) -> bool:
    text = str(error).lower()
    if is_proxy_payment_error(text):
        return False
    return is_youtube_block_error(text) or any(marker in text for marker in YTDLP_RETRYABLE_TRANSIENT_MARKERS)


def canonical_channel_tabs(target: str) -> list[tuple[str, str]]:
    cleaned = target.rstrip("/")
    parsed = urlsplit(cleaned if "://" in cleaned else f"https://www.youtube.com/{cleaned.lstrip('/')}")
    parts = [part for part in parsed.path.split("/") if part]
    if (parts and parts[0] == "playlist") or parse_qs(parsed.query).get("list"):
        return [("playlist", cleaned)]
    if cleaned.endswith("/videos") or cleaned.endswith("/streams"):
        return [(cleaned.rsplit("/", 1)[-1], cleaned)]
    return [("videos", f"{cleaned}/videos"), ("streams", f"{cleaned}/streams")]


def canonical_video_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def extract_video_id(value: str) -> str | None:
    stripped = value.strip()
    if re.fullmatch(YOUTUBE_VIDEO_ID_RE, stripped):
        return stripped
    parsed = urlsplit(stripped if "://" in stripped else f"https://www.youtube.com/{stripped.lstrip('/')}")
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/", 1)[0]
        return candidate if re.fullmatch(YOUTUBE_VIDEO_ID_RE, candidate) else None
    if host not in YOUTUBE_HOSTS and not host.endswith(".youtube.com"):
        return None
    query = parse_qs(parsed.query)
    if video_id := query.get("v", [None])[0]:
        return video_id if re.fullmatch(YOUTUBE_VIDEO_ID_RE, video_id) else None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
        return parts[1] if re.fullmatch(YOUTUBE_VIDEO_ID_RE, parts[1]) else None
    return None


def _yt_dlp_base_command() -> list[str]:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return ["yt-dlp"]
    return [sys.executable, "-m", "yt_dlp"]


def _effective_ytdlp_config(ytdlp_config: YtDlpConfig | None) -> YtDlpConfig:
    return ytdlp_config or YtDlpConfig()


def _yt_dlp_profile_args(profile: YtDlpProfile) -> list[str]:
    if profile == "python-no-js":
        return ["--no-js-runtimes", "--no-remote-components"]
    if profile == "player-skip-js":
        return ["--extractor-args", "youtube:player_skip=js"]
    return []


def _yt_dlp_profile_sequence(ytdlp_config: YtDlpConfig | None) -> tuple[YtDlpProfile, ...]:
    config = _effective_ytdlp_config(ytdlp_config)
    profiles: list[YtDlpProfile] = [config.profile]
    if config.profile_fallback_enabled and config.fallback_profile and config.fallback_profile not in profiles:
        profiles.append(config.fallback_profile)
    return tuple(profiles)


def _yt_dlp_attempts_per_profile(proxy: ProxyConfig | None, ytdlp_config: YtDlpConfig | None) -> int:
    config = _effective_ytdlp_config(ytdlp_config)
    if proxy and proxy.enabled:
        return 1 + config.retries_when_blocked
    return 1


def _sleep_before_ytdlp_retry(attempt: int) -> None:
    time.sleep(min(5 * attempt, 15))


def _pick_from_pool(urls: list[str], *, key: str | None = None) -> str | None:
    if not urls:
        return None
    if key is None:
        return urls[0]
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return urls[int(digest[:12], 16) % len(urls)]


class _HostedWebshareProxyFailure(RuntimeError):
    def __init__(self, result: subprocess.CompletedProcess[str], *, status_code: int, message: str) -> None:
        self.result = result
        self.status_code = status_code
        super().__init__(message)


def proxy_url_for_ytdlp(
    proxy: ProxyConfig | None,
    *,
    key: str | None = None,
    hosted_context: ProviderCallContext | None = None,
) -> str | None:
    if _uses_hosted_webshare_proxy(proxy, hosted_context):
        return _execute_hosted_webshare_proxy_call(
            proxy,
            hosted_context,
            lambda: _proxy_url_for_ytdlp(proxy, key=key),
            target=key,
            source="yt-dlp.proxy_url",
        )
    return _proxy_url_for_ytdlp(proxy, key=key)


def _proxy_url_for_ytdlp(proxy: ProxyConfig | None, *, key: str | None = None) -> str | None:
    if not proxy or not proxy.enabled:
        return None
    if proxy.kind == "generic":
        if selected := _pick_from_pool(proxy.urls, key=key):
            return selected
        return proxy.https or proxy.http
    if proxy.webshare_username and proxy.webshare_password:
        username = proxy.webshare_username
        rotate_suffix = "-rotate"
        if username.endswith(rotate_suffix):
            username = username[: -len(rotate_suffix)]
        location_codes = "".join(f"-{location.upper()}" for location in proxy.webshare_locations)
        username = quote(f"{username}{location_codes}{rotate_suffix}", safe="")
        password = quote(proxy.webshare_password, safe="")
        return f"http://{username}:{password}@{proxy.webshare_domain}:{proxy.webshare_port}/"
    return None


def _uses_hosted_webshare_proxy(
    proxy: ProxyConfig | None,
    hosted_context: ProviderCallContext | None,
) -> bool:
    return bool(
        hosted_context is not None
        and proxy
        and proxy.enabled
        and proxy.kind == "webshare"
        and proxy.webshare_username
        and proxy.webshare_password
    )


def _execute_hosted_webshare_proxy_call(
    proxy: ProxyConfig | None,
    hosted_context: ProviderCallContext,
    call: Callable[[], T],
    *,
    target: str | None,
    source: str,
) -> T:
    activity_payload: dict[str, Any] = {}

    def metered_call() -> T:
        started_at = time.monotonic()
        result = call()
        activity_payload.update(
            _webshare_proxy_activity_payload(
                proxy,
                result,
                target=target,
                source=source,
                duration_seconds=time.monotonic() - started_at,
            )
        )
        return result

    def normalize_usage(_: T) -> UsageNormalization:
        normalized = normalize_webshare_activity(activity_payload, operation=hosted_context.operation)
        normalized.actual_units["request_count"] = 1
        normalized.metadata = {
            **normalized.metadata,
            "accounting_source": source,
            "proxy_domain": proxy.webshare_domain if proxy else None,
            "proxy_port": proxy.webshare_port if proxy else None,
        }
        return normalized

    return execute_provider_call(hosted_context, metered_call, normalize_usage=normalize_usage)


def _webshare_proxy_activity_payload(
    proxy: ProxyConfig | None,
    result: Any,
    *,
    target: str | None,
    source: str,
    duration_seconds: float,
) -> dict[str, Any]:
    hostname = _proxy_target_hostname(target)
    byte_metrics = _locally_visible_webshare_byte_metrics(result, target=target, source=source)
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "request_duration": duration_seconds,
        "handshake_duration": 0.0,
        "tunnel_duration": 0.0,
        "hostname": hostname,
        "domain": hostname,
        "error_reason": None,
        "auth_username": None,
        "accounting_source": source,
        "provider_byte_accounting": "async_webshare_proxy_activity_or_stats",
        "provider_bytes_exact_for_call": False,
        "proxy_domain": proxy.webshare_domain if proxy else None,
        "proxy_port": proxy.webshare_port if proxy else None,
        **byte_metrics,
    }


def _proxy_target_hostname(target: str | None) -> str:
    if not target:
        return "www.youtube.com"
    parsed = urlsplit(target if "://" in target else f"https://www.youtube.com/watch?v={target}")
    return parsed.netloc.lower() or "www.youtube.com"


def _locally_visible_webshare_byte_metrics(result: Any, *, target: str | None, source: str) -> dict[str, Any]:
    response_bytes = _visible_response_bytes(result)
    request_bytes = _request_target_bytes(target)
    accounted_bytes = response_bytes + request_bytes
    status = "locally_visible_transfer"
    if source == "yt-dlp.proxy_url":
        status = "no_proxy_transfer_initiated"
        request_bytes = 0
        accounted_bytes = response_bytes
    elif response_bytes == 0 and request_bytes == 0:
        status = "local_transfer_unavailable"
    return {
        "bytes": accounted_bytes,
        "local_request_bytes": request_bytes,
        "local_response_bytes": response_bytes,
        "byte_accounting_status": status,
        "byte_accounting_basis": _byte_accounting_basis(result, source=source),
    }


def _visible_response_bytes(result: Any) -> int:
    if isinstance(result, subprocess.CompletedProcess):
        return _byte_len(result.stdout) + int(getattr(result, "yutome_output_file_bytes", 0) or 0)
    if isinstance(result, TranscriptFetchResult):
        return _json_byte_len(
            {
                "raw_snippets": result.raw_snippets,
                "source": result.source,
                "language": result.language,
                "is_generated": result.is_generated,
            }
        )
    if isinstance(result, list) and all(isinstance(item, AvailableTranscript) for item in result):
        return _json_byte_len([asdict(item) for item in result])
    if isinstance(result, str):
        return 0
    if is_dataclass(result):
        return _json_byte_len(asdict(result))
    if isinstance(result, (dict, list, tuple)):
        return _json_byte_len(result)
    return 0


def _request_target_bytes(target: str | None) -> int:
    if not target:
        return 0
    url = _proxy_accounting_target_url(target)
    parsed = urlsplit(url)
    if not parsed.netloc:
        return 0
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    request_line = f"GET {path} HTTP/1.1\r\nHost: {parsed.netloc}\r\n\r\n"
    return len(request_line.encode("utf-8"))


def _proxy_accounting_target_url(target: str) -> str:
    if "://" in target:
        return target
    if re.fullmatch(YOUTUBE_VIDEO_ID_RE, target):
        return canonical_video_url(target)
    return f"https://www.youtube.com/{target.lstrip('/')}"


def _byte_accounting_basis(result: Any, *, source: str) -> str:
    if source == "yt-dlp.proxy_url":
        return "proxy_url_generation_only"
    if isinstance(result, subprocess.CompletedProcess):
        return "yt_dlp_stdout_plus_target_request_estimate"
    if isinstance(result, TranscriptFetchResult):
        return "transcript_result_json_plus_target_request_estimate"
    if isinstance(result, list) and all(isinstance(item, AvailableTranscript) for item in result):
        return "transcript_list_json_plus_target_request_estimate"
    return "target_request_estimate"


def _byte_len(value: str | bytes | None) -> int:
    if value is None:
        return 0
    if isinstance(value, bytes):
        return len(value)
    return len(value.encode("utf-8"))


def _json_byte_len(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _webshare_proxy_failure_status_code(result: subprocess.CompletedProcess[str]) -> int | None:
    detail = (result.stderr or "") + "\n" + (result.stdout or "")
    text = detail.lower()
    if is_proxy_payment_error(detail):
        return 402
    if "407 proxy authentication required" in text or "proxy authentication required" in text:
        return 407
    if "webshare" in text and "unauthorized" in text:
        return 401
    if "webshare" in text and "forbidden" in text:
        return 403
    return None


def redact_proxy_url(url: str | None) -> str:
    if not url:
        return "none"
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, f"***:***@{host}", parts.path, "", ""))


def describe_proxy(proxy: ProxyConfig | None) -> str:
    if not proxy or not proxy.enabled:
        return "disabled"
    if proxy.kind == "generic":
        if proxy.urls:
            return f"generic pool ({len(proxy.urls)} URL{'s' if len(proxy.urls) != 1 else ''})"
        return f"generic ({redact_proxy_url(proxy.https or proxy.http)})"
    if proxy.webshare_username and proxy.webshare_password:
        locations = f", locations={','.join(proxy.webshare_locations)}" if proxy.webshare_locations else ""
        return f"webshare rotating residential ({proxy.webshare_domain}:{proxy.webshare_port}{locations})"
    return "webshare (missing credentials)"


def proxy_payment_required_message(
    proxy: ProxyConfig | None,
    *,
    operation: str = "request",
) -> str:
    provider = "Configured proxy"
    if proxy and proxy.kind == "webshare":
        provider = "Webshare proxy"
    return (
        f"{provider} returned 402 Payment Required during {operation}. "
        "Check the proxy account plan, traffic quota, target-site entitlement, and credentials; "
        "then retry or disable proxy use for this path."
    )


def redact_proxy_secrets(proxy: ProxyConfig | None, text: str, *, key: str | None = None) -> str:
    redacted = text
    if not proxy:
        return redacted
    candidates = [proxy.http, proxy.https, *proxy.urls, proxy_url_for_ytdlp(proxy, key=key)]
    for candidate in candidates:
        if candidate:
            redacted = redacted.replace(candidate, redact_proxy_url(candidate))
    for secret in (proxy.webshare_username, proxy.webshare_password):
        if secret:
            redacted = redacted.replace(secret, "***")
    return redacted


def _format_negative_returncode(returncode: int) -> str:
    signal_number = abs(returncode)
    try:
        signal_name = signal.Signals(signal_number).name
    except ValueError:
        signal_name = f"signal {signal_number}"
    return f"{signal_name} (return code {returncode})"


def format_ytdlp_failure(
    result: subprocess.CompletedProcess[str],
    *,
    operation: str,
    proxy: ProxyConfig | None = None,
    proxy_key: str | None = None,
) -> str:
    detail = (result.stderr or "").strip() or (result.stdout or "").strip()
    redacted_detail = redact_proxy_secrets(proxy, detail, key=proxy_key) if detail else ""
    if is_proxy_payment_error(detail):
        message = proxy_payment_required_message(proxy, operation=f"yt-dlp {operation}")
        if result.returncode < 0:
            message = f"{message} yt-dlp also aborted with {_format_negative_returncode(result.returncode)}."
        return message
    if result.returncode < 0:
        message = f"yt-dlp subprocess aborted with {_format_negative_returncode(result.returncode)} during {operation}."
        if proxy_url_for_ytdlp(proxy, key=proxy_key):
            message = (
                f"{message} A proxy was configured for this yt-dlp request; run `yutome proxy-test` "
                "and check proxy quota/credentials before retrying."
            )
        if redacted_detail:
            message = f"{message} Last output: {redacted_detail}"
        return message
    if redacted_detail:
        return redacted_detail
    return f"exit code {result.returncode} with no stderr/stdout"


def _run_ytdlp(
    args: Iterable[str],
    *,
    cwd: Path,
    proxy: ProxyConfig | None = None,
    ytdlp_config: YtDlpConfig | None = None,
    proxy_key: str | None = None,
    hosted_context: ProviderCallContext | None = None,
    profile: YtDlpProfile | None = None,
) -> subprocess.CompletedProcess[str]:
    def run() -> subprocess.CompletedProcess[str]:
        result = _run_ytdlp_unwrapped(
            args,
            cwd=cwd,
            proxy=proxy,
            ytdlp_config=ytdlp_config,
            proxy_key=proxy_key,
            profile=profile,
        )
        if result.returncode != 0 and _uses_hosted_webshare_proxy(proxy, hosted_context):
            status_code = _webshare_proxy_failure_status_code(result)
            if status_code is not None:
                raise _HostedWebshareProxyFailure(
                    result,
                    status_code=status_code,
                    message=format_ytdlp_failure(
                        result,
                        operation="proxy request",
                        proxy=proxy,
                        proxy_key=proxy_key,
                    ),
                )
        return result

    if _uses_hosted_webshare_proxy(proxy, hosted_context):
        try:
            return _execute_hosted_webshare_proxy_call(
                proxy,
                hosted_context,
                run,
                target=proxy_key,
                source="yt-dlp",
            )
        except _HostedWebshareProxyFailure as exc:
            return exc.result

    return run()


def _run_ytdlp_for_operation(
    args: Iterable[str],
    *,
    cwd: Path,
    operation: str,
    proxy: ProxyConfig | None = None,
    ytdlp_config: YtDlpConfig | None = None,
    proxy_key: str | None = None,
    hosted_context: ProviderCallContext | None = None,
    success_validator: Callable[[subprocess.CompletedProcess[str]], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    arg_list = list(args)
    last_result: subprocess.CompletedProcess[str] | None = None
    last_validation_error: _YtDlpValidationError | None = None
    terminal_failure = False
    attempts_per_profile = _yt_dlp_attempts_per_profile(proxy, ytdlp_config)
    for profile in _yt_dlp_profile_sequence(ytdlp_config):
        for attempt in range(1, attempts_per_profile + 1):
            result = _run_ytdlp(
                arg_list,
                cwd=cwd,
                proxy=proxy,
                ytdlp_config=ytdlp_config,
                proxy_key=proxy_key,
                hosted_context=hosted_context,
                profile=profile,
            )
            if result.returncode != 0:
                last_result = result
                if is_proxy_payment_error((result.stderr or "") + "\n" + (result.stdout or "")):
                    terminal_failure = True
                    break
                if attempt < attempts_per_profile and _is_ytdlp_retryable_error(
                    (result.stderr or "") + "\n" + (result.stdout or "")
                ):
                    _sleep_before_ytdlp_retry(attempt)
                    continue
                break
            if success_validator is None:
                return result
            try:
                success_validator(result)
            except _YtDlpValidationError as exc:
                last_validation_error = exc
                if exc.retryable and attempt < attempts_per_profile:
                    _sleep_before_ytdlp_retry(attempt)
                    continue
                break
            return result
        if terminal_failure:
            break
    if last_validation_error is not None:
        raise RuntimeError(str(last_validation_error))
    if last_result is not None:
        raise RuntimeError(
            format_ytdlp_failure(
                last_result,
                operation=operation,
                proxy=proxy,
                proxy_key=proxy_key,
            )
        )
    raise RuntimeError(f"yt-dlp did not run for {operation}")


def _run_ytdlp_unwrapped(
    args: Iterable[str],
    *,
    cwd: Path,
    proxy: ProxyConfig | None = None,
    ytdlp_config: YtDlpConfig | None = None,
    proxy_key: str | None = None,
    profile: YtDlpProfile | None = None,
) -> subprocess.CompletedProcess[str]:
    arg_list = list(args)
    config = _effective_ytdlp_config(ytdlp_config)
    selected_profile = profile or config.profile
    extra_args: list[str] = []
    proxy_url = proxy_url_for_ytdlp(proxy, key=proxy_key)
    if proxy_url:
        extra_args.extend(["--proxy", proxy_url])
    sleep_requests = config.sleep_requests_seconds_with_proxy if proxy_url else config.sleep_requests_seconds
    extra_args.extend(["--sleep-requests", str(sleep_requests)])
    extra_args.extend(["--retry-sleep", config.retry_sleep])
    if config.impersonate:
        extra_args.extend(["--impersonate", config.impersonate])
    if config.remote_components:
        extra_args.extend(["--remote-components", "ejs:github"])
    extra_args.extend(_yt_dlp_profile_args(selected_profile))
    command = [
        *_yt_dlp_base_command(),
        "--ignore-config",
        "--no-warnings",
        *extra_args,
        *arg_list,
    ]
    timeout_seconds = config.subprocess_timeout_seconds
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return _with_ytdlp_output_file_bytes(result, arg_list)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        timeout_message = f"yt-dlp timed out after {timeout_seconds:.1f}s"
        result = subprocess.CompletedProcess(
            command,
            124,
            stdout or "",
            f"{stderr or ''}\n{timeout_message}".strip(),
        )
        return _with_ytdlp_output_file_bytes(result, arg_list)


def _with_ytdlp_output_file_bytes(
    result: subprocess.CompletedProcess[str],
    args: list[str],
) -> subprocess.CompletedProcess[str]:
    paths_dir = _ytdlp_paths_dir(args)
    if paths_dir is None:
        return result
    try:
        output_bytes = sum(path.stat().st_size for path in paths_dir.rglob("*") if path.is_file())
    except OSError:
        output_bytes = 0
    result.yutome_output_file_bytes = output_bytes
    return result


def _ytdlp_paths_dir(args: list[str]) -> Path | None:
    for index, arg in enumerate(args):
        if arg == "--paths" and index + 1 < len(args):
            return Path(args[index + 1])
    return None


def _parse_json_lines(stdout: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        rows.append(json.loads(stripped))
    return rows


def discover_videos(
    *,
    target: str,
    cwd: Path,
    limit: int | None = None,
    proxy: ProxyConfig | None = None,
    ytdlp_config: YtDlpConfig | None = None,
    hosted_context: ProviderCallContext | None = None,
) -> list[DiscoveredVideo]:
    discovered: dict[str, DiscoveredVideo] = {}
    for tab_name, tab_url in canonical_channel_tabs(target):
        args = [
            "--flat-playlist",
            "--dump-json",
            # This keeps discovery cheap while filling timestamp/upload_date from
            # listing text such as "2 weeks ago". Full metadata can later
            # overwrite it with an exact video-page upload date.
            "--extractor-args",
            "youtubetab:approximate_date=1",
        ]
        if limit:
            args.extend(["--playlist-end", str(limit)])
        args.append(tab_url)
        try:
            result = _run_ytdlp_for_operation(
                args,
                cwd=cwd,
                operation=f"discovery for {tab_url}",
                proxy=proxy,
                ytdlp_config=ytdlp_config,
                proxy_key=tab_url,
                hosted_context=hosted_context,
            )
        except RuntimeError:
            if tab_name == "streams":
                continue
            raise
        for raw in _parse_json_lines(result.stdout):
            video_id = raw.get("id")
            if not video_id or video_id in discovered:
                continue
            channel_id = raw.get("playlist_channel_id") or raw.get("channel_id")
            url = raw.get("webpage_url") or raw.get("url") or f"https://www.youtube.com/watch?v={video_id}"
            if not str(url).startswith("http"):
                url = f"https://www.youtube.com/watch?v={video_id}"
            discovered[video_id] = DiscoveredVideo(
                video_id=video_id,
                title=raw.get("title"),
                url=str(url),
                channel_id=channel_id,
                channel_title=raw.get("playlist_channel") or raw.get("channel"),
                channel_handle=raw.get("playlist_uploader_id") or raw.get("uploader_id"),
                duration_seconds=int(raw["duration"]) if raw.get("duration") is not None else None,
                playlist_tab=tab_name,
                raw=raw,
            )
    return list(discovered.values())


def _validate_video_metadata_row(row: dict[str, Any], *, video_id: str) -> None:
    """Reject yt-dlp output that is a flat stub rather than a full video extraction.

    `_type='url'` (and `'url_transparent'`) come from `extract_flat`-style listings.
    A real `--dump-json` on a watch URL should produce `_type='video'` (or omit
    `_type` entirely on some yt-dlp versions). Accepting a stub silently is how
    `published_at` rows end up NULL forever.
    """
    row_type = row.get("_type")
    if row_type not in (None, "video"):
        raise RuntimeError(
            f"yt-dlp returned a '{row_type}' stub for {video_id} instead of a full extraction. "
            f"This usually means YouTube returned a soft block or the extractor degraded; retry later."
        )
    if not row.get("id") and not row.get("title"):
        raise RuntimeError(
            f"yt-dlp returned a metadata row with no id or title for {video_id}; "
            f"treating as a failed extraction."
        )


def _metadata_has_published_date(row: dict[str, Any]) -> bool:
    return any(row.get(field) is not None for field in ("upload_date", "release_date", "modified_date", "timestamp"))


def _validate_video_metadata_result(result: subprocess.CompletedProcess[str], *, video_id: str) -> None:
    rows = _parse_json_lines(result.stdout)
    if not rows:
        raise _YtDlpValidationError(f"yt-dlp returned no metadata for {video_id}")
    row = rows[0]
    try:
        _validate_video_metadata_row(row, video_id=video_id)
    except RuntimeError as exc:
        raise _YtDlpValidationError(str(exc)) from exc
    if not _metadata_has_published_date(row):
        raise _YtDlpValidationError(
            f"yt-dlp returned metadata without a published date for {video_id}; "
            "retrying with the next configured attempt/profile."
        )


def fetch_video_metadata(
    *,
    video_id: str,
    cwd: Path,
    proxy: ProxyConfig | None = None,
    ytdlp_config: YtDlpConfig | None = None,
    hosted_context: ProviderCallContext | None = None,
) -> dict[str, Any]:
    result = _run_ytdlp_for_operation(
        [
            "--skip-download",
            "--dump-json",
            f"https://www.youtube.com/watch?v={video_id}",
        ],
        cwd=cwd,
        operation=f"metadata fetch for {video_id}",
        proxy=proxy,
        ytdlp_config=ytdlp_config,
        proxy_key=video_id,
        hosted_context=hosted_context,
        success_validator=lambda result: _validate_video_metadata_result(result, video_id=video_id),
    )
    rows = _parse_json_lines(result.stdout)
    row = rows[0]
    return row


def discover_video(
    *,
    target: str,
    cwd: Path,
    proxy: ProxyConfig | None = None,
    ytdlp_config: YtDlpConfig | None = None,
    hosted_context: ProviderCallContext | None = None,
) -> DiscoveredVideo:
    video_id = extract_video_id(target)
    if video_id is None:
        raise ValueError(f"not a YouTube video URL or id: {target}")
    metadata = fetch_video_metadata(
        video_id=video_id,
        cwd=cwd,
        proxy=proxy,
        ytdlp_config=ytdlp_config,
        hosted_context=hosted_context,
    )
    channel_handle = metadata.get("uploader_id") or metadata.get("channel")
    if isinstance(channel_handle, str) and channel_handle.startswith("@"):
        channel_handle = channel_handle
    return DiscoveredVideo(
        video_id=video_id,
        title=metadata.get("title"),
        url=metadata.get("webpage_url") or canonical_video_url(video_id),
        channel_id=metadata.get("channel_id"),
        channel_title=metadata.get("channel") or metadata.get("uploader"),
        channel_handle=channel_handle,
        duration_seconds=int(metadata["duration"]) if metadata.get("duration") is not None else None,
        playlist_tab="video",
        raw=metadata,
    )


def fetch_transcript(
    *,
    video_id: str,
    languages: Iterable[str],
    proxy: ProxyConfig | None = None,
    timeout_seconds: float | None = None,
    hosted_context: ProviderCallContext | None = None,
) -> TranscriptFetchResult:
    def call() -> TranscriptFetchResult:
        api = YouTubeTranscriptApi(
            proxy_config=_transcript_proxy_config(proxy, video_id=video_id),
            http_client=_transcript_http_client(timeout_seconds),
        )
        transcript = api.fetch(video_id, languages=languages, preserve_formatting=False)
        return TranscriptFetchResult(
            raw_snippets=transcript.to_raw_data(),
            source="youtube-transcript-api",
            language=transcript.language_code,
            is_generated=bool(transcript.is_generated),
        )

    if _uses_hosted_webshare_proxy(proxy, hosted_context):
        return _execute_hosted_webshare_proxy_call(
            proxy,
            hosted_context,
            call,
            target=video_id,
            source="youtube-transcript-api.fetch",
        )
    return call()


def _transcript_proxy_config(proxy: ProxyConfig | None, *, video_id: str):
    if not proxy or not proxy.enabled:
        return None
    if proxy.kind == "generic":
        pooled_url = _pick_from_pool(proxy.urls, key=video_id)
        return GenericProxyConfig(
            http_url=proxy.http or pooled_url,
            https_url=proxy.https or pooled_url,
        )
    if proxy.kind == "webshare" and proxy.webshare_username and proxy.webshare_password:
        return WebshareProxyConfig(
            proxy_username=proxy.webshare_username,
            proxy_password=proxy.webshare_password,
            filter_ip_locations=proxy.webshare_locations or None,
            retries_when_blocked=proxy.webshare_retries_when_blocked,
            domain_name=proxy.webshare_domain,
            proxy_port=proxy.webshare_port,
        )
    return None


def _transcript_http_client(timeout_seconds: float | None) -> Session | None:
    if timeout_seconds is None:
        return None
    return TimeoutSession(timeout_seconds=timeout_seconds)


def list_available_transcripts(
    *,
    video_id: str,
    proxy: ProxyConfig | None = None,
    timeout_seconds: float | None = None,
    hosted_context: ProviderCallContext | None = None,
) -> list[AvailableTranscript]:
    def call() -> list[AvailableTranscript]:
        api = YouTubeTranscriptApi(
            proxy_config=_transcript_proxy_config(proxy, video_id=video_id),
            http_client=_transcript_http_client(timeout_seconds),
        )
        return [
            AvailableTranscript(
                language_code=transcript.language_code,
                language=transcript.language,
                is_generated=bool(transcript.is_generated),
                is_translatable=bool(transcript.is_translatable),
            )
            for transcript in api.list(video_id)
        ]

    if _uses_hosted_webshare_proxy(proxy, hosted_context):
        return _execute_hosted_webshare_proxy_call(
            proxy,
            hosted_context,
            call,
            target=video_id,
            source="youtube-transcript-api.list",
        )
    return call()


def non_preferred_generated_transcripts(
    *,
    video_id: str,
    preferred_languages: Iterable[str],
    proxy: ProxyConfig | None = None,
    timeout_seconds: float | None = None,
    hosted_context: ProviderCallContext | None = None,
) -> list[AvailableTranscript]:
    preferred = set(preferred_languages)
    return [
        transcript
        for transcript in list_available_transcripts(
            video_id=video_id,
            proxy=proxy,
            timeout_seconds=timeout_seconds,
            hosted_context=hosted_context,
        )
        if transcript.is_generated and transcript.language_code not in preferred
    ]


def fetch_subtitle_transcript_with_ytdlp(
    *,
    video_id: str,
    cwd: Path,
    language: str = "en",
    proxy: ProxyConfig | None = None,
    ytdlp_config: YtDlpConfig | None = None,
    allow_translated_captions: bool = False,
    hosted_context: ProviderCallContext | None = None,
) -> TranscriptFetchResult:
    language_candidates = [language]
    if language == "en":
        # YouTube/yt-dlp may expose native English captions as either
        # plain `en` or `en-orig`. Try both before declaring English
        # unavailable; translated-caption policy is handled by upstream
        # provider ordering and this fallback remains English-only.
        language_candidates = ["en", "en-orig"]
    last_error: str | None = None
    attempts_per_profile = _yt_dlp_attempts_per_profile(proxy, ytdlp_config)
    for profile in _yt_dlp_profile_sequence(ytdlp_config):
        profile_failed_from_process = False
        for candidate in language_candidates:
            for attempt in range(1, attempts_per_profile + 1):
                try:
                    return _fetch_subtitle_transcript_with_ytdlp_language(
                        video_id=video_id,
                        cwd=cwd,
                        language=candidate,
                        proxy=proxy,
                        ytdlp_config=ytdlp_config,
                        hosted_context=hosted_context,
                        profile=profile,
                    )
                except UsageReservationDenied:
                    raise
                except RuntimeError as exc:
                    last_error = str(exc)
                    if not _is_ytdlp_retryable_error(last_error):
                        if not _is_ytdlp_empty_subtitle_error(last_error):
                            profile_failed_from_process = True
                        break
                    if attempt < attempts_per_profile:
                        _sleep_before_ytdlp_retry(attempt)
                        continue
                    profile_failed_from_process = True
                    break
            if profile_failed_from_process:
                break
        if profile_failed_from_process:
            continue
    raise RuntimeError(last_error or f"yt-dlp did not write subtitles for {video_id}")


def _json3_text_snippets(payload: dict[str, Any]) -> list[dict[str, Any]]:
    snippets: list[dict[str, Any]] = []
    for event in payload.get("events", []):
        if "segs" not in event:
            continue
        text = "".join(seg.get("utf8", "") for seg in event["segs"]).strip()
        if not text:
            continue
        start_ms = int(event.get("tStartMs", 0) or 0)
        duration_ms = int(event.get("dDurationMs", 0) or 0)
        snippets.append(
            {
                "text": text,
                "start": start_ms / 1000,
                "duration": duration_ms / 1000,
            }
        )
    return snippets


def _is_ytdlp_empty_subtitle_error(error: Exception | str) -> bool:
    text = str(error).lower()
    return "did not write json3 subtitles" in text or "json3 subtitles with no text segments" in text


def _fetch_subtitle_transcript_with_ytdlp_language(
    *,
    video_id: str,
    cwd: Path,
    language: str,
    proxy: ProxyConfig | None,
    ytdlp_config: YtDlpConfig | None,
    hosted_context: ProviderCallContext | None,
    profile: YtDlpProfile | None = None,
) -> TranscriptFetchResult:
    with tempfile.TemporaryDirectory(prefix="yutome-subs-") as temp_dir:
        # Mirror the proxy-aware sleep policy from _run_ytdlp: per-IP rate
        # limits don't apply when a rotating proxy hands out fresh IPs.
        config = _effective_ytdlp_config(ytdlp_config)
        using_proxy = bool(proxy_url_for_ytdlp(proxy, key=video_id))
        sleep_subs = config.sleep_subtitles_seconds_with_proxy if using_proxy else config.sleep_subtitles_seconds
        result = _run_ytdlp(
            [
                "--skip-download",
                "--write-auto-subs",
                "--write-subs",
                "--sub-langs",
                language,
                "--sub-format",
                "json3",
                "--sleep-subtitles",
                str(sleep_subs),
                "--paths",
                temp_dir,
                "-o",
                "%(id)s.%(ext)s",
                f"https://www.youtube.com/watch?v={video_id}",
            ],
            cwd=cwd,
            proxy=proxy,
            ytdlp_config=ytdlp_config,
            proxy_key=video_id,
            hosted_context=hosted_context,
            profile=profile,
        )
        if result.returncode != 0:
            raise RuntimeError(
                format_ytdlp_failure(
                    result,
                    operation=f"subtitle fetch for {video_id}",
                    proxy=proxy,
                    proxy_key=video_id,
                )
            )
        files = sorted(Path(temp_dir).glob(f"{video_id}*.json3"))
        if not files:
            raise RuntimeError(f"yt-dlp did not write json3 subtitles for {video_id}")
        payload = json.loads(files[0].read_text(encoding="utf-8"))
    snippets = _json3_text_snippets(payload)
    if not snippets:
        raise RuntimeError(f"yt-dlp wrote json3 subtitles with no text segments for {video_id}")
    return TranscriptFetchResult(
        raw_snippets=snippets,
        source=f"yt-dlp-json3:{language}",
        language=language,
        is_generated=True,
    )


def download_audio_for_asr(
    *,
    video_id: str,
    cwd: Path,
    output_dir: Path,
    proxy: ProxyConfig | None = None,
    ytdlp_config: YtDlpConfig | None = None,
    hosted_context: ProviderCallContext | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    def validate_audio_output(_: subprocess.CompletedProcess[str]) -> None:
        if not any(path.is_file() and path.name.startswith(video_id) for path in output_dir.iterdir()):
            raise _YtDlpValidationError(f"yt-dlp did not write audio for {video_id}")

    _run_ytdlp_for_operation(
        [
            "-f",
            "bestaudio/best",
            "--paths",
            str(output_dir),
            "-o",
            "%(id)s.%(ext)s",
            f"https://www.youtube.com/watch?v={video_id}",
        ],
        cwd=cwd,
        operation=f"audio download for {video_id}",
        proxy=proxy,
        ytdlp_config=ytdlp_config,
        proxy_key=video_id,
        hosted_context=hosted_context,
        success_validator=validate_audio_output,
    )
    candidates = [path for path in output_dir.iterdir() if path.is_file() and path.name.startswith(video_id)]
    return max(candidates, key=lambda path: path.stat().st_size)
