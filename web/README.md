# Yutome Web Operator Guide

This is the hosted Yutome account app: React Router v7 SSR running on
Cloudflare Workers. It handles account sign-in, assistant/YouTube connection,
source import handoffs, billing handoffs, and read-only library/search views.

It is a BFF over the hosted Python API. The browser talks to this Worker; the
Worker talks to the Python API's `/account/*` routes.

## Pages

Routes are defined in `app/routes.ts`.

- `/` - landing page.
- `/signup` - email magic-link sign-in and Google sign-in entry.
- `/auth/verify` - redeems email sign-in tokens and sets the account cookie.
- `/auth/google/start`, `/auth/google/callback` - Google sign-in flow.
- `/dashboard` - account home: MCP URL, connected assistants, add source,
  YouTube subscription import, activity, plan/billing, and recent videos.
- `/dashboard/search` - transcript search with hybrid/semantic/lexical modes.
- `/dashboard/library` - indexed channels and videos.
- `/dashboard/channel/:channelId` - channel detail and paged videos.
- `/dashboard/video/:videoId` - embedded YouTube player and transcript reader.
- `/dashboard/youtube/start`, `/dashboard/youtube/callback` - read-only
  YouTube subscription OAuth flow.
- `/dashboard/connect` - compatibility redirect back to `/dashboard`.
- `/cli/authorize` - browser approval step for the CLI OAuth-style flow.
- `/privacy`, `/terms`, `/signout` - static/legal and session cleanup routes.

## BFF/API seam

The server-only seam lives in `app/lib/*.server.ts`.

- `env.server.ts` reads Cloudflare bindings. Required:
  `YUTOME_HOSTED_API_URL` and `YUTOME_DASHBOARD_API_TOKEN`.
- `hosted-api.server.ts` calls the hosted Python API. It sends
  `Authorization: Bearer <YUTOME_DASHBOARD_API_TOKEN>` on account requests and,
  for signed-in routes, forwards the browser cookie as
  `X-Yutome-Account-Session`.
- `session.server.ts` only checks that the cookie exists. The Python API is the
  authority that verifies sessions and derives the workspace.
- `cookies.server.ts` manages the `yutome_account_session` HttpOnly cookie.
  `YUTOME_COOKIE_DOMAIN` is empty in local dev and `getyutome.com` in
  production.

Optional bindings:

- `YUTOME_ACCOUNT_SESSION_AUDIENCE` defaults to `yutome:hosted-oauth`.
- `YUTOME_MCP_URL` defaults to `https://mcp.getyutome.com/mcp`.

The hosted API endpoints used by the app are in
`src/yutome/hosted/http_api.py`: `/account/login/*`, `/account/google/*`,
`/account/summary`, `/account/library`, `/account/assistants`,
`/account/sources`, `/account/source-jobs`, `/account/youtube/*`,
`/account/search`, `/account/show`, `/account/list`, `/billing/checkout`, and
`/billing/portal`.

## Local smoke test

Prereqs: Docker or OrbStack, `uv`, Node/npm, and web dependencies installed.

From the repo root:

```bash
cd web
npm install
cat > .dev.vars <<'EOF'
YUTOME_HOSTED_API_URL="http://127.0.0.1:8000"
YUTOME_DASHBOARD_API_TOKEN="dev-dashboard-token"
EOF
cd ..
./web/scripts/local-dev.sh
```

Then open `http://127.0.0.1:5273`, sign up, click the dev sign-in link shown on
the signup page, and land on `/dashboard`.

What the script starts:

- a disposable VectorChord Postgres container named `yutome-dev-pg` on port
  `55432`;
- the hosted FastAPI dev app on `http://127.0.0.1:8000`;
- the React Router dev server on `http://127.0.0.1:5273`.

The script exports local-only Python API settings:

- `YUTOME_E2E_PG_DSN=postgresql://postgres:postgres@127.0.0.1:55432/postgres`
- `YUTOME_APP_URL=http://127.0.0.1:5273`
- `YUTOME_AUTH_DEV_RETURN_LINK=1`

`dev_hosted_api.py` applies the full hosted schema on startup through the
canonical hosted migration path, then serves the Python API with these local dev
credentials:

- dashboard token: `dev-dashboard-token`
- MCP token: `dev-mcp-token`
- account session secret: `dev-account-session-secret`

No local email is sent. In dev mode the API returns the magic link in the
response, and the signup page renders it as a "Dev mode" sign-in link.

## Build, preview, deploy

Run commands from `web/`.

```bash
npm run typecheck
npm run build
npm run preview
npm run deploy
```

`npm run preview` runs `npm run build && vite preview`. It is a local production
build preview, not a Cloudflare deployment.

`npm run deploy` runs `npm run build && wrangler deploy`. Production Worker
settings are in `wrangler.jsonc`: the Worker name is `yutome-web`, routes are
`getyutome.com/*`, `www.getyutome.com/*`, and `app.getyutome.com/*`, and
`YUTOME_HOSTED_API_URL` currently points at
`https://api-production-e072.up.railway.app`.

Before deploying, set the dashboard token as a Cloudflare secret and keep it in
sync with the hosted Python API:

```bash
npx wrangler secret put YUTOME_DASHBOARD_API_TOKEN
```

For a Wrangler version upload instead of an immediate production deploy:

```bash
npx wrangler versions upload
npx wrangler versions deploy
```

## Common failures

- `Missing required server env: YUTOME_HOSTED_API_URL` or
  `Missing required server env: YUTOME_DASHBOARD_API_TOKEN`: the Worker did not
  receive required BFF bindings. For local dev, check `web/.dev.vars`.
- `app_url_unconfigured`: the Python API cannot issue sign-in links because
  `YUTOME_APP_URL` is unset. `local-dev.sh` sets it; manual API runs must set it.
- `account_api_token_unconfigured`, 401, or 403 from account routes: the web
  `YUTOME_DASHBOARD_API_TOKEN` is missing or does not match the hosted API.
- `account_session_signing_unconfigured`: the Python API lacks
  `YUTOME_ACCOUNT_SESSION_HMAC_SECRET`; this is a hosted API configuration
  issue, not a web secret.
- Connection refused or 502/503 during local signup/dashboard load: confirm the
  dev API is on `127.0.0.1:8000` and `.dev.vars` points the web app there.
- Docker startup failures: make sure Docker/OrbStack is running and port `55432`
  is free.
- YouTube panel says connection is not configured: the hosted API does not have
  YouTube OAuth settings for that environment.
- Missing table/column errors in local dev: the schema bootstrap has drifted.
  `dev_hosted_api.py` should use `HostedCommandRunner(...).migrate(phase="hosted")`
  so the local schema matches the hosted migration path.
