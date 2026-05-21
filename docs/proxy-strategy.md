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

Run `yutome setup` for the guided path. In interactive mode it can write the Webshare environment keys into `.env` without printing credentials. For manual setup, copy `.env.example` to `.env`.

Generic proxy:

```text
YUTOME_PROXY_URLS=http://user:pass@host1:port,socks5://user:pass@host2:port
YUTOME_HTTP_PROXY=http://user:pass@host:port
YUTOME_HTTPS_PROXY=http://user:pass@host:port
```

Webshare:

```text
YUTOME_WEBSHARE_USERNAME=...
YUTOME_WEBSHARE_PASSWORD=...
YUTOME_WEBSHARE_DOMAIN=p.webshare.io
YUTOME_WEBSHARE_PORT=80
```

Gemini fallback:

```text
GEMINI_API_KEY=...
```

`yutome` keeps proxy config disabled by default in `yutome.toml`; environment variables can enable a local proxy profile without committing secrets.

## Operational Policy

- Commit after each video.
- Record transcript attempts per tool.
- Treat 429/IP-block/CAPTCHA as retryable deferred states, not permanent failures.
- Stop the run on rate limits by default.
- Resume with `--max-process` for small batches.
- Retry deferred items only with `--retry-failed`.
- Run `yutome proxy-test` against one video after changing proxy config.
- Keep `yt-dlp` subprocesses under a fixed timeout so a hung provider request becomes a retryable per-video outcome.
- `sync` uses a staged provider policy by default: transcript API first, unresolved queue second with `yt-dlp` fallback, then exact metadata backfill. Logs print explicit stage banners and queue counts.
- Use `proxy-test` for provider/proxy diagnostics instead of changing normal `sync` provider order.
