# OAuth Testing

`yutome corpus import-youtube` imports YouTube subscriptions. With no channel argument it tries local browser cookies first and falls back to a local OAuth desktop flow for the YouTube Data API read-only subscription scope when `YUTOME_YOUTUBE_OAUTH_CLIENT_SECRETS` is configured.

The browser-cookie path uses the active YouTube account in the browser profile it can read. On macOS, yt-dlp may trigger a password or Touch ID prompt to decrypt Chrome cookie storage. If the returned subscription count looks wrong, use OAuth to target a specific Google account.

## Unit-Level Checks

These should run in normal CI and do not require Google credentials:

- Parse Google client-secret JSON in `installed`, `web`, or flat shape.
- Generate an authorization URL with:
  - `https://www.googleapis.com/auth/youtube.readonly`
  - PKCE `code_challenge`
  - `access_type=offline`
  - localhost redirect URI
- Treat cached tokens as valid only when an access token exists and `expires_at` is still in the future.

## Live Smoke Test

Use a Google Cloud OAuth client configured as a Desktop app.

1. Enable the YouTube Data API v3 in the Google Cloud project.
2. Create OAuth client credentials for a Desktop app.
3. Download the client-secret JSON locally. Do not commit it.
4. Add the path to `.env`:

```bash
YUTOME_YOUTUBE_OAUTH_CLIENT_SECRETS=/path/to/client_secret.json
```

5. Run:

```bash
uv run yutome --config yutome.toml corpus import-youtube
```

Expected result:

- Browser opens a Google consent screen.
- Scope shown is read-only YouTube access.
- Callback lands on `127.0.0.1`.
- Token is written under `data/auth/youtube-oauth-token.json` with `0600` permissions.
- `yutome search list channels` shows imported subscriptions.

For terminal-only environments:

```bash
uv run yutome --config yutome.toml corpus import-youtube \
  --print-url
```

Open the printed URL manually in a browser.

## Hosted Dashboard Smoke Test

Use a Google Cloud OAuth client configured as a Web application. Add the hosted dashboard callback as an authorized redirect URI:

```text
https://app.getyutome.com/auth/google/callback
https://app.getyutome.com/dashboard/youtube/callback
```

For local development, add the matching localhost callback for the dev server you use. Configure the hosted Python API with:

```bash
YUTOME_GOOGLE_OAUTH_CLIENT_ID=...
YUTOME_GOOGLE_OAUTH_CLIENT_SECRET=...
YUTOME_YOUTUBE_OAUTH_CLIENT_ID=...
YUTOME_YOUTUBE_OAUTH_CLIENT_SECRET=...
```

`YUTOME_GOOGLE_OAUTH_*` is for account sign-in and requests only `openid email profile`. `YUTOME_YOUTUBE_OAUTH_*` is for the explicit dashboard YouTube connection and requests `https://www.googleapis.com/auth/youtube.readonly`. The sign-in settings fall back to the YouTube OAuth client env vars for local/dev convenience, but production should set the Google identity env vars explicitly so account auth and source-discovery auth stay conceptually separate.

Expected result:

- Signup shows Sign in with Google and returns to `/auth/google/callback` with a normal account session.
- Dashboard shows the YouTube subscriptions card as connectable.
- Account sign-in consent requests only `openid email profile`; YouTube connection consent requests only `https://www.googleapis.com/auth/youtube.readonly`.
- Returning to `/dashboard/youtube/callback` stores a YouTube grant.
- Selecting subscribed channels imports them as public channel sources and queues `discover_source` jobs.

## What We Do Not Mock

The Google consent page, account policy, quota behavior, and real subscription listing should be verified with a live smoke test. Mocking those gives false confidence because most OAuth breakage is in redirect URI setup, consent-screen publishing state, account restrictions, or API enablement.
