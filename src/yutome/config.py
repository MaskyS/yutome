from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, PositiveInt


DEFAULT_CONFIG_FILENAME = "yutome.toml"

DEFAULT_CONFIG_TOML = """# yutome project configuration

[storage]
data_dir = "data"
artifact_root = "artifacts"
catalog_path = "indexes/catalog.sqlite"
lancedb_path = "indexes/lancedb"

[backfill]
workers = 2
batch_size = 25
max_videos_per_run = 50
request_delay_min_seconds = 5
request_delay_max_seconds = 30

[scheduler]
enabled = false
cadence_hours = 3

[youtube]
# OAuth client secrets JSON path can also come from YUTOME_YOUTUBE_OAUTH_CLIENT_SECRETS.
# A YouTube Data API key can come from YUTOME_YOUTUBE_API_KEY.
api_key_env = "YUTOME_YOUTUBE_API_KEY"
browser_cookie_browsers = ["chrome", "brave", "safari", "firefox", "edge"]

[transcripts]
preferred_languages = ["en"]
include_srt = true
include_markdown = true
word_timestamps = false
# Avoid auto-translated captions by default. When YouTube mislabels English
# audio as another caption language, translated captions are often unusable.
allow_translated_captions = false
request_timeout_seconds = 30.0
prefer_ytdlp_subtitles = false

[transcript_cleanup]
enabled = true
auto_after_sync = true
video_workers = 1
batch_segments = 80
concurrency = 4
max_change_ratio = 0.35
max_patch_retries = 2

[asr]
provider = "faster-whisper"
model = "small.en"
device = "cpu"
compute_type = "int8"

[embeddings]
enabled = false
provider = "voyage"
model = "voyage-4-lite"
dimension = 1024
batch_size = 128
concurrency = 4
max_retries = 5
retry_base_seconds = 2.0

[vectors]
backend = "lancedb"
enabled = true

[exports]
portable_markdown = true
obsidian = true

[proxy]
enabled = false
kind = "generic"
use_for_discovery = false
use_for_metadata = false
use_for_asr_audio = false
# Generic proxy URLs can also come from YUTOME_PROXY_URLS / YUTOME_HTTP_PROXY / YUTOME_HTTPS_PROXY.
# urls = ["http://user:pass@host1:port", "socks5://user:pass@host2:port"]
# http = "http://user:pass@host:port"
# https = "http://user:pass@host:port"
# Webshare credentials can also come from YUTOME_WEBSHARE_USERNAME / YUTOME_WEBSHARE_PASSWORD.
webshare_domain = "p.webshare.io"
webshare_port = 80
webshare_retries_when_blocked = 10

[gemini]
enabled = false
model = "gemini-3.1-flash-lite"
fallback_enabled = false
max_output_tokens = 65536
cleanup_max_output_tokens = 4096
request_timeout_seconds = 90.0
cleanup_thinking_level = "low"
# Explicit per-video context caching for the LLM cleanup pass. Disabled by
# default. The shared-across-batches prefix is small here: only the fixed
# instructions string (~200 tokens) plus bounded video/channel metadata
# (~700 tokens with current _context_payload bounds), for ~900 tokens total
# — right around the 1,024-token Flash-Lite explicit-cache floor. Meanwhile
# each batch's tail (80 caption segments as JSON) is ~1.5-2K tokens, so the
# prefix:tail ratio is roughly 1:2. At that ratio the server-side cache
# lookup overhead cancels the prefill savings and measurements showed 2-3x
# slower wall-clock with caching on. Set true if you grow the prefix (raise
# _context_payload bounds, add channel-level context, etc.) past ~3-4K
# tokens, where the ratio tips back in caching's favor.
cleanup_cache_enabled = false
cleanup_cache_ttl_seconds = 600
media_resolution = "low"
window_seconds = 900

[yt_dlp]
profile = "python-no-js"
fallback_profile = "current"
profile_fallback_enabled = true
sleep_requests_seconds = 2.0
sleep_subtitles_seconds = 8.0
retries_when_blocked = 3
subtitle_retries_when_blocked = 3
retry_sleep = "exp=5:120"
remote_components = false
impersonate = "chrome"
subprocess_timeout_seconds = 300.0

[find]
# Default search mode when `yutome search find` is invoked without --mode.
# "hybrid" combines lexical (SQLite FTS) and semantic (LanceDB + Voyage)
# recall and is the most powerful — but needs VOYAGE_API_KEY and a
# populated vector index. "lexical" uses FTS only and works without any
# embedding setup. `yutome setup` rewrites this to "lexical" when no
# VOYAGE_API_KEY is configured. "semantic" forces vectors only.
default_mode = "hybrid"

[hosted]
enabled = false
# Hosted mode is opt-in and uses the same CLI entry points with a hosted
# provider/search-store broker behind them.
workspace_id = ""
app_url = "https://app.getyutome.com"
api_url = "https://api-production-e072.up.railway.app"
usage_ledger_path = "data/hosted/usage_events.jsonl"
postgres_url_env = "YUTOME_POSTGRES_URL"
"""


