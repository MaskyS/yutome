# yt-dlp Webshare Decision Log

Last updated: 2026-05-27

This note records the current Yutome `yt-dlp` research, benchmark results,
known pitfalls, and production decisions for YouTube indexing. It is the durable
reference for future hosted indexing work; Beads remains the task tracker for
implementation.

## Decision

Use Webshare for hosted YouTube indexing and make `python-no-js` the default
hosted `yt-dlp` profile:

```text
--no-js-runtimes --no-remote-components
```

Keep the current/full `yt-dlp` profile as the fallback after bounded attempts.
Do not make `youtube:player_skip=js` the hosted default yet.

Reasons:

- The production Webshare benchmark found `python-no-js` and `player_skip=js`
  essentially tied on total runtime.
- `player_skip=js` had more raw-attempt failures, especially YouTube
  "page needs to be reloaded" errors.
- `python-no-js` avoids dependence on Node, Deno, Bun, QuickJS, or remote
  JavaScript components in hosted workers.
- The current/full profile is about 2x slower in the production Webshare matrix.

Hosted is still pre-production, so no hosted backwards compatibility shim is
required for this change. Local behavior can remain configurable, but hosted
should optimize for a simple production contract.

## Hosted JavaScript Runtime Note

Last checked: 2026-05-27.

Railway's current Railpack docs list Deno among supported languages and say the
builder detects the repository language/framework, installs the appropriate
runtime and dependencies, and allows custom build configuration:

- https://docs.railway.com/builds/railpack
- https://docs.railway.com/languages-frameworks

That is not a good enough reason to make hosted `yt-dlp` depend on Deno, Node,
Bun, QuickJS, or remote JavaScript components. Yutome's hosted worker is a
Python service, and the benchmarked `python-no-js` profile removes this
dependency while matching `player_skip=js` runtime in the production Webshare
matrix. If a future extractor change requires a JavaScript runtime, add it
explicitly in the deployment image and rerun the production Webshare matrix
before changing the default profile.

## Current Code Paths

`yt-dlp` is used in `src/yutome/youtube.py` for three YouTube paths:

- `discover_videos`: channel/playlist discovery with `--flat-playlist`,
  `--dump-json`, and `youtubetab:approximate_date=1`.
- `fetch_video_metadata`: exact watch-page metadata with `--dump-json`.
- `fetch_subtitle_transcript_with_ytdlp`: JSON3 subtitle fallback with
  `--write-auto-subs`, `--write-subs`, `--sub-langs`, and `--sub-format json3`.

`youtube-transcript-api` is still the primary transcript provider. It does not
replace `yt-dlp` discovery or exact watch-page metadata extraction:

- It can fetch/list transcripts for a known video.
- It does not enumerate channel videos.
- It does not provide the full video-level extractor metadata we use for exact
  `published_at`, title, channel, duration, live status, thumbnail URL, and
  selected metadata JSON.

## yt-dlp Metadata Dependencies

The `yt-dlp` README metadata-related extras such as `mutagen`,
`AtomicParsley`, `xattr`, `pyxattr`, and `setfattr` are not relevant to the
current Yutome indexing path. They are for media post-processing:

- embedding thumbnails into downloaded media files
- writing extended attributes
- tagging MP4/M4A or other downloaded formats

Yutome does not download or tag media files for normal indexing. The relevant
`yt-dlp` metadata path is `--dump-json` extraction, not media post-processing.

## Hosted and Local Proxy Policy

Implemented policy:

- Hosted YouTube indexing requires Webshare before external YouTube fetches.
- Hosted discovery, exact metadata, transcript API, and `yt-dlp` subtitle paths
  route through Webshare regardless of local discovery/metadata flags.
- Hosted rows store selected video metadata without bulky raw extractor payloads.
- Local Webshare credentials auto-enable metadata proxying unless explicitly
  disabled; local discovery remains opt-in.

Why Webshare exists in the design:

- Direct requests may work in local short tests, but hosted/shared IPs are
  expected to hit YouTube rate limits and bot checks.
- Webshare rotating residential IPs are the production mitigation, so raw direct
  success is not a reason to remove hosted proxying.
- Direct/no-proxy fallback should not be the hosted production policy.

## Hosted Metadata Shape

Hosted exact metadata should store useful video-level fields and avoid bulky or
volatile extractor payloads.

Columns populated from `yt-dlp`:

- `title`
- `description`
- `published_at`
- `duration_seconds`
- `channel_id`

`published_at` parsing preference:

1. `upload_date`
2. `release_date`
3. `modified_date`
4. numeric `timestamp`

The stored value should be timezone-aware UTC.

Selected `metadata_json` fields:

- `source`
- `channel_title`
- `channel_handle`
- `playlist_tab`
- `thumbnail_url`
- `webpage_url`
- `live_status`
- `upload_date`
- `release_date`
- `timestamp`
- `metadata_hash`

Do not store bulky or unstable raw keys in hosted rows:

- `formats`
- `requested_formats`
- `subtitles`
- `automatic_captions`
- `heatmap`
- full thumbnail arrays
- HTTP headers
- volatile counters

## Subtitle Language Finding

