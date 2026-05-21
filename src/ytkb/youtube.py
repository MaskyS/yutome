from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlsplit, urlunsplit

from requests import Session
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig, WebshareProxyConfig

from ytkb.config import ProxyConfig, YtDlpConfig


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


def is_youtube_block_error(error: Exception | str) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in YOUTUBE_BLOCK_MARKERS)


_is_ytdlp_block_error = is_youtube_block_error


def canonical_channel_tabs(target: str) -> list[tuple[str, str]]:
    cleaned = target.rstrip("/")
    if cleaned.endswith("/videos") or cleaned.endswith("/streams"):
        return [(cleaned.rsplit("/", 1)[-1], cleaned)]
    return [("videos", f"{cleaned}/videos"), ("streams", f"{cleaned}/streams")]


def _yt_dlp_base_command() -> list[str]:
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        return ["yt-dlp"]
    return [sys.executable, "-m", "yt_dlp"]


def _pick_from_pool(urls: list[str], *, key: str | None = None) -> str | None:
    if not urls:
        return None
    if key is None:
        return urls[0]
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return urls[int(digest[:12], 16) % len(urls)]


def proxy_url_for_ytdlp(proxy: ProxyConfig | None, *, key: str | None = None) -> str | None:
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


def _run_ytdlp(
    args: Iterable[str],
    *,
    cwd: Path,
    proxy: ProxyConfig | None = None,
    ytdlp_config: YtDlpConfig | None = None,
    proxy_key: str | None = None,
) -> subprocess.CompletedProcess[str]:
    extra_args: list[str] = []
    proxy_url = proxy_url_for_ytdlp(proxy, key=proxy_key)
    if proxy_url:
        extra_args.extend(["--proxy", proxy_url])
    if ytdlp_config:
        sleep_requests = (
            ytdlp_config.sleep_requests_seconds_with_proxy
            if proxy_url
            else ytdlp_config.sleep_requests_seconds
        )
        extra_args.extend(["--sleep-requests", str(sleep_requests)])
        extra_args.extend(["--retry-sleep", ytdlp_config.retry_sleep])
        if ytdlp_config.impersonate:
            extra_args.extend(["--impersonate", ytdlp_config.impersonate])
        if ytdlp_config.remote_components:
            extra_args.extend(["--remote-components", "ejs:github"])
    command = [
        *_yt_dlp_base_command(),
        "--ignore-config",
        "--no-warnings",
        *extra_args,
        *args,
    ]
    timeout_seconds = ytdlp_config.subprocess_timeout_seconds if ytdlp_config else 300.0
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        timeout_message = f"yt-dlp timed out after {timeout_seconds:.1f}s"
        return subprocess.CompletedProcess(
            command,
            124,
            stdout or "",
            f"{stderr or ''}\n{timeout_message}".strip(),
        )


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
        result = _run_ytdlp(
            args,
            cwd=cwd,
            proxy=proxy,
            ytdlp_config=ytdlp_config,
            proxy_key=tab_url,
        )
        if result.returncode != 0:
            if tab_name == "streams":
                continue
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
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


