# yutome Project Guide

`yutome` is a local-first YouTube channel knowledge base indexer. It discovers channel videos without the YouTube Data API, captures transcripts and metadata, stores durable artifacts, builds retrieval indexes, and exposes compact commands that agents, humans, and later applications can use.

The project is optimized for:

- Long-form channels with hundreds or thousands of videos.
- Incremental and resumable indexing.
- Timestamped source citations.
- Plain text and Markdown artifacts that remain useful outside the CLI.
- Agent-safe retrieval that avoids dumping entire transcripts into context.
- Rebuildable vector indexes backed by canonical SQLite and disk artifacts.

Default corpus scope:

- Include public videos.
- Include completed lives/streams.
- Skip Shorts unless an explicit Shorts mode is added.
- Exclude comments in the current product.
- Exclude website crawling in the current product, even when a creator has a related website.
- Avoid durable audio/video storage by default.

The current primary test corpus is Leo and Longevity:

- Channel URL: `https://www.youtube.com/@LeoandLongevity/`
- Site: `https://www.leoandlongevity.com/`
- Videos discovered: `739`
- Videos indexed: `737`
- Deferred videos: `2`, currently `deferred: rate_limited`
- Transcript versions: `750`
- Transcript attempts recorded: `1062`
- Chunks: `9080`
- Embedding records: `9080`
- LanceDB rows: `9080`
- Portable Markdown exports: `737`
- Obsidian exports: `737`

This document is written as a first-time guide for implementation and review. It describes the current system, the reasoning behind the main design choices, and the next work to prioritize.

## Goals

Primary goal:

- Convert an entire YouTube channel into a durable, source-cited knowledge base that can be searched by people, command-line workflows, applications, and agents.

The index should support:

- Exact keyword search for names, drugs, molecules, terms, and quoted phrases.
- Semantic search for broader ideas and paraphrases.
- Hybrid retrieval that benefits from both lexical precision and semantic recall.
- Explicit context expansion around a search hit.
- Direct timestamp citations back to YouTube.
- Plain transcript exports for long-term portability.
- Markdown exports for Obsidian and other note systems.
- Resumable operation when a run is interrupted, rate-limited, or intentionally split into smaller batches.

Non-goals for the current slice:

- No built-in LLM answer command yet.
- No mandatory topic-card or per-video summary artifacts yet.
- No UI yet.
- No reliance on the YouTube Data API.
- No dependency on Obsidian Web Clipper internals.
- No automatic ASR as the normal path for videos that already have usable captions or subtitle files.
- No comments, Shorts, or related-site crawling in the current slice.
- No Postgres/pgvector, Qdrant, or hosted service-mode vector backend in the current slice.

## Product Shape

The system has four useful surfaces.

1. Artifact store

   Raw metadata, raw provider transcripts, normalized timestamped segments, plain transcripts, subtitle files, chunks, and exports are written to disk. This keeps the corpus portable and inspectable even if the indexes are rebuilt.

2. SQLite catalog

   SQLite is the canonical structured catalog. It tracks videos, transcript versions, chunks, attempts, embeddings, statuses, and SQLite FTS rows.

3. LanceDB retrieval index

   LanceDB is the rebuildable vector and hybrid search index. It stores chunk text, embeddings, and enough metadata to return useful results without extra joins for every field.

4. Query API

   A shared declarative `QueryRequest` primitive in `src/yutome/query.py` powers CLI, MCP, and HTTP retrieval. The transport-neutral verbs in `src/yutome/api.py` are `find`, `list`, `show`, and `q`.

5. CLI

   The `yutome` CLI is the current product interface. It can initialize, index, resume, list status, test proxies/providers, search with `find`, expand citations with `show context`, rebuild indexes, and export Markdown.

6. Scheduler and job model

   The catalog includes a `jobs` table and config includes scheduler cadence. Durable scheduled syncing is a target product surface, but the current CLI does not yet install a cron/launchd job or expose a full job runner.

## Stack

Project and packaging:

- Python `>=3.11`
- `uv`
- `typer` for CLI
- `pydantic` for config validation
- `pytest` for tests

Optional ingest dependencies:

- `yt-dlp` for channel discovery, metadata, subtitle files, and optional audio download.
- `youtube-transcript-api` for caption/transcript fetching.
- `curl-cffi` because modern YouTube access frequently benefits from browser-like TLS/request behavior through `yt-dlp`.

Optional retrieval dependencies:

- `voyageai` for embeddings.
- `lancedb` for vector and hybrid retrieval.

Optional fallbacks:

- `google-genai` for Gemini video-understanding fallback.
- `faster-whisper` for local ASR fallback.

Planned optional retrieval/enrichment dependencies:

- Provider-backed reranker, disabled unless configured.
- Additional ASR providers behind one interface: `mlx-whisper`, OpenAI, Gemini, and Deepgram.
- Future service-mode vector adapters such as Postgres/pgvector or Qdrant, not v1 defaults.

Scheduler target:

- A generated cron or launchd job should eventually run scheduled syncs.
- The default intended cadence is every 3 hours.
- Scheduled runs should be bounded: 2 workers, batch size 25, max 50 videos per scheduled run, jittered request delays, and resumable checkpoints.

Feature dependencies are mandatory for now; install the development environment with:

```bash
uv sync
```

## Current CLI Surface

Implemented commands:

| Command | Purpose |
| --- | --- |
| `yutome setup [SOURCE]` | Guided first-run setup: config, `.env`, Webshare proxy secrets, semantic search, YouTube subscription import, source picker, and optional first sync. |
| `yutome init` | Create `yutome.toml`, base directories, and SQLite catalog. |
| `yutome doctor` | Check runtime, config, SQLite FTS5, and optional dependency availability. |
| `yutome add SOURCE` | Add a YouTube channel or video source to the local library. |
| `yutome import FILE` | Import source entries from CSV, OPML/XML, or a plain URL list. |
| `yutome import-youtube [CHANNEL]` | Import the signed-in user's YouTube subscriptions, or a channel's public subscriptions when `CHANNEL` is passed. |
| `yutome select/unselect SOURCE` | Include or exclude source entries from default library syncs. |
| `yutome sync [SOURCE]` | Discover and index a channel source, exact video source, or selected sources when omitted. |
| `yutome sync --all` | Sync every selected source in the local library. |
| `yutome find QUERY` | Ranked retrieval over chunks, titles, or descriptions with lexical/semantic/hybrid modes. |
| `yutome list videos` | Enumerate videos by status, channel, source, language, and date filters. |
| `yutome list channels` | Show channel entries from the local source library and catalog. |
| `yutome list attention` | Show failed/deferred videos with latest provider-attempt details. |
| `yutome list status` | Show catalog counts, index percentages, statuses, and job breakdowns. |
| `yutome show chunk/video/channel/transcript` | Fetch one resource by id or selector; transcripts can be addressed by transcript id or active video id. |
| `yutome show source` | Resolve a citation anchor to a YouTube timestamp and provenance. |
| `yutome show context` | Expand a selected hit into bounded neighboring transcript context. |
| `yutome q` | Execute a raw QueryRequest JSON object. |
| `yutome eval run FILE` | Run corpus-relative retrieval evals from a JSON fixture. |
| `yutome remote prepare` | Generate and store the authenticated HTTP API token for remote clients. |
| `yutome remote serve` | Serve the authenticated HTTP API for private-network or reverse-proxy remote access. |
| `yutome remote mcp` | Serve the authenticated MCP streamable HTTP endpoint for remote agent clients. |
| `yutome remote check URL` | Verify remote liveness and authenticated readiness from a client machine. |
| `yutome rebuild-chunks` | Rebuild chunk rows/artifacts from active normalized transcripts. |
| `yutome rebuild-vectors` | Rebuild or resume embeddings and LanceDB rows from canonical chunks. |
| `yutome proxy-info` | Show proxy policy and supported env config. |
| `yutome proxy-test` | Test transcript API and yt-dlp subtitle paths through configured proxy. |
| `yutome gemini-test` | Test Gemini video-understanding fallback on one video. |
| `yutome export portable-md` | Export indexed videos as portable Markdown. |
| `yutome export obsidian` | Export indexed videos as Obsidian-friendly Markdown. |
| `yutome quality upgrade` | Create LLM-cleaned transcript versions from already-indexed active transcripts. |