The earlier benchmark default used `en-orig`. That was wrong for production
English coverage.

Live Webshare metadata probes for videos with zero JSON3 segments showed that
English captions were advertised under plain `en`, not `en-orig`:

- `ldxFjLJ3rVY`: manual English and auto English exposed as `en`.
- `Gqy1E5piq1w`: manual English and auto English exposed as `en`.

Using only `en-orig` caused successful `yt-dlp` exits with no JSON3 file or no
text segments. That made raw process success look better than transcript
success.

Runtime and benchmark policy:

- Try `en` first.
- Try `en-orig` only after `yt-dlp` exits successfully but writes no usable
  English JSON3 text.
- On process failures such as `429`, bot checks, or page reload errors, retry
  the same language through a rotated Webshare attempt instead of switching
  language.
- A subtitle attempt is production-successful only when it writes at least one
  JSON3 file and the parsed segment count is greater than zero.

## Benchmark Method

The raw matrix and production matrix answer different questions.

Raw matrix:

- Runs each operation/profile/proxy combination once per scheduled repetition.
- Records failures as failures.
- Does not retry failed cells until success.
- Useful for comparing failure modes and raw extractor behavior.

Production Webshare matrix:

- Webshare only.
- Bounded attempts per production case.
- Measures time-to-success including failed attempts.
- Treats empty subtitle output as failure.
- Retries the same language/profile after retryable process failures.
- Switches subtitle language only after successful-but-empty output.

Benchmark pitfalls observed:

- Do not count `returncode=0` subtitle calls as usable unless JSON3 files and
  nonzero text segments exist.
- Do not hide reliability by retrying raw benchmark cells until green.
- Do not compare direct local results to hosted policy as if direct hosted
  requests were acceptable; hosted expects rate limits.
- The first warmup run is not representative.
- Variant order must be randomized per repetition.
- Failure rows need redacted stdout/stderr tails, not just byte counts.

## Benchmark Results

Environment for the latest benchmark series:

- OS: macOS 14.4.1 arm64
- Python: 3.12.11
- `yt-dlp`: 2026.03.17
- `curl_cffi`: available
- JavaScript runtimes on PATH: Bun, Deno, Node; QuickJS absent
- Webshare proxy: `p.webshare.io:80`

Videos used:

- `ldxFjLJ3rVY` - 3Blue1Brown
- `SVTPv4sI_Jc` - Veritasium
- `Gqy1E5piq1w` - TED
- `9GSDvO0LFFE` - MKBHD
- `-14t6_yu-7w` - Computerphile

Raw five-video Webshare matrix, measured rows only:

```text
metadata   current         10/10 success, total 134.19s
metadata   player-skip-js   9/10 success, total 42.50s
metadata   python-no-js     9/10 success, total 42.12s
subtitles  current         10/10 success, total 131.15s
subtitles  player-skip-js   8/10 success, total 59.01s
subtitles  python-no-js    10/10 success, total 60.63s
discovery  current          3/3 success, total 8.01s
discovery  player-skip-js   3/3 success, total 4.99s
discovery  python-no-js     3/3 success, total 4.98s
```

Raw Webshare failures included:

- `429 Too Many Requests`
- YouTube "Sign in to confirm you're not a bot"
- YouTube "The page needs to be reloaded"

Production Webshare matrix, bounded to four attempts, measured rows only:

```text
discovery  current          3/3 success, total 13.31s
discovery  player-skip-js   3/3 success, total 14.00s
discovery  python-no-js     3/3 success, total 15.25s
metadata   current         10/10 success, total 223.33s
metadata   player-skip-js  10/10 success, total 103.20s
metadata   python-no-js    10/10 success, total 90.23s
subtitles  current         10/10 success, total 272.88s
subtitles  player-skip-js  10/10 success, total 140.35s
subtitles  python-no-js    10/10 success, total 154.28s
```

Overall production Webshare totals:

```text
current         23 rows, 23/23 success, total 509.52s, avg 22.15s
player-skip-js 23 rows, 23/23 success, total 257.55s, avg 11.20s
python-no-js   23 rows, 23/23 success, total 259.76s, avg 11.29s
```

For metadata + subtitles, excluding discovery:

```text
current         total 496.21s
player-skip-js total 243.55s
python-no-js   total 244.51s
```

Only one production row needed a retry:

- `9GSDvO0LFFE`, subtitles, `python-no-js`
- attempt 1: `429 Too Many Requests`
- attempt 2: success with 439 JSON3 text segments

All 30 production subtitle rows produced nonzero English segments:

```text
ldxFjLJ3rVY   722 segments
SVTPv4sI_Jc   516 segments
Gqy1E5piq1w   416 segments
9GSDvO0LFFE   439 segments
-14t6_yu-7w   396 segments
```

## Error Taxonomy

Important failure classes:

- `youtube_rate_limited`: `429`, "Too Many Requests", rate-limit text.
- `youtube_block`: captcha, bot check, sign-in challenge, `/sorry/`.
- `youtube_page_reload`: YouTube "The page needs to be reloaded".
- `proxy_payment_required`: Webshare or proxy `402 Payment Required`.
- `signal_6` / `SIGABRT`: child process abort, observed with Webshare
  `402` plus `--impersonate chrome`/curl-cffi in earlier debugging.