class StorageConfig(BaseModel):
    data_dir: Path = Path("data")
    artifact_root: Path = Path("artifacts")
    catalog_path: Path = Path("indexes/catalog.sqlite")
    lancedb_path: Path = Path("indexes/lancedb")


class BackfillConfig(BaseModel):
    workers: PositiveInt = 8
    batch_size: PositiveInt = 25
    max_videos_per_run: PositiveInt = 50


class SchedulerConfig(BaseModel):
    enabled: bool = False
    cadence_hours: PositiveInt = 3


class YouTubeConfig(BaseModel):
    oauth_client_secrets: Path | None = None
    api_key_env: str = "YUTOME_YOUTUBE_API_KEY"
    browser_cookie_browsers: list[str] = Field(
        default_factory=lambda: ["chrome", "brave", "safari", "firefox", "edge"]
    )


class TranscriptConfig(BaseModel):
    preferred_languages: list[str] = Field(default_factory=lambda: ["en"])
    include_srt: bool = True
    include_markdown: bool = True
    word_timestamps: bool = False
    allow_translated_captions: bool = False
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    prefer_ytdlp_subtitles: bool = False


class TranscriptCleanupConfig(BaseModel):
    enabled: bool = True
    auto_after_sync: bool = True
    video_workers: PositiveInt = 1
    batch_segments: PositiveInt = 80
    concurrency: PositiveInt = 4
    max_change_ratio: float = Field(default=0.35, ge=0, le=1)
    max_patch_retries: int = Field(default=2, ge=0, le=5)


class AsrConfig(BaseModel):
    provider: Literal["faster-whisper", "mlx-whisper", "openai", "gemini", "deepgram"] = "faster-whisper"
    model: str = "small.en"
    device: str = "cpu"
    compute_type: str = "int8"


class EmbeddingsConfig(BaseModel):
    enabled: bool = False
    provider: Literal["voyage", "openai-compatible", "local"] = "voyage"
    model: str = "voyage-4-lite"
    dimension: Literal[256, 512, 1024, 2048] = 1024
    batch_size: PositiveInt = 128
    concurrency: PositiveInt = 4
    max_retries: int = Field(default=5, ge=0)
    retry_base_seconds: float = Field(default=2.0, ge=0)


class VectorConfig(BaseModel):
    backend: Literal["lancedb", "sqlite-vec", "none"] = "lancedb"
    enabled: bool = True


class ExportConfig(BaseModel):
    portable_markdown: bool = True
    obsidian: bool = True


class ProxyConfig(BaseModel):
    enabled: bool = False
    kind: Literal["generic", "webshare"] = "generic"
    use_for_discovery: bool = False
    use_for_metadata: bool = False
    use_for_asr_audio: bool = False
    urls: list[str] = Field(default_factory=list)
    http: str | None = None
    https: str | None = None
    webshare_username: str | None = None
    webshare_password: str | None = None
    webshare_domain: str = "p.webshare.io"
    webshare_port: int = Field(default=80, ge=1, le=65535)
    webshare_locations: list[str] = Field(default_factory=list)
    webshare_retries_when_blocked: PositiveInt = 10