Target command surface not yet implemented:

| Target command | Intended purpose |
| --- | --- |
| `yutome sync --channel ID` | Sync one registered channel by local/channel id. |
| `yutome backfill --channel ID --limit N --workers N` | Controlled historical ingest for an existing channel. Current `sync --use-catalog --max-process --workers` covers part of this need. |
| `yutome index --lexical --vectors` | Rebuild SQLite FTS and/or LanceDB from artifacts. Current implementation has `rebuild-chunks` and `rebuild-vectors`. |
| `yutome scheduler install` | Install a cron/launchd schedule for bounded recurring syncs. |
| `yutome inspect video VIDEO_ID` | Inspect one video, active transcript version, artifacts, chunks, and status. |
| `yutome inspect chunk CHUNK_ID` | Inspect one chunk, text, timestamps, source, and neighboring chunks. |
| `yutome inspect attempts VIDEO_ID` | Inspect provider attempts and retry classification for one video. |

This distinction matters during review: implemented commands should be runnable now; target commands should be treated as design requirements or next-slice candidates.

Important `sync` options:

| Option | Use |
| --- | --- |
| `--limit` | Limit videos discovered per tab during development or small tests. |
| `--max-process` | Process only N non-indexed candidate videos after discovery. Useful for batch backfills. |
| `--workers` | Process videos concurrently. Caption fetching should remain conservative unless proxy capacity and block behavior are known. |
| `--use-catalog` | Skip channel crawling and use already discovered videos. Useful for resuming a known catalog. |
| `--retry-failed` | Include previously failed or deferred videos. |
| `--status-filter` | Process only matching statuses, for example `--status-filter 'deferred: rate_limited'`. |
| `--source-filter` | Refresh only videos whose active transcript source matches a prefix, usually with `--force`. |
| `--force` | Reprocess videos that already have an active transcript. |
| `--gemini-fallback` | Use Gemini video understanding when caption/subtitle paths fail. |
| `--asr-fallback` | Use local ASR when caption/subtitle paths fail. This is a last-resort path. |
| `--stop-on-rate-limit` | Stop the run when a likely block/rate limit appears. Default behavior. |
| `--continue-on-rate-limit` | Keep processing after block/rate-limit signals. Higher risk for large runs. |
| `--sleep` | Per-video delay between transcript requests. |
| `--shortest-first` | Process shorter candidate videos first. Useful for fallback testing or quick coverage. |
| `--max-duration-seconds` | Restrict processing by duration. |
| `--proxy-retries-when-blocked` | Override Webshare transcript retry count for one run. |

`sync` intentionally does not expose provider-order flags in the normal command surface. The default
policy is staged and logged: discovery stores cheap catalog metadata, stage 1 runs the transcript API
across candidates, stage 2 immediately retries unresolved videos with `yt-dlp` subtitles, and stage 3
backfills exact per-video metadata. The logs print stage banners and queue counts so the operator can see
when fallback and metadata backfill are happening without having to orchestrate those phases.

Important `find` options:

| Option | Use |
| --- | --- |
| `--mode lexical` | SQLite FTS only. Best for exact strings, acronyms, names, and drug terms. |
| `--mode semantic` | LanceDB vector search only. Best for paraphrase and conceptual recall. |
| `--mode hybrid` | LanceDB vector + text hybrid. Default. |
| `--in chunks` | Search transcript chunks. Default. |
| `--in titles` | Search video titles lexically. |
| `--in descriptions` | Search video descriptions lexically. |
| `--project thin` | Compact result shape. Does not include full chunk text or full descriptions. |
| `--project chunk` | Include full matched chunk text. |
| `--project metadata` | Include video metadata fields and full description. |
| `--group-by video` | Return ranked videos with top chunk hits nested under each video. |
| `--json` | Emit machine-readable JSON. |

Important `show context` inputs:

| Input | Use |
| --- | --- |
| `CHUNK_ID` | Expand around an exact retrieval hit. |
| `--video-id VIDEO_ID --time SECONDS` | Expand around a timestamp in a video. |
| `URL` or `--youtube-url URL` | Parse `v`/`youtu.be` plus `t` or `start` timestamp and expand around it. |
| `--token-budget 3000` | Maximum estimated context tokens. Default is `3000`. |

## Configuration

The config file is `yutome.toml`. The runtime config is the parsed TOML plus Pydantic defaults for any fields not present in the local file.

Core defaults in `src/yutome/config.py`:

```toml
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

[transcripts]
preferred_languages = ["en"]
include_srt = true
include_markdown = true
word_timestamps = false
allow_translated_captions = false
request_timeout_seconds = 30.0
prefer_ytdlp_subtitles = false

[transcript_cleanup]
video_workers = 1
batch_segments = 80
concurrency = 4
max_change_ratio = 0.35
max_patch_retries = 2

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

[proxy]
enabled = false
kind = "generic"
use_for_discovery = false
use_for_metadata = false
use_for_asr_audio = false
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
media_resolution = "low"
window_seconds = 900

[yt_dlp]
sleep_requests_seconds = 2.0
sleep_subtitles_seconds = 8.0
subtitle_retries_when_blocked = 3
retry_sleep = "exp=5:120"
remote_components = false
impersonate = "chrome"
subprocess_timeout_seconds = 300.0
```

Secrets and local provider credentials should live in `.env`, not in checked-in docs or config. Supported environment-style inputs include:

- Voyage API key through the provider's normal environment variable.
- Gemini API key through `GEMINI_API_KEY`.
- Webshare credentials through `YUTOME_WEBSHARE_USERNAME` and `YUTOME_WEBSHARE_PASSWORD`.
- Generic proxies through `YUTOME_PROXY_URLS`, `YUTOME_HTTP_PROXY`, and `YUTOME_HTTPS_PROXY`.

## Architecture

The canonical source of truth is the artifact store plus SQLite catalog. LanceDB is a rebuildable retrieval index.

```text
YouTube channel
  -> yt-dlp discovery over /videos and /streams tabs
  -> optional RSS hint after channel id is known
  -> video metadata snapshots
  -> transcript acquisition
  -> normalized timestamped segments
  -> plain transcript and subtitle artifacts
  -> timestamp-aware chunks
  -> SQLite catalog + SQLite FTS5
  -> Voyage embeddings
  -> LanceDB vector + FTS index
  -> find/list/show/q/export commands
```

Main boundaries:

- `src/yutome/youtube.py` owns YouTube discovery, metadata fetches, transcript API calls, subtitle fetches, proxy selection, and ASR audio download.
- `src/yutome/transcripts.py` owns transcript normalization and transcript artifacts.
- `src/yutome/chunking.py` owns chunk construction.
- `src/yutome/store.py` owns catalog persistence and FTS writes.
- `src/yutome/embeddings.py` owns Voyage embedding calls and LanceDB writes.
- `src/yutome/query.py` owns QueryRequest compile/execute and SQL/LanceDB dispatch.
- `src/yutome/api.py` owns transport-neutral `find`, `list`, `show`, and `q` verbs.
- `src/yutome/retrieval.py` owns shared citation/context helper functions.
- `src/yutome/exports.py` owns portable and Obsidian Markdown output.
- `src/yutome/indexer.py` orchestrates discovery, per-video indexing, fallback decisions, statuses, and attempts.
- `src/yutome/maintenance.py` owns rebuild routines.

Discovery policy:

- Use `yt-dlp` for channel crawling.
- Do not require the YouTube Data API.
- Query `/videos` and `/streams` tabs for the current default corpus.
- Use RSS only as a cheap new-upload hint after channel id is known; it is not enough for historical discovery.
- Skip Shorts unless an explicit Shorts mode is added.
- Store raw `yt-dlp` metadata before normalization so extractor changes can be audited.

## Code Map

Read these files first:

