#!/usr/bin/env bash
# Run the hosted web frontend end-to-end locally:
#   - a disposable Postgres (via Docker/OrbStack)
#   - the hosted FastAPI (account endpoints) on :8000
#   - the React Router dev server on :5273
#
# Then open http://127.0.0.1:5273 and sign up. Ctrl-C stops everything.
#
# Requires: docker (OrbStack), uv, npm, and `npm install` already run in web/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONTAINER="yutome-dev-pg"
export YUTOME_E2E_PG_DSN="postgresql://postgres:postgres@127.0.0.1:55432/postgres"
# LOCAL ONLY: the hosted API builds magic-link verify URLs from this app origin.
# Paired with YUTOME_AUTH_DEV_RETURN_LINK=1, the signup page shows the link
# directly instead of requiring local email delivery.
export YUTOME_APP_URL="http://127.0.0.1:5273"
export YUTOME_AUTH_DEV_RETURN_LINK=1

API_PID=""
RR_PID=""
cleanup() {
  [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null || true
  [ -n "$RR_PID" ] && kill "$RR_PID" 2>/dev/null || true
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "▶ Starting disposable VectorChord Postgres ($CONTAINER) on :55432…"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --rm --name "$CONTAINER" -e POSTGRES_PASSWORD=postgres -p 55432:5432 tensorchord/vchord-suite:pg17-latest >/dev/null

echo "▶ Starting hosted API on :8000 (waits for Postgres, applies full hosted schema)…"
( cd "$ROOT" && uv run --with uvicorn uvicorn dev_hosted_api:app --app-dir web/scripts --port 8000 --log-level warning ) &
API_PID=$!

echo "▶ Starting web app on :5273…"
( cd "$ROOT/web" && npm run dev -- --port 5273 --host 127.0.0.1 ) &
RR_PID=$!

echo ""
echo "✅ Open http://127.0.0.1:5273  — sign up, then view the dashboard."
echo "   Local auth returns a dev sign-in link on the signup page; no email is sent."
echo "   (Ctrl-C to stop and remove the Postgres container.)"
wait