def fetch_video_metadata(
    *,
    video_id: str,
    cwd: Path,
    proxy: ProxyConfig | None = None,
    ytdlp_config: YtDlpConfig | None = None,
) -> dict[str, Any]:
    # Tempting optimisation we tried and reverted: `--extractor-args youtube:player_skip=js`
    # skips the player-JS download and signature decryption that yt-dlp does even
    # for metadata. On bare IP it cut wall time ~41% (11.5s → 6.8s) with all fields
    # intact. Through Webshare rotating residential, the same flag only bought ~8%
    # because proxy hops dominate wall time, AND it introduced sporadic
    # "page needs to be reloaded" failures — yt-dlp can't reconcile when JS state
    # is needed and a rotated IP returns a player response shaped for a different
    # session. Other player_skip values (configs / initial_data / webpage alone)
    # gave no measurable improvement. The `js,webpage` combination breaks
    # extraction unconditionally with the same "page needs to be reloaded" error.
    # If we ever support a no-proxy fast path, apply `player_skip=js` only when
    # `proxy_url is None`.
    result = _run_ytdlp(
        [
            "--skip-download",
            "--dump-json",
            f"https://www.youtube.com/watch?v={video_id}",
        ],
        cwd=cwd,
        proxy=proxy,
        ytdlp_config=ytdlp_config,
        proxy_key=video_id,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        if not detail:
            detail = f"exit code {result.returncode} with no stderr/stdout"
        raise RuntimeError(f"yt-dlp metadata fetch failed for {video_id}: {detail}")
    rows = _parse_json_lines(result.stdout)
    if not rows:
        raise RuntimeError(f"yt-dlp returned no metadata for {video_id}")
    row = rows[0]
    _validate_video_metadata_row(row, video_id=video_id)
    return row


def fetch_transcript(
    *,
    video_id: str,
    languages: Iterable[str],
    proxy: ProxyConfig | None = None,
    timeout_seconds: float | None = None,
) -> TranscriptFetchResult:
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
) -> list[AvailableTranscript]:
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


def non_preferred_generated_transcripts(
    *,
    video_id: str,
    preferred_languages: Iterable[str],
    proxy: ProxyConfig | None = None,
    timeout_seconds: float | None = None,
) -> list[AvailableTranscript]:
    preferred = set(preferred_languages)
    return [
        transcript
        for transcript in list_available_transcripts(
            video_id=video_id,
            proxy=proxy,
            timeout_seconds=timeout_seconds,
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
) -> TranscriptFetchResult:
    language_candidates = [language]
    if language == "en":
        language_candidates = ["en-orig", "en"] if allow_translated_captions else ["en-orig"]
    last_error: str | None = None
    for candidate in language_candidates:
        attempts = 1
        if ytdlp_config and proxy and proxy.enabled:
            attempts += ytdlp_config.subtitle_retries_when_blocked
        for attempt in range(1, attempts + 1):
            try:
                return _fetch_subtitle_transcript_with_ytdlp_language(
                    video_id=video_id,
                    cwd=cwd,
                    language=candidate,
                    proxy=proxy,
                    ytdlp_config=ytdlp_config,
                )
            except RuntimeError as exc:
                last_error = str(exc)
                if not is_youtube_block_error(last_error):
                    break
                if attempt >= attempts:
                    raise
                time.sleep(min(5 * attempt, 15))
    raise RuntimeError(last_error or f"yt-dlp did not write subtitles for {video_id}")


def _fetch_subtitle_transcript_with_ytdlp_language(
    *,
    video_id: str,
    cwd: Path,
    language: str,
    proxy: ProxyConfig | None,
    ytdlp_config: YtDlpConfig | None,
) -> TranscriptFetchResult:
    with tempfile.TemporaryDirectory(prefix="ytkb-subs-") as temp_dir:
        # Mirror the proxy-aware sleep policy from _run_ytdlp: per-IP rate
        # limits don't apply when a rotating proxy hands out fresh IPs.
        using_proxy = bool(proxy_url_for_ytdlp(proxy, key=video_id))
        if ytdlp_config:
            sleep_subs = (
                ytdlp_config.sleep_subtitles_seconds_with_proxy
                if using_proxy
                else ytdlp_config.sleep_subtitles_seconds
            )
        else:
            sleep_subs = 0.0 if using_proxy else 8.0
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
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        files = sorted(Path(temp_dir).glob(f"{video_id}*.json3"))
        if not files:
            raise RuntimeError(f"yt-dlp did not write json3 subtitles for {video_id}")
        payload = json.loads(files[0].read_text(encoding="utf-8"))
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
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    result = _run_ytdlp(
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
        proxy=proxy,
        ytdlp_config=ytdlp_config,
        proxy_key=video_id,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    candidates = [path for path in output_dir.iterdir() if path.is_file() and path.name.startswith(video_id)]
    if not candidates:
        raise RuntimeError(f"yt-dlp did not write audio for {video_id}")
    return max(candidates, key=lambda path: path.stat().st_size)