- CLI entrypoint: `src/yutome/cli.py`
- Configuration: `src/yutome/config.py`
- Environment overrides: `src/yutome/env.py`
- Path layout: `src/yutome/paths.py`
- SQLite schema/bootstrap: `src/yutome/db.py`
- Ingest orchestration: `src/yutome/indexer.py`
- YouTube/transcript providers: `src/yutome/youtube.py`
- Gemini fallback: `src/yutome/gemini.py`
- ASR fallback: `src/yutome/asr.py`
- Transcript normalization/artifacts: `src/yutome/transcripts.py`
- Chunking: `src/yutome/chunking.py`
- Catalog writes and FTS rebuild: `src/yutome/store.py`
- Embeddings and LanceDB: `src/yutome/embeddings.py`
- Retrieval and context expansion: `src/yutome/retrieval.py`
- Markdown exports: `src/yutome/exports.py`
- Maintenance rebuilds: `src/yutome/maintenance.py`
- Tests: `tests/test_config_paths_db.py`, `tests/test_retrieval_exports.py`
- Proxy strategy: `docs/proxy-strategy.md`
- Review checklist: `docs/reviewer-handoff.md`

## Storage Layout

Default layout:

```text
yutome.toml
data/
  artifacts/
    channels/{channel_id}/channel.json
    videos/{video_id}/metadata/{version_id}.json
    videos/{video_id}/transcripts/{transcript_version_id}/raw.json
    videos/{video_id}/transcripts/{transcript_version_id}/normalized.jsonl
    videos/{video_id}/transcripts/{transcript_version_id}/transcript.txt
    videos/{video_id}/transcripts/{transcript_version_id}/transcript.md
    videos/{video_id}/transcripts/{transcript_version_id}/transcript.vtt
    videos/{video_id}/transcripts/{transcript_version_id}/transcript.srt
    videos/{video_id}/chunks/{chunker_version}.jsonl
    videos/{video_id}/summaries/{summary_version}.json
  indexes/
    catalog.sqlite
    lancedb/
  exports/
    portable-md/
    obsidian/
  logs/
```

Artifact roles:

| Artifact | Consumer | Why it exists |
| --- | --- | --- |
| `raw.json` | Developers, provider debugging, replay | Preserves the provider-native payload before normalization. |
| `normalized.jsonl` | Chunker, exports, deterministic rebuilds | Canonical timestamped transcript segment format. |
| `transcript.txt` | Humans, LLM context, grep, portable archives | Plain non-timestamped transcript. This is intentionally always written. |
| `transcript.md` | Humans, Markdown tools | Timestamp-linked transcript without export frontmatter. |
| `transcript.vtt` | Subtitle tools, media players, future UI | Standard WebVTT with timestamps. |
| `transcript.srt` | Subtitle tools | Optional SRT output. |
| `chunks/{chunker_version}.jsonl` | Rebuild inspection, retrieval debugging | Versioned chunk artifact. |
| `summaries/{summary_version}.json` | Future optional summaries/topic cards | Reserved, not required for current retrieval. |

Plain text matters because agents and non-Obsidian tooling often need a readable transcript without timestamps interrupting every line. Timestamped artifacts matter because citations, context expansion, and UI navigation need exact source locations. Both are required.

Snapshot/versioning policy:

- Metadata and transcripts should be content-addressed or version-addressed.
- Active indexes point to the latest accepted transcript version.
- Older transcript versions should remain addressable so citations do not drift.
- Chunk artifacts are versioned by `chunker_version`; changing chunking strategy should create a rebuildable new view, not silently mutate the meaning of old chunk ids.
- Tool/provider versions should be recorded in future manifest files so broken extractor/provider runs can be audited.

## SQLite Model

SQLite is the canonical catalog and status store.

Core tables:

| Table | Role |
| --- | --- |
| `schema_migrations` | Tracks catalog schema version. |
| `channels` | Channel identity, handle, source URL, uploads URL, title, description, sync timestamps. |
| `videos` | Video metadata, description, duration, published date, thumbnail, live status, ingest status. |
| `transcript_versions` | Versioned transcript source, language, generated/manual flag, artifact paths, text hash, segment count, active flag. |
| `chunks` | Active transcript chunk rows with sequence, timestamps, text, text hash, token count, and chunker version. |
| `chunks_fts` | SQLite FTS5 virtual table over `chunks.text`. |
| `embeddings` | Per-chunk provider/model/dimension status for embedding and index completion. |
| `transcript_attempts` | Per-video provider attempts, status, error class, retryable flag, and error text. |
| `jobs` | Reserved table for future durable scheduling. |

Important SQLite invariants:

- Active retrieval should only use active transcript versions.
- `chunks` are rebuildable from active `normalized.jsonl` transcript artifacts.
- `chunks_fts` is rebuildable from `chunks`.
- `embeddings` records should match the active chunk ids, provider, model, and dimension.
- Deferred/rate-limited videos remain in the catalog and are explicitly retried later.

Target job invariants:

- Jobs should be idempotent.
- Jobs should be lockable by a worker.
- Jobs should track attempts, retry-after time, and final error.
- Interrupted jobs should be safely resumable.
- Scheduled backfills should be bounded by max videos per run and safe provider concurrency.

Planned manifest metadata:

- `yt-dlp` version.
- `youtube-transcript-api` version.
- Gemini model and API version where exposed.
- ASR provider/model/version.
- Embedding provider/model/dimension.
- Chunker version and parameters.
- Exporter version.

Useful consistency SQL:

```bash
sqlite3 data/indexes/catalog.sqlite "
SELECT COUNT(*) AS chunks FROM chunks;
SELECT COUNT(*) AS indexed_embeddings FROM embeddings WHERE index_status='indexed';
SELECT chunker_version, COUNT(*) AS chunks, MAX(token_count) AS max_tokens
FROM chunks
GROUP BY chunker_version;
SELECT ingest_status, COUNT(*)
FROM videos
GROUP BY ingest_status
ORDER BY COUNT(*) DESC;
"
```

## LanceDB Model

LanceDB table: `chunks`

Required columns:

- `chunk_id`
- `channel_id`
- `video_id`
- `transcript_version_id`
- `source`
- `language`
- `is_generated`
- `sequence`
- `start_ms`
- `end_ms`
- `text`
- `token_count`
- `text_hash`
- `chunker_version`
- `active`
- `embedding_model`
- `embedding_dim`
- `vector`

Required text index:

- FTS index on `text`, named by LanceDB as `text_idx`.

Why LanceDB is rebuildable:

- SQLite and artifacts are the source of truth.
- Embeddings are deterministic enough for a selected provider/model/dimension.
- If LanceDB table shape is missing required columns, `find --mode hybrid` and semantic retrieval fail with a clear message to run `yutome rebuild-vectors`.
- If hybrid search fails because the LanceDB FTS index is not ready, the error tells the user to run `yutome rebuild-vectors`.

Why SQLite is still needed when LanceDB exists:

- It owns canonical video/transcript state.
- It records provider attempts and failures.
- It supports cheap exact lexical fallback through SQLite FTS5.
- It lets the system rebuild LanceDB without re-fetching YouTube data.
- It is easier to inspect, migrate, and debug than treating a vector table as the catalog.

Why LanceDB is still needed when SQLite exists:

- SQLite FTS is lexical only.
- Vector search finds paraphrases and concepts.
- Hybrid retrieval can combine semantic recall with BM25-style text matching.
- LanceDB can later support incremental vector index maintenance and metadata filtering.

## Transcript Acquisition

Default provider order:

1. Manual captions when exposed by the provider.
2. YouTube generated captions for preferred languages.
3. `yt-dlp` subtitle fallback in JSON3 format.
4. Optional Gemini video-understanding fallback.
5. Optional ASR fallback.
6. Optional enrichment from an LLM, versioned separately from transcript acquisition.

The first two paths are much cheaper than ASR and should be exhausted before local audio transcription for normal indexing.

The current implementation records `is_generated` and transcript source. Provider APIs do not always make manual-versus-generated selection equally ergonomic, so review should verify whether preferred manual captions are actually selected before generated captions when both are available.

### Preferred Captions

Config defaults:

- `preferred_languages = ["en"]`
- `allow_translated_captions = false`

Reasoning:

- Auto-translated captions can be much worse than original captions.
- YouTube can expose mislabeled generated captions, including cases where English audio appears under a non-English caption language.
- Non-preferred generated caption tracks are treated cautiously.

The implementation includes a caption-language check path and records `caption-language-check` attempts when suspicious tracks are encountered. Bad-caption outcomes are not treated as permanent proof that the video is useless; they are a reason to try another provider or a fallback.

### yt-dlp Subtitle Fallback

`yt-dlp` subtitle fetching uses:

- `--skip-download`
- `--write-auto-subs`
- `--write-subs`
- `--sub-langs`
- `--sub-format json3`
- `--sleep-subtitles`
- retry sleep from config
- optional impersonation
- optional proxy

For English, the default language candidate is `en-orig` when translated captions are disabled. This avoids silently using translated captions as the main transcript. If translated captions are explicitly enabled, `en` is also considered.

`yt-dlp` is important because it can sometimes access subtitle files when `youtube-transcript-api` is blocked or returns no useful transcript.

### Gemini Fallback

Gemini video understanding is optional and currently used as an explicit fallback, not the main ingest path.

Use cases:

- Videos with no accessible captions.
- Videos where caption language metadata is misleading.
- Videos where subtitle providers are blocked but the YouTube URL can still be processed by Gemini.

Tradeoffs:

- It can be slower than fetching existing captions.
- It may produce longer unsegmented transcript blocks that require forced chunk splitting.
- API limits and cost need to be treated as product constraints.
- It should be versioned as a transcript source, not silently mixed with caption transcripts.

Current config default:

- `model = "gemini-3.1-flash-lite"`
- `cleanup_max_output_tokens = 4096`
- `request_timeout_seconds = 90.0`
- `cleanup_thinking_level = "low"`
- `media_resolution = "low"`
- `window_seconds = 900`
- `max_output_tokens = 65536`

### ASR Fallback

ASR is available but should be a last resort.

Reasons:

- It requires audio download.
- It is slower and more compute-intensive than caption/subtitle fetches.
- It can consume proxy bandwidth if routed through a residential proxy.

The proxy policy defaults `use_for_asr_audio = false` so residential proxy bandwidth is not burned by large media downloads unless direct audio fetches are also blocked and the user explicitly opts in.

ASR implementation requirements:

- Download audio to temporary storage only.
- Delete audio after transcript artifacts are written.
- Preserve provider/model/version/timing metadata.
- Normalize ASR segments through the same transcript artifact path as captions.
- Keep word timestamps disabled by default; segment timestamps are enough for v1 retrieval and citation.

## Rate Limits, Proxies, And Resumability

The default operational posture is:

- Use the local residential IP first.
- Keep runs resumable and commit after each video.
- Stop on likely rate limits by default.
- Use small bounded retries rather than hammering a failing provider.
- Add a rotating residential proxy only when sustained block/rate-limit failures appear.

Proxy support:

- Generic proxy pool through `YUTOME_PROXY_URLS`.
- Single HTTP/HTTPS proxies through `YUTOME_HTTP_PROXY` and `YUTOME_HTTPS_PROXY`.
- Webshare rotating residential through `YUTOME_WEBSHARE_USERNAME` and `YUTOME_WEBSHARE_PASSWORD`.
- `youtube-transcript-api` can use its native Webshare proxy config.
- `yt-dlp` receives a proxy URL through `--proxy`.

Current proxy design:

- Generic proxy pools select a proxy deterministically by video id. This spreads requests without changing a video's route mid-attempt.
- Webshare uses rotating residential credentials and lets Webshare/library behavior handle blocked retries.
- `yt-dlp` block markers include 429, CAPTCHA, "not a bot", Google `/sorry`, IP blocked, and sign-in challenges.

Resumability rules:

- Videos are processed independently.
- Successful transcript artifacts and catalog rows are committed per video.
- Failed provider attempts are recorded in `transcript_attempts`.
- Rate-limit outcomes become deferred statuses rather than permanent failures.
- `--use-catalog` avoids rediscovery when resuming known videos.
- `--max-process` lets users index a channel a little at a time.
- `--retry-failed` is required to revisit failed or deferred videos.
- Embedding failures leave chunks pending so `rebuild-vectors --resume` can continue.

Reliability requirements:

- Provider-level circuit breakers should pause transcript fetching after repeated 429/block errors.
- `yt-dlp` must run from the project environment, not an arbitrary system Python.
- `yt-dlp` subprocesses must have a fixed timeout so one hung metadata/subtitle/audio request cannot stall a backfill.
- Historical transcript backfills should be able to defer full metadata to reduce YouTube request volume.
- Backfill resumes should be able to skip discovery and quiet already-indexed videos so runs reach fresh candidates quickly.
- Start channel-scale YouTube work at low concurrency, such as 2 workers, and increase only after block/rate-limit telemetry is clean.
- Proxy-backed concurrency should still be bounded by provider behavior, not just proxy capacity.

See `docs/proxy-strategy.md` for the proxy/provider operating policy.

## Chunking

Current chunker: `timestamp-aware-v2`

Defaults:

- Target size: `700` estimated tokens.
- Overlap: `100` estimated tokens.
- Hard max: `1000` estimated tokens.
- Token estimate: `round(word_count * 1.33)`.

Current Leo corpus:

- Chunks: `9080`
- Max chunk size: at or below `1000` estimated tokens
- Chunker version: `timestamp-aware-v2`

Behavior:

- Build chunks from normalized timestamped transcript segments.
- Preserve start/end timestamp provenance.
- Emit overlap from the previous chunk so retrieval has continuity across boundaries.
- Split oversized provider segments before chunking.
- Mark chunks with forced split metadata when split segments are used.
- Compute chunk ids from video id, transcript version id, timestamp range, text hash, and chunker version.

### Chunking Rationale

A target near 700 estimated tokens is a pragmatic middle point:

- Smaller chunks, such as 150 to 300 tokens, improve precise hit localization but can fragment ideas and cause many near-duplicate hits.
- Larger chunks, such as 1200 to 2000 tokens, preserve more local reasoning but are expensive for agent context and make thin retrieval less precise.
- Around 700 tokens usually captures a few minutes of speech, enough for a coherent claim or explanation, while still allowing several chunks inside a 3000-token context expansion.

The 100-token overlap is intentionally modest:

- It keeps continuity when a sentence or idea crosses a chunk boundary.
- It does not multiply the corpus size too aggressively.
- `context` dedupes overlapping text when merging neighboring chunks.

The hard 1000-token cap exists because provider segments are not uniform:

- `youtube-transcript-api` and `yt-dlp` usually return many small timed segments.
- Gemini or other fallback providers can return much longer blocks.
- Bad auto-captions can produce odd segmentation.
- A hard cap prevents one provider artifact from blowing up retrieval result size.

Future chunking improvements:

- Replace estimated tokens with provider/model-specific tokenizer counts.
- Store forced split metadata in a richer JSON field instead of only deriving it from segment ids.
- Add sentence-aware splitting for very long provider segments.
- Add topic-boundary-aware chunking only after retrieval evaluation exists.

## Embedding Pipeline

Current embedding provider:

- Voyage
- Default model: `voyage-4-lite`
- Default output dimension: `1024`
- Default batch size: `128`
- Default concurrency: `4`

Embedding behavior:

- Query only chunks that do not already have an indexed embedding record for the selected provider/model/dimension.
- Fetch embeddings in bounded concurrent batches.
- Retry transient and rate-limit provider errors with exponential backoff.
- Leave failed batches pending so `yutome rebuild-vectors --resume` can continue.
- Write LanceDB rows and SQLite embedding status after successful batches.
- Create or refresh the LanceDB FTS index on `text` when vectors are ingested.

Important implementation detail:

- Embedding API calls are parallelized.
- LanceDB/SQLite writes are kept controlled so one failed batch does not corrupt the whole rebuild.
- Full `yutome rebuild-vectors` drops the existing LanceDB table and clears matching embedding records.
- `yutome rebuild-vectors --resume` preserves existing indexed rows and embeds only pending chunks.

Future embedding work:

- Add provider abstraction for OpenAI-compatible and local embeddings.
- Add richer progress output per batch.
- Add merge-insert/upsert behavior for incremental LanceDB maintenance.
- Add scalar indexes if LanceDB filtering or update patterns require them.

## Citation Model

Every retrieval hit should be able to provide:

- `video_id`
- `transcript_version_id`
- `start_ms`
- `end_ms`
- `exact_text` or selected chunk text when requested
- bounded `snippet`
- `youtube_url`
- `chunk_id`
- transcript source
- language
- generated/manual flag

Human citation format:

```text
https://youtube.com/watch?v={video_id}&t={seconds}s
```

Internal resource URI format:

```text
yutome://chunk/{chunk_id}
```

Future resource URI targets:

```text
yutome://video/{video_id}
yutome://transcript/{transcript_version_id}
yutome://segment/{transcript_version_id}/{sequence}
```

The citation model is deliberately transcript-first. Video descriptions, summaries, and topic cards can help discovery, but claims about what was said should cite transcript timestamps.

## Retrieval Contract

Retrieval is intentionally two-stage.

Stage 1 returns compact hits:

```bash
uv run yutome find "Crohn probiotics" --mode hybrid --limit 5 --json
```

Defaults:

- `--mode hybrid`
- `--in chunks`
- `--project thin`
- `--limit 10`

Modes:

- `lexical`: SQLite FTS5 using `chunks_fts`.
- `semantic`: LanceDB vector search.
- `hybrid`: LanceDB vector + text hybrid search.

Projection levels:

- `thin`: handles, snippet, provenance, timestamps, and scores.
- `chunk`: thin fields plus full matched chunk text.
- `metadata`: thin fields plus video metadata fields and full description.
- `group_video`: ranked videos with nested thin chunk hits.
- `video_card`, `video_attention`, `channel_card`, and `status_breakdown`: list/show projections for non-chunk entities.

Thin result shape:

```json
{
  "chunk_id": "...",
  "resource_uri": "yutome://chunk/...",
  "video_id": "...",
  "title": "...",
  "youtube_url": "https://youtube.com/watch?v=VIDEO_ID&t=123s",
  "start_ms": 123000,
  "end_ms": 180000,
  "snippet": "...bounded text...",
  "transcript_version_id": "...",
  "transcript_source": "youtube-transcript-api",
  "language": "en",
  "is_generated": true,
  "token_count": 690,
  "match_type": "hybrid",
  "scores": {
    "hybrid_score": 0.78
  }
}
```

Chunk detail adds:

```json
{
  "text": "...full matched chunk text..."
}
```

Metadata detail adds:

```json
{
  "published_at": "...",
  "duration_seconds": 1234,
  "channel_id": "...",
  "sequence": 12,
  "chunker_version": "timestamp-aware-v2",
  "text_hash": "...",
  "description": "...full YouTube description..."
}
```

Important retrieval rule:

- Full chunk text is not returned by default.
- Full YouTube descriptions are not returned by default.
- Agents should call `show context` only after selecting promising thin hits.

## Context Contract

Stage 2 expands a selected hit:

```bash
uv run yutome show context CHUNK_ID --token-budget 3000
uv run yutome show context "https://youtube.com/watch?v=VIDEO_ID&t=123s" --token-budget 1800
uv run yutome show context --video-id VIDEO_ID --time 123
```

Output shape:

```json
{
  "anchor": {
    "chunk_id": "...",
    "youtube_url": "https://youtube.com/watch?v=VIDEO_ID&t=123s",
    "text": "...anchor chunk..."
  },
  "token_budget": 3000,
  "estimated_tokens": 2800,
  "text": "...merged neighboring context...",
  "chunks": [
    {
      "chunk_id": "...",
      "start_ms": 90000,
      "end_ms": 150000,
      "text": "..."
    }
  ],
  "citations": [
    {
      "chunk_id": "...",
      "video_id": "...",
      "title": "...",
      "youtube_url": "https://youtube.com/watch?v=VIDEO_ID&t=90s",
      "start_ms": 90000,
      "end_ms": 150000,
      "transcript_version_id": "...",
      "transcript_source": "yt-dlp-json3:en-orig"
    }
  ]
}
```

Expansion behavior:

- Locate anchor by chunk id, video/time, or timestamped YouTube URL.
- Load all chunks from the same transcript version.
- Add neighboring chunks symmetrically around the anchor until the token budget is reached.
- Return selected chunks in sequence order.
- Merge text while deduping exact word overlap at chunk boundaries.
- Preserve per-chunk citations so a synthesizer can cite exact timestamps.

## Context Bloat Math

The Leo corpus currently has `9080` chunks.

Napkin math:

- Average chunk target is around 700 estimated tokens.
- Full corpus chunk text is roughly millions of tokens, before metadata and overlap.
- `10` full chunks can be around `7,000` estimated tokens.
- `20` full chunks can be around `14,000` estimated tokens.
- Repeated agent calls that return full chunks by default can burn `50,000+` context tokens quickly.
- A thin hit is usually a few hundred tokens including metadata and snippet.
- `10` thin hits are usually closer to `1,000` to `3,000` tokens, depending on titles/snippets/scores.

This is why `find` defaults to thin results and why `show context` is explicit. The intended agent flow is:

1. Retrieve thin hits with `find`.
2. Pick the likely relevant hit or small group of hits.
3. Call `show context` for bounded local transcript text.
4. Synthesize only from expanded context.
5. Cite timestamp URLs.

Returning full descriptions by default would also bloat context. Descriptions are often long, repetitive, and less trustworthy than transcript context for claims about what was said. They are still available through `--project metadata`.

## Search Behavior And Known Gaps

Current search behavior:

- Lexical search uses SQLite FTS5 and BM25 score.
- Semantic search embeds the query with Voyage and searches LanceDB vectors.
- Hybrid search uses LanceDB native hybrid search with vector and text query.
- LanceDB search filters to `active = true`.
- Retrieval joins or enriches video metadata as needed.

Known ranking gaps:

- Adjacent chunks from the same video can crowd out diverse results.
- Exact biomedical names may need synonym/alias handling.
- Queries with abbreviations can benefit from explicit lexical expansion.
- Hybrid result ordering needs a small benchmark suite before tuning.
- Search output should eventually support per-video caps and adjacent chunk collapse.

Recommended next ranking improvements:

1. Add retrieval evaluation fixtures with expected video ids/timestamps.
2. Add per-video caps.
3. Collapse adjacent chunks in default result presentation.
4. Add `--diversify` and `--dense` retrieval modes.
5. Tune hybrid retrieval from benchmark failures, not ad hoc impressions.

## Markdown Exports

Portable Markdown:

```bash
uv run yutome export portable-md
```

Output:

- `data/exports/portable-md/`
- One file per indexed video.
- YAML frontmatter.
- Source URL.
- Video id.
- Channel.
- Published date.
- Duration.
- Transcript source.
- Transcript version id.
- Language.
- Generated/manual flag.
- Description truncated to a bounded length.
- Timestamped transcript links.

Obsidian Markdown:

```bash
uv run yutome export obsidian
```

Output:

- `data/exports/obsidian/`
- One file per indexed video.
- YAML frontmatter compatible with Obsidian Properties.
- YouTube embed near the top.
- Timestamp links.
- Obsidian-compatible block ids on transcript bullets.

Design constraints:

- Portable YouTube timestamp links are the primary citation format.
- Obsidian output may add wikilink/block-id conveniences, but should not require Obsidian URI links or Web Clipper internals.
- Exports should be deterministic and rebuildable from SQLite plus transcript artifacts.
- Exports should work even without generated summaries/topic cards.

## Obsidian Notes

Obsidian Web Clipper 1.4 added interactive transcript-related capabilities, but this project should not depend on Web Clipper internals.

The stable Obsidian-compatible path is:

- Plain Markdown files.
- YAML frontmatter for properties.
- Standard Markdown links to YouTube timestamps.
- Optional YouTube embed syntax.
- Optional block ids for direct block references.

This makes exported files useful in:

- Obsidian.
- Git repositories.
- Static site generators.
- Local grep/ripgrep workflows.
- LLM/agent document loaders.

