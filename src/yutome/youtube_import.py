from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from http.cookiejar import CookieJar
from typing import Any

from yutome.channels import LibraryChannel, channel_from_input
from yutome.youtube_oauth import SUBSCRIPTIONS_URI


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
YOUTUBE_CHANNELS_FEED_URL = "https://www.youtube.com/feed/channels"
YOUTUBE_CHANNELS_API_URL = "https://www.googleapis.com/youtube/v3/channels"


class YouTubeImportError(RuntimeError):
    """Raised when a YouTube subscription import source cannot be used."""


def fetch_user_subscription_channels_from_browser(
    *,
    browsers: Iterable[str],
    status_callback: Callable[[str], None] | None = None,
) -> list[LibraryChannel]:
    """Fetch the signed-in user's subscriptions using local browser cookies."""
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError as exc:  # pragma: no cover - exercised by CLI dependency checks.
        raise YouTubeImportError("yt-dlp is not installed; run `uv sync --extra ingest`") from exc

    failures: list[str] = []
    for browser in browsers:
        browser = browser.strip()
        if not browser:
            continue
        try:
            cookiejar = extract_cookies_from_browser(browser)
            channels = fetch_user_subscription_channels_with_cookies(cookiejar)
        except Exception as exc:  # noqa: BLE001 - browser stores fail in environment-specific ways.
            failures.append(f"{browser}: {exc}")
            continue
        if channels:
            if status_callback:
                status_callback(f"Imported subscriptions from {browser} browser cookies.")
            return channels
        failures.append(f"{browser}: no subscriptions found")
    detail = "; ".join(failures) if failures else "no browsers configured"
    raise YouTubeImportError(f"Could not import subscriptions from browser cookies ({detail}).")


def fetch_user_subscription_channels_with_cookies(cookiejar: CookieJar) -> list[LibraryChannel]:
    html = _read_url(YOUTUBE_CHANNELS_FEED_URL, cookiejar=cookiejar)
    data = _extract_yt_initial_data(html)
    channels = _channels_from_initial_data(data, import_source="youtube-browser-cookies")
    if not channels:
        raise YouTubeImportError("YouTube channels feed did not include subscription channels.")
    return channels