class YtDlpConfig(BaseModel):
    profile: Literal["current", "python-no-js", "player-skip-js"] = "python-no-js"
    fallback_profile: Literal["current", "python-no-js", "player-skip-js"] | None = "current"
    profile_fallback_enabled: bool = True
    # Sleep defaults are for the no-proxy / local residential-IP case. When a
    # proxy is actually applied to a request, the *_with_proxy values are used
    # instead because per-IP rate limits no longer apply (rotating residential
    # proxies hand out a fresh IP per request).
    sleep_requests_seconds: float = Field(default=2.0, ge=0)
    sleep_subtitles_seconds: float = Field(default=8.0, ge=0)
    sleep_requests_seconds_with_proxy: float = Field(default=0.0, ge=0)
    sleep_subtitles_seconds_with_proxy: float = Field(default=0.0, ge=0)
    retries_when_blocked: int = Field(default=3, ge=0)
    subtitle_retries_when_blocked: int = Field(default=3, ge=0)
    retry_sleep: str = "exp=5:120"
    remote_components: bool = False
    impersonate: str | None = "chrome"
    subprocess_timeout_seconds: float = Field(default=300.0, gt=0)


class FindConfig(BaseModel):
    default_mode: Literal["lexical", "semantic", "hybrid"] = "hybrid"


class HostedConfig(BaseModel):
    enabled: bool = False
    workspace_id: str = ""
    app_url: str = "https://app.getyutome.com"
    api_url: str = "https://api-production-e072.up.railway.app"
    usage_ledger_path: Path = Path("data/hosted/usage_events.jsonl")
    postgres_url_env: str = "YUTOME_POSTGRES_URL"


class GeminiConfig(BaseModel):
    enabled: bool = False
    model: str = "gemini-3.1-flash-lite"
    fallback_enabled: bool = False
    max_output_tokens: PositiveInt = 65536
    cleanup_max_output_tokens: PositiveInt = 4096
    request_timeout_seconds: float = Field(default=90.0, ge=10)
    cleanup_thinking_level: Literal["minimal", "low", "medium", "high"] | None = "low"
    cleanup_thinking_budget: int | None = Field(default=None, ge=0)
    cleanup_cache_enabled: bool = False
    cleanup_cache_ttl_seconds: PositiveInt = 600
    media_resolution: Literal["low", "medium", "high"] = "low"
    window_seconds: PositiveInt = 900


class AppConfig(BaseModel):
    storage: StorageConfig = Field(default_factory=StorageConfig)
    backfill: BackfillConfig = Field(default_factory=BackfillConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    youtube: YouTubeConfig = Field(default_factory=YouTubeConfig)
    transcripts: TranscriptConfig = Field(default_factory=TranscriptConfig)
    transcript_cleanup: TranscriptCleanupConfig = Field(default_factory=TranscriptCleanupConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    vectors: VectorConfig = Field(default_factory=VectorConfig)
    exports: ExportConfig = Field(default_factory=ExportConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    yt_dlp: YtDlpConfig = Field(default_factory=YtDlpConfig)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    find: FindConfig = Field(default_factory=FindConfig)
    hosted: HostedConfig = Field(default_factory=HostedConfig)


def default_config() -> AppConfig:
    return AppConfig()


def load_config(config_path: Path = Path(DEFAULT_CONFIG_FILENAME)) -> AppConfig:
    with config_path.open("rb") as config_file:
        data = tomllib.load(config_file)
    return AppConfig.model_validate(data)


def write_default_config(config_path: Path = Path(DEFAULT_CONFIG_FILENAME), *, overwrite: bool = False) -> bool:
    if config_path.exists() and not overwrite:
        return False
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return True