## LLM Summaries And Topic Cards

Summaries and topic cards are useful, but not required for the retrieval foundation.

Potential future artifacts:

```text
data/artifacts/videos/{video_id}/summaries/{summary_version}.json
data/artifacts/videos/{video_id}/topics/{topic_version}.json
```

Potential summary fields:

- `summary_version`
- `model`
- `prompt_version`
- `source_transcript_version_id`
- `source_chunker_version`
- `summary`
- `topic_tags`
- `claims`
- `citations`
- `created_at`

Why defer summaries:

- Retrieval correctness should not depend on generated summaries.
- Summary quality needs evaluation.
- Per-video summaries can become another stale artifact if not versioned carefully.
- A direct answer flow should first prove that `find`/`show context` is stable and citation-safe.

Useful later:

- Optional video-level summary cards for human browsing.
- Topic tag lists for faceted search.
- LLM-generated query expansion.
- Built-in answer synthesis that cites transcript timestamps.
- Metarational or domain-specific extraction lenses with explicit schemas, prompts, provenance, and evaluation.

## Built-In Answer Mode

The project does not currently include `yutome answer`.

Recommended future answer flow:

1. Run `find` in hybrid mode with thin projection.
2. Deduplicate adjacent chunks and cap per video.
3. Expand context for selected hits.
4. Synthesize from expanded transcript context only.
5. Cite timestamp URLs.
6. State when the retrieved context does not answer a question.
7. Include source transcript provenance in debug output.

Do not add answer synthesis before retrieval evaluation exists. Otherwise answer quality issues will be hard to separate from retrieval quality issues.

## Agent And Multi-Device Connector

The product promise is that a user's daily-driver LLM can query their YouTube corpus from the places they actually work. Local-first still matters for ownership and inspection, but laptop-only querying is too narrow: a useful corpus should be reachable from multiple agents, devices, and surfaces.

Decided direction:

- The first connector is a local **MCP server** over stdio.
- A thin local **HTTP/JSON API** is added underneath so scripts, future browser inspectors, and non-MCP clients share the same core.
- **Remote access** is promoted from "later integration" to a core product track. The likely shapes are hosted authenticated HTTP, remote MCP on top of the same API, or a hosted read-only replica of the local corpus/indexes.
- Both adapters call the same in-process Python functions that already back the CLI; no new orchestration logic lives in the connector layer.

### Architecture

```text
yutome CLI ────────────┐
                     │
MCP server (stdio) ──┼──> api.py verbs: find/list/show/q
                     │       query.py compiler/executor
HTTP API (localhost) ┘        SQLite + LanceDB + retrieval helpers
```

`QueryRequest` is the raw contract. Connector adapters must not invent new query semantics; they expose the same `find`, `list`, `show`, and `q` verbs so behavior stays consistent across CLI, MCP, and HTTP.

### Tool Catalog (MCP)

Tools are the verbs an agent can invoke. The first slice exposes a small, agent-safe set built from the shared query primitive:

| Tool | Backed by | Purpose |
| --- | --- | --- |
| `find` | `api.find` | Ranked hits across chunks, titles, or descriptions. Inputs include `text`, `mode`, `in_`, filters, grouping, and projection. |
| `list` | `api.list_` | Enumerate videos, channels, attention rows, or status breakdowns by filter. |
| `show` | `api.show` | Fetch chunk/video/channel/transcript resources or resolve `source`/`context` from citation anchors. |
| `q` | `api.q` | Execute a raw `QueryRequest` JSON object. |

Deferred to a later slice (need either a job model or evaluation work first):

- `sync` / `sync_channel` — long-running, needs the planned `jobs` table to be writable and a job-status tool. Exposing it synchronously over MCP would block the agent.
- `quality_upgrade` — same reason.
- `inspect_*` — wait until the planned `yutome inspect video|chunk|attempts` CLI commands land so MCP and CLI ship one definition.
- `answer` — gated on retrieval evaluation per the existing Built-In Answer Mode section.

### Resource Catalog (MCP)

Resources are addressable read-only artifacts. The URI scheme is already drafted in the Citation Model section:

| URI | Backed by | Returns |
| --- | --- | --- |
| `yutome://chunk/{chunk_id}` | `chunks` table | Full chunk text, timestamps, transcript provenance, citation URL. |
| `yutome://video/{video_id}` | `videos` table + active transcript | Video metadata, active transcript version, plain transcript text path, indexing status. |
| `yutome://channel/{channel_id}` | `channels` / `library_channels` | Channel metadata, local-library selection, and indexed counts. |
| `yutome://transcript/{transcript_version_id}` | `transcript_versions` + artifact path | Provenance, language, generated/manual, link to `normalized.jsonl` / `transcript.txt`. |

Resource bytes for transcripts come from the existing artifact files on disk; the connector never re-fetches from YouTube.

### Transport And Auth

| Surface | Transport | Auth | When |
| --- | --- | --- | --- |
| Local MCP | stdio | none (process-local) | First slice. |
| Local HTTP | HTTP on `127.0.0.1` | optional bearer token from `.env` | First slice, opt-in. |
| Hosted HTTP API | HTTPS | user auth + corpus ACL | Next architecture track. |
| Authenticated HTTP API | HTTP/HTTPS behind private network or reverse proxy | bearer token | Current multi-device slice. |
| Remote MCP | MCP streamable HTTP behind private network or reverse proxy | same bearer token | Current multi-device slice for agent clients. |

The local HTTP surface is bound to loopback by default. `yutome remote serve` and `yutome remote mcp` are the current authenticated remote-read shapes; both refuse non-loopback binding without `YUTOME_HTTP_TOKEN`. Public hosted remote access still needs per-user data isolation, rate limits, audit logging, and a decision about whether remote writes can start sync jobs or remote is read-only over already-indexed corpus data.

### Beginner Vs Expert Shape

Per the product-design split, connector outputs follow the same two-register rule as the CLI:

- Default tool output uses beginner vocabulary (channels, results, timestamps, "still indexing", "needs attention") and small provenance badges (`captions`, `auto-captions`, `llm-cleaned`, `gemini`, `asr`).
- Expert fields (`chunk_id`, `transcript_version_id`, `chunker_version`, `embedding_model`, raw provider attempt rows) are present in MCP responses but live under nested `debug`/`provenance` blocks so they don't crowd default agent reasoning.

### Rollout

Shipped (query/API slice):

1. `mcp` optional dependency group pinned to `mcp[cli]>=1.20,<2` in `pyproject.toml`.
2. `src/yutome/query.py` defines `QueryRequest`, projections, compiler, SQL/FTS/LanceDB execution, two-stage pushdown, and status breakdowns.
3. `src/yutome/api.py` exposes transport-neutral `find`, `list`, `show`, and `q` verbs plus resources.
4. `src/yutome/mcp_server.py` implements four tools and four resources wired through `api.py`.
5. `src/yutome/http_server.py` exposes the same verbs/resources as REST endpoints: `POST /find`, `POST /list`, `POST /show`, `POST /q`, `GET /chunks/{id}`, `GET /videos/{id}`, `GET /channels/{id}`, `GET /transcripts/{id}`, plus unauthenticated `GET /healthz` and authenticated `GET /readyz`.
6. `yutome mcp serve` CLI entrypoint runs FastMCP over stdio in the project venv so `yt-dlp`, LanceDB, and Voyage credentials are inherited.
7. `yutome http serve --config yutome.toml --port 8765` CLI entrypoint runs uvicorn on `127.0.0.1`.
8. Optional bearer auth via the `YUTOME_HTTP_TOKEN` env var for loopback HTTP; required for non-loopback remote serving.
9. Tests: `tests/test_mcp_server.py`, `tests/test_http_server.py`, `tests/test_retrieval_exports.py`, plus subprocess smoke helpers.
10. `yutome remote prepare|serve|mcp|check` for authenticated remote HTTP and MCP access from other devices/private agents.

Still to do:

11. Optional Claude skill bundling few-shot examples of good yutome queries (Scry-style).
12. Long-running tools (`sync`, `quality_upgrade`) once the `jobs` table is writable.
13. Remote MCP / ChatGPT connector shape with OAuth or app-issued tokens.