def fetch_public_subscription_channels_from_api(target: str, *, api_key: str) -> list[LibraryChannel]:
    channel_id = _resolve_channel_id_for_api(target, api_key=api_key)
    channels: list[LibraryChannel] = []
    page_token: str | None = None
    while True:
        params = {
            "part": "snippet",
            "channelId": channel_id,
            "maxResults": "50",
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token
        payload = _read_json(f"{SUBSCRIPTIONS_URI}?{urllib.parse.urlencode(params)}")
        for item in payload.get("items", []):
            snippet = item.get("snippet") or {}
            resource = snippet.get("resourceId") or {}
            subscribed_channel_id = resource.get("channelId")
            if not subscribed_channel_id:
                continue
            channel = channel_from_input(
                subscribed_channel_id,
                title=snippet.get("title"),
                import_source="youtube-public-api",
            )
            if channel is not None:
                channels.append(channel)
        page_token = payload.get("nextPageToken")
        if not page_token:
            return _dedupe_channels(channels)


def fetch_public_subscription_channels_from_scrape(target: str) -> list[LibraryChannel]:
    url = _channels_tab_url(target)
    html = _read_url(url)
    data = _extract_yt_initial_data(html)
    channels = _channels_from_initial_data(data, import_source="youtube-public-scrape")
    if not channels:
        raise YouTubeImportError(
            "No public subscription channels were found. The channel may keep subscriptions private."
        )
    return channels


def _resolve_channel_id_for_api(target: str, *, api_key: str) -> str:
    channel = channel_from_input(target)
    if channel and channel.channel_id:
        return channel.channel_id
    if channel and channel.handle:
        params = {
            "part": "id",
            "forHandle": channel.handle.lstrip("@"),
            "key": api_key,
        }
        payload = _read_json(f"{YOUTUBE_CHANNELS_API_URL}?{urllib.parse.urlencode(params)}")
        items = payload.get("items") or []
        if items and items[0].get("id"):
            return str(items[0]["id"])
    raise YouTubeImportError("Public subscription API import needs a channel id or @handle target.")


def _channels_tab_url(target: str) -> str:
    channel = channel_from_input(target)
    if channel is None:
        raise YouTubeImportError("YouTube channel target is empty.")
    base = channel.source_url.rstrip("/")
    if base.endswith("/channels"):
        return base
    return f"{base}/channels"


def _read_json(url: str) -> dict[str, Any]:
    try:
        payload = _read_url(url)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        message = _youtube_error_message(body) or exc.reason
        if exc.code in {401, 403}:
            raise YouTubeImportError(
                f"YouTube API rejected the subscription request: {message}. "
                "The channel may keep subscriptions private, or the API key may be missing access."
            ) from exc
        raise YouTubeImportError(f"YouTube API request failed: HTTP {exc.code} {message}") from exc
    return json.loads(payload)


def _read_url(url: str, *, cookiejar: CookieJar | None = None) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": USER_AGENT,
        },
    )
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookiejar)) if cookiejar else None
    if opener:
        with opener.open(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def _youtube_error_message(body: str) -> str | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return None
    error = payload.get("error") or {}
    if message := error.get("message"):
        return str(message)
    errors = error.get("errors") or []
    if errors and errors[0].get("reason"):
        return str(errors[0]["reason"])
    return None


def _extract_yt_initial_data(html: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    marker = "ytInitialData"
    start = html.find(marker)
    while start != -1:
        brace = html.find("{", start)
        if brace == -1:
            break
        try:
            data, _ = decoder.raw_decode(html[brace:])
        except json.JSONDecodeError:
            start = html.find(marker, start + len(marker))
            continue
        if isinstance(data, dict):
            return data
        break
    raise YouTubeImportError("Could not parse YouTube page data.")


def _channels_from_initial_data(data: dict[str, Any], *, import_source: str) -> list[LibraryChannel]:
    channels: list[LibraryChannel] = []
    for container in _iter_dicts(data):
        renderer = container.get("channelRenderer") or container.get("gridChannelRenderer")
        if not isinstance(renderer, dict):
            continue
        channel = _channel_from_renderer(renderer, import_source=import_source)
        if channel is not None:
            channels.append(channel)
    return _dedupe_channels(channels)


def _channel_from_renderer(renderer: dict[str, Any], *, import_source: str) -> LibraryChannel | None:
    channel_id = renderer.get("channelId") or _browse_id(renderer)
    title = _text(renderer.get("title")) or _text(renderer.get("shortBylineText"))
    url = _renderer_url(renderer)
    value = str(channel_id or url or "").strip()
    if not value:
        return None
    return channel_from_input(value, title=title, import_source=import_source)


def _browse_id(value: dict[str, Any]) -> str | None:
    for container in _iter_dicts(value):
        browse_endpoint = container.get("browseEndpoint")
        if isinstance(browse_endpoint, dict) and browse_endpoint.get("browseId"):
            browse_id = str(browse_endpoint["browseId"])
            if browse_id.startswith("UC"):
                return browse_id
    return None


def _renderer_url(renderer: dict[str, Any]) -> str | None:
    for container in _iter_dicts(renderer):
        command = container.get("webCommandMetadata")
        if isinstance(command, dict) and command.get("url"):
            return f"https://www.youtube.com{command['url']}"
        browse = container.get("browseEndpoint")
        if isinstance(browse, dict) and browse.get("canonicalBaseUrl"):
            return f"https://www.youtube.com{browse['canonicalBaseUrl']}"
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    if simple := value.get("simpleText"):
        return str(simple).strip() or None
    runs = value.get("runs")
    if isinstance(runs, list):
        rendered = "".join(str(run.get("text", "")) for run in runs if isinstance(run, dict)).strip()
        return rendered or None
    return None


def _iter_dicts(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _iter_dicts(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_dicts(item)


def _dedupe_channels(channels: Iterable[LibraryChannel]) -> list[LibraryChannel]:
    seen: set[str] = set()
    deduped: list[LibraryChannel] = []
    for channel in channels:
        key = channel.channel_id or channel.source_url
        if key in seen:
            continue
        seen.add(key)
        deduped.append(channel)
    return deduped
