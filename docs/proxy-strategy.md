# Proxy Strategy For YouTube Transcript Fetching

## Recommendation

Use local residential IP first with slow, resumable batches. Add a paid rotating residential proxy only after sustained `RequestBlocked`, `IpBlocked`, 403, or 429 failures.

Do not use free proxy lists for real indexing. They are unreliable, commonly abused, frequently blocked, and can expose traffic to interception or manipulation.

## Default Order

1. `youtube-transcript-api` with preferred language only, usually `en`.
2. `yt-dlp` subtitle fallback with `--skip-download`, `--write-subs`, `--write-auto-subs`, `--sub-langs en`, `--sleep-subtitles`, and retry sleep.
3. Defer non-preferred generated captions as likely mislabeled/foreign caption tracks unless translated captions are explicitly enabled.
4. Optional Gemini video-understanding fallback when a Gemini key is configured and the run explicitly enables it.
5. Deferred retry after a cooldown when block/rate-limit signals appear.
6. Optional rotating residential proxy.
7. ASR only as an explicit last resort.

## Proxy Options

- Best default: no proxy on a local home IP.
- First paid option: Webshare rotating residential. The transcript library supports `WebshareProxyConfig` directly.
- Backup paid options: Decodo/Smartproxy, Rayobyte, IPRoyal, PacketStream, Oxylabs, Bright Data.
- Avoid: free proxies, datacenter proxies for bulk caption scraping, static residential proxies for long runs, and Google account cookies in the bulk path.
- ASR audio downloads bypass residential proxies by default to preserve bandwidth. Enable `proxy.use_for_asr_audio` only when direct media downloads are also blocked.

## Local Configuration

Copy `.env.example` to `.env`.

Generic proxy:

```text
YTKB_PROXY_URLS=http://user:pass@host1:port,socks5://user:pass@host2:port
YTKB_HTTP_PROXY=http://user:pass@host:port
YTKB_HTTPS_PROXY=http://user:pass@host:port
```

Webshare:

```text
YTKB_WEBSHARE_USERNAME=...
YTKB_WEBSHARE_PASSWORD=...
YTKB_WEBSHARE_DOMAIN=p.webshare.io
YTKB_WEBSHARE_PORT=80
```

Gemini fallback:

```text
GEMINI_API_KEY=...
```

`ytkb` keeps proxy config disabled by default in `ytkb.toml`; environment variables can enable a local proxy profile without committing secrets.

## Operational Policy

- Commit after each video.
- Record transcript attempts per tool.
- Treat 429/IP-block/CAPTCHA as retryable deferred states, not permanent failures.
- Stop the run on rate limits by default.
- Resume with `--max-process` for small batches.
- Retry deferred items only with `--retry-failed`.
- Run `ytkb proxy-test` against one video after changing proxy config.
- Keep `yt-dlp` subprocesses under a fixed timeout so a hung provider request becomes a retryable per-video outcome.
- For fastest single-command bulk import, keep metadata deferred and use `--staged-fallback`; the run first sweeps with `youtube-transcript-api`, queues unresolved rows, then immediately drains that queue with `yt-dlp` fallback. Logs print explicit stage banners and queue counts.
- Use `--no-yt-dlp-fallback` only for a transcript-API-only diagnostic pass where unresolved rows should remain queued for a later run.
- When `youtube-transcript-api` is broadly hitting Google `/sorry`, resume with `--yt-dlp-first` to use `yt-dlp` subtitle files before the transcript API.