Redacted diagnostic tails are required for future benchmarks. Byte counts and
coarse classes are not enough to diagnose why a row failed.

## Runtime Gaps

Already implemented:

- Hosted Webshare fail-fast guard before external YouTube fetches.
- Hosted selected metadata shape and published-date parsing.
- Local Webshare credential auto-enables metadata proxying with explicit opt-out.
- Benchmark failure diagnostics with redacted stdout/stderr tails.
- Production benchmark mode with bounded Webshare attempts.
- Runtime English subtitle fallback now tries `en` before `en-orig`.

Remaining implementation work:

1. Runtime profile selection is not centralized in production code.
   `scripts/benchmark_ytdlp_runtime.py` has profiles, but
   `src/yutome/youtube.py` still builds the production command directly from
   `YtDlpConfig`.

2. Hosted Webshare runtime calls do not yet default to `python-no-js`.
   The default command still uses current/full `yt-dlp` behavior with optional
   impersonation and optional remote components.

3. The benchmarked production retry semantics are not fully applied to
   metadata/discovery/subtitle runtime calls.

4. The old inline metadata comment about `player_skip=js` explains why it
   should not be the Webshare default, but it should be updated after the
   `python-no-js` runtime profile lands.

## Implementation Plan

Beads follow-up issues:

- `yt-indexer-qhb`: Runtime: default hosted yt-dlp Webshare calls to
  `python-no-js` profile.
- `yt-indexer-5tu`: Runtime: add production Webshare retry policy for `yt-dlp`.
- `yt-indexer-ar9`: Hosted smoke: verify production `yt-dlp` Webshare profile
  after runtime change.

Plan:

1. Add a centralized `yt-dlp` runtime profile abstraction.
   - Profiles: `current`, `python-no-js`, `player-skip-js`.
   - Hosted Webshare default: `python-no-js`.
   - Fallback profile: `current`.
   - Keep `player-skip-js` as benchmark/optional, not default.

2. Route production command construction through that abstraction.
   - Metadata, discovery, and subtitle paths should use the same profile code.
   - Benchmark variants should reuse or mirror the same profile names.
   - Tests should assert actual command args.

3. Add bounded production retry behavior to runtime paths.
   - Retry retryable process failures with the same profile/language.
   - Retry with a new Webshare attempt so rotation has a chance to work.
   - For subtitles, switch from `en` to `en-orig` only after successful empty
     output.
   - For profile fallback, try `current` only after the default profile exhausts
     attempts or returns a validation failure that suggests degraded extraction.

4. Preserve hosted accounting and diagnostics.
   - Hosted UsageGate and Webshare accounting should observe each subprocess
     attempt.
   - Failures must preserve redacted diagnostic tails and stable error classes.

5. Run targeted tests and a real Webshare smoke.
   - Unit tests for command profiles and retry state.
   - Hosted indexing tests for Webshare-required behavior.
   - Live production Webshare matrix on 3-5 videos outside CI.

## Reproduction Commands

Raw multi-video matrix:

```bash
uv run python scripts/benchmark_ytdlp_runtime.py \
  --video-id ldxFjLJ3rVY \
  --video-id SVTPv4sI_Jc \
  --video-id Gqy1E5piq1w \
  --video-id 9GSDvO0LFFE \
  --video-id=-14t6_yu-7w \
  --operation metadata \
  --operation subtitles \
  --proxy-mode both \
  --repetitions 2 \
  --warmups 1 \
  --seed 20260527 \
  --timeout-seconds 240 \
  --output /tmp/yutome-ytdlp-multivideo-matrix-rerun.jsonl
```

Production Webshare matrix:

```bash
uv run python scripts/benchmark_ytdlp_runtime.py \
  --production-webshare \
  --video-id ldxFjLJ3rVY \
  --video-id SVTPv4sI_Jc \
  --video-id Gqy1E5piq1w \
  --video-id 9GSDvO0LFFE \
  --video-id=-14t6_yu-7w \
  --operation metadata \
  --operation subtitles \
  --repetitions 2 \
  --warmups 0 \
  --seed 20260527 \
  --timeout-seconds 240 \
  --production-attempts 4 \
  --output /tmp/yutome-ytdlp-production-webshare-multivideo.jsonl
```

Production Webshare discovery sweep:

```bash
uv run python scripts/benchmark_ytdlp_runtime.py \
  --production-webshare \
  --operation discovery \
  --repetitions 3 \
  --warmups 0 \
  --seed 20260527 \
  --timeout-seconds 180 \
  --production-attempts 4 \
  --output /tmp/yutome-ytdlp-production-webshare-discovery.jsonl
```

## Related Notes

- `docs/issues/ytdlp-webshare-402-sigabrt.md` records the earlier Webshare
  `402` / curl-cffi `SIGABRT` issue.
- `scripts/benchmark_ytdlp_runtime.py` is the repeatable benchmark harness.
- Live benchmark JSONL files are local artifacts under `/tmp`; summarize results
  in this document or Beads before assuming they will persist.
