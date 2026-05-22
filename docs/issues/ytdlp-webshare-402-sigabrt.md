# Issue: yt-dlp aborts on Webshare 402 proxy failures and Yutome should surface proxy exhaustion clearly

## Summary

During a 100-video Visa CLI indexing run, `yt-dlp` metadata fetches began failing with exit code `-6` and no stderr/stdout for multiple videos. Reproduction shows the root trigger is the configured Webshare proxy returning `402 Payment Required` during CONNECT. When `yt-dlp` runs with `--impersonate chrome`, the curl-cffi transport can abort the child Python process with `SIGABRT` instead of returning a normal extractor error.

Yutome should treat this as a proxy/account/quota failure, stop or defer cleanly, and tell the user to check the proxy plan/traffic/credentials instead of presenting an opaque Python subprocess crash.

## Environment

- Repo: `/Users/sheikmeeran/yt-indexer`
- Python: `3.12.11`
- `yt-dlp`: `2026.03.17`
- `curl_cffi`: `0.14.0`
- Proxy config:
  - `proxy.enabled = true`
  - `proxy.kind = "webshare"`
  - `proxy.use_for_metadata = true`
  - `proxy.use_for_discovery = false`
  - `yt_dlp.impersonate = "chrome"`
- Channel under test: Visa, `UCvqKb7KRw1vbkYiJihoYE8w`

## Reproduction

Start from a config with Webshare credentials loaded from `.env`, then run a metadata fetch through the project code path:

```bash
uv run python - <<'PY'
from pathlib import Path
from dotenv import load_dotenv
from yutome.config import load_config
from yutome.env import apply_env_to_config
from yutome.paths import ProjectPaths
from yutome.youtube import fetch_video_metadata, redact_proxy_secrets, describe_proxy

root = Path(".").resolve()
load_dotenv(root / ".env")
config = apply_env_to_config(load_config(root / "yutome.toml"))
paths = ProjectPaths.from_config(config, project_root=root)
print("proxy:", describe_proxy(config.proxy))

for video_id in ["m1vUbl2Z5xM", "rbT6fGBR0d8", "phK9caSRgPU"]:
    try:
        row = fetch_video_metadata(video_id=video_id, cwd=paths.root, proxy=config.proxy, ytdlp_config=config.yt_dlp)
        print(video_id, "OK", row.get("title"))
    except Exception as exc:
        print(video_id, "ERR", redact_proxy_secrets(config.proxy, str(exc), key=video_id)[:300])
PY
```

Observed before the fix:

```text
proxy: webshare rotating residential (p.webshare.io:80)
m1vUbl2Z5xM ERR yt-dlp metadata fetch failed for m1vUbl2Z5xM: exit code -6 with no stderr/stdout
rbT6fGBR0d8 ERR yt-dlp metadata fetch failed for rbT6fGBR0d8: exit code -6 with no stderr/stdout
phK9caSRgPU ERR yt-dlp metadata fetch failed for phK9caSRgPU: exit code -6 with no stderr/stdout
```

The lower-level option matrix isolated the proxy condition:

```text
no proxy, no impersonate: rc=0
no proxy, --impersonate chrome: rc=0
Webshare proxy, no impersonate: rc=1, stderr includes "Tunnel connection failed: 402 Payment Required"
Webshare proxy, --impersonate chrome: rc=-6; sometimes stderr includes curl 402, sometimes stderr is empty
```

`uv run yutome proxy-test --video-id m1vUbl2Z5xM --no-yt-dlp` also reproduced the active proxy condition:

```text
[WARN] youtube-transcript-api - ... ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 402 Payment Required'))
```

## Impact

- A noob user sees an opaque `yt-dlp`/Python crash-like failure instead of an account or quota action.
- Sync can continue spending work on a queue even though the configured proxy is not currently usable.
- Metadata backfill leaves videos in partial `metadata`/deferred states even when transcripts and chunks were already written.
- The same proxy condition can affect both transcript API and `yt-dlp`; only one path may show a clear `402`.

## Likely Root Cause

Webshare is rejecting CONNECT with `402 Payment Required`, likely because traffic, account entitlement, target-site access, or payment state is exhausted. With `yt-dlp --impersonate chrome`, the request goes through curl-cffi; in this failure mode curl-cffi/`yt-dlp` may abort the child interpreter with `SIGABRT` (`returncode = -6`) rather than returning a normal Python exception.

This is distinct from:

- YouTube transcript rate limiting (`429`, bot challenge, CAPTCHA, `/sorry/`)
- Missing captions/subtitles
- A bad video ID
- Discovery being unproxied

## Implemented Mitigation

- Classify proxy `402 Payment Required` separately from YouTube rate limits.
- Format negative `yt-dlp` return codes as subprocess signal failures, for example `SIGABRT (return code -6)`.
- When proxy 402 is visible, report it as a proxy account/quota/credentials problem.
- When `yt-dlp` aborts with no output while a proxy is configured, tell the user to run `yutome proxy-test` and check proxy quota/credentials.
- Mark transcript-path proxy 402 as `deferred: proxy_payment_required` and treat it as stop-worthy for rate-limit guard behavior.

## Remaining Work

1. Add a startup preflight for `sync` when `proxy.enabled = true`:
   - Run a cheap transcript API check.
   - Run a cheap `yt-dlp` metadata check if `proxy.use_for_metadata = true`.
   - Fail before large runs if the proxy returns 402.

2. Add a sync-level circuit breaker:
   - If N videos in a row return `proxy_payment_required`, stop submitting work even without `--stop-on-rate-limit`.
   - Print a final summary with the proxy diagnosis.

3. Add a safer metadata backfill strategy:
   - If Webshare 402 appears during metadata, retry direct/no-proxy once if allowed.
   - Otherwise skip remaining metadata backfill and preserve already-indexed transcript status.

4. Investigate curl-cffi/yt-dlp behavior:
   - Reduce to a minimal `yt-dlp --proxy ... --impersonate chrome --dump-json` repro.
   - File upstream only if the proxy account is valid and the abort persists on a non-402 proxy error.

5. Improve docs:
   - Document that Webshare `402` means the proxy service rejected the request before YouTube.
   - Add troubleshooting steps: check Webshare dashboard, credentials, plan, traffic, target-site access, then rerun `yutome proxy-test`.

## Acceptance Criteria

- `yutome proxy-test` prints a clear proxy 402 diagnosis without exposing credentials.
- `yutome sync` does not show bare `exit code -6 with no stderr/stdout` for `yt-dlp` crashes.
- Repeated proxy 402 failures stop or pause bulk sync before burning the entire queue.
- Existing YouTube rate-limit and missing-caption classifications continue to work.
- Tests cover:
  - proxy 402 classification
  - `yt-dlp` `SIGABRT` formatting
  - redaction of proxy credentials in diagnostics
  - sync deferral status for proxy payment failures

## References

- Webshare proxy troubleshooting: https://help.webshare.io/en/articles/6089292-proxy-connection-issues
- HTTP `402 Payment Required`: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/402