### Client Setup

For Claude Code, the project-scoped `.mcp.json` at the repo root is read automatically; Claude Code prompts once to enable it on first launch. To install as a user-scoped server (available from any working directory):

```bash
claude mcp add --scope user yutome -- \
  uv run --directory /absolute/path/to/yutome yutome mcp serve \
  --config /absolute/path/to/yutome/yutome.toml
```

For Claude Desktop, add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "yutome": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/yutome",
               "yutome", "mcp", "serve",
               "--config", "/absolute/path/to/yutome/yutome.toml"]
    }
  }
}
```

### Why Not Just HTTP, And Why Not Remote First

- Local MCP over stdio is what current daily-driver agent clients (Claude Desktop, Claude Code, Cursor) launch directly against a local binary, with no port exposure and no auth surface. It matches the local-first trust boundary.
- Plain HTTP alone does not give an agent a tool/resource catalog; the agent does not know what `/find` or `/q` mean unless something teaches it. MCP carries that catalog by design.
- Remote MCP (the ChatGPT connector shape) requires a public endpoint, OAuth, and per-user isolation. It is the right second adapter, not the first.

## Operations Runbook

Initialize:

```bash
uv run yutome init
uv run yutome doctor
```

Index or resume Leo and Longevity from catalog:

```bash
uv run yutome sync "https://www.youtube.com/@LeoandLongevity" --use-catalog --max-process 25
```

Run with embeddings:

```bash
uv run yutome sync "https://www.youtube.com/@LeoandLongevity" --use-catalog --max-process 25 --embed
```

Test provider/proxy paths:

```bash
uv run yutome proxy-test --video-id VIDEO_ID
```

Retry deferred rows:

```bash
uv run yutome sync "https://www.youtube.com/@LeoandLongevity" --use-catalog --retry-failed --status-filter "deferred: rate_limited" --max-process 10
```

Use Gemini fallback for known fallback rows:

```bash
uv run yutome sync "https://www.youtube.com/@LeoandLongevity" --use-catalog --retry-failed --gemini-fallback --max-process 5
```

Check status:

```bash
uv run yutome list status
```

Rebuild chunks from active normalized transcripts:

```bash
uv run yutome rebuild-chunks
```

Resume vector indexing for pending chunks:

```bash
uv run yutome rebuild-vectors --resume --batch-size 128 --concurrency 4
```

Full vector rebuild:

```bash
uv run yutome rebuild-vectors
```

Run retrieval checks:

```bash
uv run yutome find "Crohn probiotics" --mode hybrid --limit 5 --json
uv run yutome find "donepezil AChEI" --mode hybrid --limit 5 --json
uv run yutome find "neuroautoimmune disease" --mode hybrid --limit 5 --json
uv run yutome show context CHUNK_ID --token-budget 3000
```

Export:

```bash
uv run yutome export portable-md
uv run yutome export obsidian
```

Test:

```bash
uv run pytest -q
```

## Verification Targets

Expected current checks:

```bash
uv run yutome doctor
uv run yutome list status
uv run pytest -q
```

Expected current status shape:

```json
{
  "searchable_now": 742,
  "still_indexing": 9,
  "needs_attention": 3,
  "channels": 2,
  "videos": 754,
  "transcript_versions": 755,
  "chunks": 9143,
  "embeddings": 9080,
  "statuses": {
    "indexed": 742
  }
}
```

Expected tests:

```text
65 passed
```

LanceDB inspection:

```bash
uv run python - <<'PY'
import lancedb
t = lancedb.connect("data/indexes/lancedb").open_table("chunks")
print("rows", t.count_rows())
print("columns", t.schema.names)
print("indices", [getattr(i, "name", str(i)) for i in t.list_indices()])
PY
```

Expected:

- LanceDB row count agrees with the rebuilt vector corpus.
- Required columns listed in the LanceDB model section.
- FTS index present for `text`, typically `text_idx`.

## Test Plan

Current tests:

- Config parsing and path layout.
- SQLite schema/bootstrap.
- Thin retrieval result shape does not include full chunk text.
- Chunk detail includes full chunk text.
- Context expansion returns neighboring chunks within budget and dedupes overlaps.
- Hybrid retrieval errors cleanly when LanceDB schema/index is not ready.
- Oversized transcript segments are split under the max token cap.
- Portable and Obsidian Markdown exports produce timestamp links and YAML frontmatter.

Unit tests to add:

- Metadata normalization from representative `yt-dlp` payloads.
- Transcript normalization for `youtube-transcript-api`, `yt-dlp` JSON3, Gemini, and ASR-like segments.
- VTT/SRT/TXT/Markdown transcript rendering.
- Citation roundtrip from chunk to timestamp URL and back through `context`.
- SQLite FTS rebuild from chunks.
- Provider error classification for no captions, transient failures, language unavailable, rate limit, and bad captions.
- Proxy URL redaction and deterministic generic proxy pool selection.
- `yt-dlp` timeout classification.
- Embedding retry behavior with simulated 429/503 errors.

Integration tests to add:

- Ingest a tiny fixture channel or fixed list of 3 Leo videos.
- Rerun ingest idempotently.
- Export both Markdown modes.
- Search with JSON citations.
- Rebuild chunks from transcript artifacts.
- Rebuild vectors from SQLite chunks using a fake embedding provider.
- Simulate interrupted embedding rebuild and resume.

Failure tests to add:

- Missing captions.
- Mislabeled generated captions.
- ASR fallback path.
- Gemini fallback path.
- Interrupted backfill.
- Stale or missing `yt-dlp`.
- Transcript API 429/block.
- Proxy misconfiguration.
- Stale LanceDB schema.
- Missing Voyage credentials.
- Missing Gemini credentials when fallback is requested.

Scale tests to add:

- Synthetic thousands-video catalog.
- Millions-scale chunk metadata simulation.
- SQLite FTS query latency.
- LanceDB rebuild behavior.
- Export runtime and file count behavior for large channels.

## Design Decisions

### SQLite plus LanceDB

Use both because they solve different problems.

SQLite:

- Canonical metadata and status.
- Easy local inspection.
- FTS fallback.
- Attempt history.
- Rebuild source for chunks and vectors.

LanceDB:

- Vector search.
- Native hybrid search.
- Fast retrieval over embeddings.
- Rebuildable from SQLite and artifacts.

### Thin Retrieval plus Explicit Context

Default search results should stay small.

Reasons:

- Agents often call search repeatedly.
- Full chunks and descriptions quickly consume context.
- A thin result gives enough information to decide whether to expand.
- `context` gives exact transcript text only when selected.

### Plain Text plus Timestamped Formats

Both are required.

Plain text:

- Best for reading, grep, LLM context, and external archival use.
- Avoids timestamp noise.

Timestamped normalized JSONL/VTT/SRT/Markdown:

- Needed for citations.
- Needed for context windows.
- Needed for future interactive transcript UI.
- Needed to reconstruct or audit chunk provenance.

### Caption Providers before ASR

Captions and subtitle files are cheaper and usually faster than ASR.

ASR remains important for:

- No captions.
- Broken/mislabeled captions.
- Private fallback experiments.
- Higher-fidelity transcript replacement if quality is worth the compute cost.

But ASR should not be the default for a channel-scale import.

### Gemini as Optional Fallback

Gemini can process YouTube URLs and is valuable for missing or broken captions. It should remain an explicit fallback until cost, speed, limits, and transcript segmentation quality are better characterized.

### Versioned Transcripts and Chunks

Transcript versions prevent source changes from silently overwriting prior artifacts. Chunker versions let the system rebuild chunks and vectors without pretending that results from different chunking strategies are equivalent.

### Bounded Backfill Defaults

Channel-scale indexing should favor resumability over all-at-once throughput.

Defaults and targets:

- Moderate scheduled backfills.
- Small worker pool first.
- Batch limits for recurring jobs.
- Jittered delays.
- Explicit retry of deferred rows.

This makes the system usable on old channels with thousands of videos without requiring one giant fragile run.

### Future Scheduler As A Thin Wrapper

The scheduler should not own indexing logic. It should invoke the same bounded sync/index commands as a human would run manually, with a configured cadence and clear logs.

### Extraction Lenses Stay Versioned

Summaries, topic cards, entities, claims, supplement lists, diagnostic frameworks, and other extracted structures should be versioned lenses over transcript artifacts. They should not replace transcript retrieval as the source of truth.

## Assumptions

- No YouTube Data API in the current product.
- No comments in the current product.
- No Shorts in the default corpus.
- No related website crawl in the current product.
- No durable audio/video storage by default.
- LanceDB is the default local vector backend.
- SQLite remains the canonical catalog and job/status store.
- All vector indexes are rebuildable.
- Postgres/pgvector and Qdrant are later service-mode adapters.
- Summaries, topic cards, and extraction lenses require explicit schemas, prompts, provenance, and evaluation.
- Obsidian exports are plain Markdown/YAML first; Web Clipper behavior can inspire UX but should not be a dependency.

## Open Implementation Questions

Retrieval evaluation:

- What exact query set should become the acceptance benchmark?
- Which Leo videos/timestamps should be gold hits for Crohn/probiotics, AChEIs, neuroautoimmune disease, complex disease diagnosis, sodium butyrate, and lentils?
- What ranking metric is good enough for a first pass: top-5 contains expected video, top-10 contains expected timestamp neighborhood, or graded relevance?

Result shaping:

- Should default `find` collapse adjacent chunks from the same video?
- Should default `find` cap results to N chunks per video?
- Should `--dense` mean no diversification and `--diversify` mean per-video caps plus adjacent collapse?

Chunking:

- Is 700/100/1000 still optimal after retrieval evaluation?
- Should biomedical talks get sentence-aware or topic-aware chunks?
- Should exact tokenizer counts replace the current word-count estimate?

Provider strategy:

- How aggressively should proxy-backed runs increase concurrency?
- Should proxy rotation be per video, per provider attempt, or provider-managed?
- Should `yt-dlp` subtitle fallback run before transcript API by default after enough block evidence?
- Should Gemini fallback be run only for videos under a duration cap?
- What block/error threshold should trigger a provider-level circuit breaker?
- Should scheduled runs automatically pause a provider after repeated block signals?

Exports:

- Should exports include a short generated abstract when summaries exist?
- Should Obsidian exports include topic-tag properties later?
- Should file names be stable by video id first rather than title first?

Future application/API:

- Connector shape decided: local MCP first, thin local HTTP underneath, remote access promoted to a core architecture track. See the Agent And Multi-Device Connector section.
- Resource URIs decided: `yutome://chunk/...`, `yutome://video/...`, `yutome://transcript/...`. Segment-level URIs remain a future option.
- Should `show context` accept multiple chunk ids in one call so an agent can merge several hits without N round trips?
- Should the MCP server expose a long-running `sync` tool once the `jobs` table is writable, or stay read-only until then?
- What is the right beginner/expert split inside MCP responses (default beginner badges, expert details under `debug`)?
- What is the first remote shape: hosted read-only HTTP API, remote MCP adapter, or corpus/index sync to a private server?

