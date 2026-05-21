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
    if proxy_urls := os.environ.get("YTKB_PROXY_URLS"):
        proxy_updates["urls"] = [url.strip() for url in proxy_urls.split(",") if url.strip()]
        proxy_updates["enabled"] = True
        proxy_updates["kind"] = "generic"
    if http_proxy := os.environ.get("YTKB_HTTP_PROXY"):
        proxy_updates["http"] = http_proxy
        proxy_updates["enabled"] = True
    if https_proxy := os.environ.get("YTKB_HTTPS_PROXY"):
        proxy_updates["https"] = https_proxy
        proxy_updates["enabled"] = True
    if username := os.environ.get("YTKB_WEBSHARE_USERNAME"):
        proxy_updates["webshare_username"] = username
        proxy_updates["enabled"] = True
        proxy_updates["kind"] = "webshare"
    if password := os.environ.get("YTKB_WEBSHARE_PASSWORD"):
        proxy_updates["webshare_password"] = password
        proxy_updates["enabled"] = True
        proxy_updates["kind"] = "webshare"
    if domain := os.environ.get("YTKB_WEBSHARE_DOMAIN"):
        proxy_updates["webshare_domain"] = domain
        proxy_updates["enabled"] = True
        proxy_updates["kind"] = "webshare"
    if port := os.environ.get("YTKB_WEBSHARE_PORT"):
        proxy_updates["webshare_port"] = int(port)
        proxy_updates["enabled"] = True
        proxy_updates["kind"] = "webshare"

    gemini_updates = {}
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        gemini_updates["enabled"] = True
    if gemini_model := os.environ.get("YTKB_GEMINI_MODEL"):
        gemini_updates["model"] = gemini_model
        gemini_updates["enabled"] = True
    if gemini_media_resolution := os.environ.get("YTKB_GEMINI_MEDIA_RESOLUTION"):
        gemini_updates["media_resolution"] = gemini_media_resolution
        gemini_updates["enabled"] = True
    if gemini_request_timeout_seconds := os.environ.get("YTKB_GEMINI_REQUEST_TIMEOUT_SECONDS"):
        gemini_updates["request_timeout_seconds"] = float(gemini_request_timeout_seconds)
        gemini_updates["enabled"] = True
    if gemini_cleanup_thinking_level := os.environ.get("YTKB_GEMINI_CLEANUP_THINKING_LEVEL"):
        gemini_updates["cleanup_thinking_level"] = gemini_cleanup_thinking_level
        gemini_updates["enabled"] = True
    if gemini_cleanup_thinking_budget := os.environ.get("YTKB_GEMINI_CLEANUP_THINKING_BUDGET"):
        gemini_updates["cleanup_thinking_budget"] = int(gemini_cleanup_thinking_budget)
        gemini_updates["enabled"] = True
    if gemini_window_seconds := os.environ.get("YTKB_GEMINI_WINDOW_SECONDS"):
        gemini_updates["window_seconds"] = int(gemini_window_seconds)
        gemini_updates["enabled"] = True
    if gemini_cache_enabled := os.environ.get("YTKB_GEMINI_CLEANUP_CACHE_ENABLED"):
        gemini_updates["cleanup_cache_enabled"] = gemini_cache_enabled.strip().lower() in ("1", "true", "yes", "on")
    if gemini_cache_ttl := os.environ.get("YTKB_GEMINI_CLEANUP_CACHE_TTL_SECONDS"):
        gemini_updates["cleanup_cache_ttl_seconds"] = int(gemini_cache_ttl)

    updates = {}
    if proxy_updates:
        updates["proxy"] = config.proxy.model_copy(update=proxy_updates)
    if gemini_updates:
        updates["gemini"] = config.gemini.model_copy(update=gemini_updates)
    if updates:
        return config.model_copy(update=updates)
    return config
