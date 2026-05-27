from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def apply_env_to_config(config):
    proxy_updates = {}
    explicit_use_for_metadata = os.environ.get("YUTOME_PROXY_USE_FOR_METADATA")
    if proxy_urls := os.environ.get("YUTOME_PROXY_URLS"):
        proxy_updates["urls"] = [url.strip() for url in proxy_urls.split(",") if url.strip()]
        proxy_updates["enabled"] = True
        proxy_updates["kind"] = "generic"
    if http_proxy := os.environ.get("YUTOME_HTTP_PROXY"):
        proxy_updates["http"] = http_proxy
        proxy_updates["enabled"] = True
    if https_proxy := os.environ.get("YUTOME_HTTPS_PROXY"):
        proxy_updates["https"] = https_proxy
        proxy_updates["enabled"] = True
    if username := os.environ.get("YUTOME_WEBSHARE_USERNAME"):
        proxy_updates["webshare_username"] = username
        proxy_updates["enabled"] = True
        proxy_updates["kind"] = "webshare"
    if password := os.environ.get("YUTOME_WEBSHARE_PASSWORD"):
        proxy_updates["webshare_password"] = password
        proxy_updates["enabled"] = True
        proxy_updates["kind"] = "webshare"
    if domain := os.environ.get("YUTOME_WEBSHARE_DOMAIN"):
        proxy_updates["webshare_domain"] = domain
        proxy_updates["enabled"] = True
        proxy_updates["kind"] = "webshare"
    if port := os.environ.get("YUTOME_WEBSHARE_PORT"):
        proxy_updates["webshare_port"] = int(port)
        proxy_updates["enabled"] = True
        proxy_updates["kind"] = "webshare"
    if use_for_discovery := os.environ.get("YUTOME_PROXY_USE_FOR_DISCOVERY"):
        proxy_updates["use_for_discovery"] = _env_bool(use_for_discovery)
    if explicit_use_for_metadata is not None:
        proxy_updates["use_for_metadata"] = _env_bool(explicit_use_for_metadata)
    if use_for_asr_audio := os.environ.get("YUTOME_PROXY_USE_FOR_ASR_AUDIO"):
        proxy_updates["use_for_asr_audio"] = _env_bool(use_for_asr_audio)
    effective_proxy = config.proxy.model_copy(update=proxy_updates) if proxy_updates else config.proxy
    if (
        explicit_use_for_metadata is None
        and effective_proxy.kind == "webshare"
        and effective_proxy.webshare_username
        and effective_proxy.webshare_password
    ):
        proxy_updates["use_for_metadata"] = True

    gemini_updates = {}
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        gemini_updates["enabled"] = True
    if gemini_model := os.environ.get("YUTOME_GEMINI_MODEL"):
        gemini_updates["model"] = gemini_model
        gemini_updates["enabled"] = True
    if gemini_media_resolution := os.environ.get("YUTOME_GEMINI_MEDIA_RESOLUTION"):
        gemini_updates["media_resolution"] = gemini_media_resolution
        gemini_updates["enabled"] = True
    if gemini_request_timeout_seconds := os.environ.get("YUTOME_GEMINI_REQUEST_TIMEOUT_SECONDS"):
        gemini_updates["request_timeout_seconds"] = float(gemini_request_timeout_seconds)
        gemini_updates["enabled"] = True
    if gemini_cleanup_thinking_level := os.environ.get("YUTOME_GEMINI_CLEANUP_THINKING_LEVEL"):
        gemini_updates["cleanup_thinking_level"] = gemini_cleanup_thinking_level
        gemini_updates["enabled"] = True
    if gemini_cleanup_thinking_budget := os.environ.get("YUTOME_GEMINI_CLEANUP_THINKING_BUDGET"):
        gemini_updates["cleanup_thinking_budget"] = int(gemini_cleanup_thinking_budget)
        gemini_updates["enabled"] = True
    if gemini_window_seconds := os.environ.get("YUTOME_GEMINI_WINDOW_SECONDS"):
        gemini_updates["window_seconds"] = int(gemini_window_seconds)
        gemini_updates["enabled"] = True
    if gemini_cache_enabled := os.environ.get("YUTOME_GEMINI_CLEANUP_CACHE_ENABLED"):
        gemini_updates["cleanup_cache_enabled"] = _env_bool(gemini_cache_enabled)
    if gemini_cache_ttl := os.environ.get("YUTOME_GEMINI_CLEANUP_CACHE_TTL_SECONDS"):
        gemini_updates["cleanup_cache_ttl_seconds"] = int(gemini_cache_ttl)
    if gemini_fallback_enabled := os.environ.get("YUTOME_GEMINI_FALLBACK_ENABLED"):
        gemini_updates["fallback_enabled"] = _env_bool(gemini_fallback_enabled)

    transcript_updates = {}
    if prefer_ytdlp := os.environ.get("YUTOME_TRANSCRIPTS_PREFER_YTDLP_SUBTITLES"):
        transcript_updates["prefer_ytdlp_subtitles"] = _env_bool(prefer_ytdlp)
    if allow_translated := os.environ.get("YUTOME_TRANSCRIPTS_ALLOW_TRANSLATED_CAPTIONS"):
        transcript_updates["allow_translated_captions"] = _env_bool(allow_translated)
    if request_timeout := os.environ.get("YUTOME_TRANSCRIPTS_REQUEST_TIMEOUT_SECONDS"):
        transcript_updates["request_timeout_seconds"] = float(request_timeout)

    yt_dlp_updates = {}
    if ytdlp_profile := os.environ.get("YUTOME_YT_DLP_PROFILE"):
        yt_dlp_updates["profile"] = ytdlp_profile
    if ytdlp_fallback_profile := os.environ.get("YUTOME_YT_DLP_FALLBACK_PROFILE"):
        yt_dlp_updates["fallback_profile"] = ytdlp_fallback_profile
    if ytdlp_profile_fallback_enabled := os.environ.get("YUTOME_YT_DLP_PROFILE_FALLBACK_ENABLED"):
        yt_dlp_updates["profile_fallback_enabled"] = _env_bool(ytdlp_profile_fallback_enabled)
    if ytdlp_retries_when_blocked := os.environ.get("YUTOME_YT_DLP_RETRIES_WHEN_BLOCKED"):
        yt_dlp_updates["retries_when_blocked"] = int(ytdlp_retries_when_blocked)

    youtube_updates = {}
    if youtube_client_secrets := os.environ.get("YUTOME_YOUTUBE_OAUTH_CLIENT_SECRETS"):
        youtube_updates["oauth_client_secrets"] = youtube_client_secrets
    if youtube_api_key_env := os.environ.get("YUTOME_YOUTUBE_API_KEY_ENV"):
        youtube_updates["api_key_env"] = youtube_api_key_env
    if youtube_cookie_browsers := os.environ.get("YUTOME_YOUTUBE_BROWSER_COOKIE_BROWSERS"):
        youtube_updates["browser_cookie_browsers"] = [
            browser.strip() for browser in youtube_cookie_browsers.split(",") if browser.strip()
        ]

    updates = {}
    if proxy_updates:
        updates["proxy"] = config.proxy.model_copy(update=proxy_updates)
    if gemini_updates:
        updates["gemini"] = config.gemini.model_copy(update=gemini_updates)
    if transcript_updates:
        updates["transcripts"] = config.transcripts.model_copy(update=transcript_updates)
    if yt_dlp_updates:
        updates["yt_dlp"] = config.yt_dlp.model_copy(update=yt_dlp_updates)
    if youtube_updates:
        updates["youtube"] = config.youtube.model_copy(update=youtube_updates)
    if updates:
        return config.model_copy(update=updates)
    return config


def _env_bool(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")