Scheduler and catalog:

- Should channel registration be a first-class command before scheduler work?
- Should RSS be used only as a new-upload hint, or should it feed a separate lightweight watch path?
- Should jobs remain SQLite-only, or should long-running workers use an external queue in service mode?
- What manifest format should store extractor/tool versions?

## Next Work

Recommended immediate slice:

1. Finish guided setup and first-run checks.
2. Add retrieval evaluation fixtures.
3. Improve big-import progress/resume ergonomics.
4. Add per-video result caps.
5. Add adjacent chunk collapse.
6. Add `yutome inspect video VIDEO_ID`.
7. Add `yutome inspect chunk CHUNK_ID`.
8. Add `yutome inspect attempts VIDEO_ID`.
9. Design hosted read-only API / remote MCP architecture.

Why this order:

- The user-facing pain is answer quality and specificity.
- Answer quality depends first on retrieval quality.
- Ranking changes need tests.
- Inspect commands make debugging retrieval failures faster.
- Built-in answer synthesis can then be added on a stable retrieval/context base.

Suggested retrieval eval queries:

- `Crohn probiotics`
- `donepezil AChEI`
- `acetylcholinesterase inhibitors`
- `neuroautoimmune disease`
- `complex disease diagnosis`
- `lentils`
- `sodium butyrate`
- `mast cell activation`
- `small fiber neuropathy`
- `probiotics Crohn diet`

Shipped: local MCP server (`yutome mcp serve`) and local HTTP API (`yutome http serve`) sharing the same in-process handlers. See the Agent And Multi-Device Connector section.

Next slice:

- Optional Claude skill with few-shot yutome query examples.

Later slices:

- Optional per-video summaries/topic cards.
- Built-in `yutome answer`.
- Hosted API / remote MCP for multi-device access.
- Web UI or local browser transcript navigator.
- `yutome add`, `yutome sync --all`, and scheduler install/run commands.
- `yutome index --lexical --vectors` as a unified rebuild command.
- Incremental scheduler for new channel videos.
- Topic/entity extraction.
- Tool/provider version manifests.

## External References

LanceDB:

- Hybrid search: [https://docs.lancedb.com/search/hybrid-search](https://docs.lancedb.com/search/hybrid-search)
- Full-text search: [https://docs.lancedb.com/search/full-text-search](https://docs.lancedb.com/search/full-text-search)
- Filtering: [https://docs.lancedb.com/search/filtering](https://docs.lancedb.com/search/filtering)
- Query optimization: [https://docs.lancedb.com/search/optimize-queries](https://docs.lancedb.com/search/optimize-queries)
- Table updates / merge-insert: [https://docs.lancedb.com/tables/update](https://docs.lancedb.com/tables/update)

Agent/RAG/tool design:

- MCP resources: [https://modelcontextprotocol.io/specification/2025-06-18/server/resources](https://modelcontextprotocol.io/specification/2025-06-18/server/resources)
- MCP tools: [https://modelcontextprotocol.io/specification/2025-06-18/server/tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- Anthropic contextual retrieval: [https://www.anthropic.com/engineering/contextual-retrieval](https://www.anthropic.com/engineering/contextual-retrieval)
- Anthropic search results: [https://docs.anthropic.com/en/docs/build-with-claude/search-results](https://docs.anthropic.com/en/docs/build-with-claude/search-results)
- OpenAI file search: [https://platform.openai.com/docs/guides/tools-file-search/](https://platform.openai.com/docs/guides/tools-file-search/)

Obsidian:

- Web Clipper help: [https://obsidian.md/help/web-clipper](https://obsidian.md/help/web-clipper)
- Web Clipper releases: [https://github.com/obsidianmd/obsidian-clipper/releases](https://github.com/obsidianmd/obsidian-clipper/releases)
- Internal links and block IDs: [https://obsidian.md/help/links](https://obsidian.md/help/links)
- Properties/YAML frontmatter: [https://obsidian.md/help/properties](https://obsidian.md/help/properties)

Gemini:

- Video understanding: [https://ai.google.dev/gemini-api/docs/video-understanding](https://ai.google.dev/gemini-api/docs/video-understanding)
- Models: [https://ai.google.dev/gemini-api/docs/models](https://ai.google.dev/gemini-api/docs/models)
- Rate limits: [https://ai.google.dev/gemini-api/docs/rate-limits](https://ai.google.dev/gemini-api/docs/rate-limits)

YouTube transcript tooling:

- `yt-dlp`: [https://github.com/yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp)
- `youtube-transcript-api`: [https://github.com/jdepoix/youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api)

Related project:

- Matthew Siu Latticework: [https://www.matthewsiu.com/Latticework](https://www.matthewsiu.com/Latticework)

## Fresh Review Prompt

```text
Read docs/plan.md, docs/proxy-strategy.md, and docs/reviewer-handoff.md. Inspect the code paths named in the Code Map. Review retrieval/context/export correctness, ranking quality, context-bloat controls, rebuild reliability, provider fallback behavior, and the next implementation slice. Report findings first and include file/line references where relevant.
```
