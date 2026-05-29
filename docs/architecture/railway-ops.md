# Railway cron and worker scheduling for hosted Yutome

One-line summary: hosted Yutome runs on six Railway services sharing one image and one Postgres — an always-on web API, an always-on worker (the cron exception), and four single-shot jobs scheduled via native Railway cron (restart `NEVER`), with overlap made safe by `FOR UPDATE SKIP LOCKED` and `stripe-meter-export` shipped disabled until overage billing launches.

Status: documentation-derived, not empirically tested. No Railway cron tick has been observed firing the Yutome image. Railway behavior below is confirmed against current Railway docs (fetched 2026-05-29); CLI behavior is confirmed by reading `src/yutome/cli/hosted.py`. See section 6 for what must be proven before this is load-bearing for launch.

---

## 1. Decision / recommendation

Six services share one image and reach the Postgres over Railway private networking. Two are always-on; four are single-shot jobs on native Railway cron.

| Service | Mechanism | Schedule (UTC) | Restart policy | Exact start command |
|---|---|---|---|---|
| `web` | always-on | none (must NOT be cron) | `ON_FAILURE` | `yutome hosted api` |
| `worker` | always-on (cron exception) | none | `ON_FAILURE` | `yutome hosted run worker --poll-interval 5 --lease-owner $RAILWAY_REPLICA_ID` |
| `balance-rollover` | native cron | `0 * * * *` (hourly) | `NEVER` | `yutome hosted run balance-rollover --once --limit 100` |
| `source-refresh` | native cron | `*/5 * * * *` | `NEVER` | `yutome hosted run source-refresh --once --lease-owner $RAILWAY_REPLICA_ID` |
| `maintenance` | native cron | `*/15 * * * *` | `NEVER` | `yutome hosted run maintenance --once` |
| `stripe-meter-export` | native cron (DISABLED) | `*/15 * * * *` when enabled | `NEVER` | `yutome hosted run stripe-meter-export --once --lease-owner $RAILWAY_REPLICA_ID` |

Notes carried from CLI verification:

- The web service command is `yutome hosted api` (the `api` subcommand) — NOT `hosted run api`. `api` is not a `run` job.
- Loop mode = omit `--once`. There is no `--loop` flag; loop sleep is governed by `--poll-interval` (default 5.0s).
- `--lease-owner` is declared on the `run` command and forwarded only to `worker`, `source-refresh`, and `stripe-meter-export`. It is accepted-but-ignored (silent no-op, not an error) on `maintenance` and `balance-rollover`, so those start commands omit it.
- `balance-rollover` is launch-critical: without it a `SUBSCRIBED` workspace keeps period-1's allowance and never refreshes, so paid users hard-cap at the usage gate after period 1.

---

## 2. Design choices and rationale

**Native cron over an external scheduler.** Each launch-critical tick (`source-refresh`, `maintenance`, `balance-rollover`) is a single-shot `--once` pass that claims its own work with `FOR UPDATE SKIP LOCKED`. Railway's per-service Cron Schedule runs the start command on a crontab tick and expects the process to exit, which matches `--once` exactly. An external scheduler (a separate cron host, a queue, GitHub Actions) would add a second platform to credential, monitor, and keep in env parity with the six services — for no behavioral gain over a job that already self-claims and exits.

**Worker is the cron exception.** The worker drains the job queue on a ~5s poll. Railway's cron minimum interval is 5 minutes, which is far too coarse for ingest latency. So the worker runs as a long-running always-on service (omit `--once`, `--poll-interval 5`), not as cron. The four genuinely periodic jobs (5-min, 15-min, hourly) fit inside the 5-min floor and stay on cron.

**Single consolidated `--loop` scheduler — considered, not chosen.** A single always-on service running an internal scheduler (one process looping with its own timers) would dodge the 5-min floor, avoid the config-as-code cron bug (section 5/6), centralize timeout + alerting, and cut env-parity surface from six services to about three. Tradeoffs: it is always-on (small constant cost vs cron's run-only cost), a crash takes down every schedule at once, and we own scheduling correctness instead of Railway. Given the field-reliability and config-as-code concerns in section 6, this is a credible simpler alternative and should be re-weighed before launch — not assumed away. The per-job-cron plan's advantages are operational legibility (each job's schedule and logs are isolated) and not paying for idle time. yt-indexer-zjy also notes a spec for a `hosted run scheduler --loop/--once` job; if that lands, the consolidated path becomes a one-service swap.

**Restart `NEVER` on cron services (recommendation derived from documented behavior, not a Railway-stated rule).** Railway's documented halves: (1) the default restart policy is `ON_FAILURE`, max 10 retries, and `ON_FAILURE` restarts only on a non-zero exit; (2) a still-running (`Active`) deployment causes the next scheduled fire to be skipped. The inference: a tick that exits non-zero under `ON_FAILURE`/`ALWAYS` is restarted, goes `Active`, and wedges the schedule. Railway does not document a "use NEVER for cron" rule, so treat `NEVER` as our recommendation. Precise condition: only a non-zero exit triggers resurrection — a clean `--once` run that exits 0 is not restarted even under the default `ON_FAILURE`. `NEVER` has no plan-tier restriction (only `ALWAYS` and >10 retries are paid-gated), so it works on every tier.

**`SKIP LOCKED` makes overlap safe.** Every tick claims work under `FOR UPDATE SKIP LOCKED` — the job lease (`jobs.py`, `runtime.py`), the source-refresh policy lock (`runtime.py`, `FOR UPDATE OF policy SKIP LOCKED`), and the workspace-balance period row (`billing.py`, `runtime.py`, `FOR UPDATE OF balance SKIP LOCKED`). So cron jitter, an operator's manual `--once` run, and extra worker replicas never double-process. This is the same skip mechanism Railway's overlap=skip already assumes. Caveat: `SKIP LOCKED` prevents double-processing but does NOT protect against a wedged process — a lease held by a hung run blocks others until the lease expires, so leases need a TTL / steal-on-expiry, not just `SKIP LOCKED` (see section 7).

**`stripe-meter-export` ships disabled.** Overage billing is off by default, so the export job has no work and is kept unscheduled. Usage export to a billing meter is cumulative: a missed or skipped run must not drop usage. Before enabling it, it must export from a durable high-water-mark (last-exported cursor), not "usage since last tick," so a skip just exports more next time. Gate enabling on (a) the high-water-mark cursor, (b) the missed-run heartbeat wired up, and (c) one observed successful tick in staging.

---

## 3. Setup runbook

### Dashboard (recommended path until a tick is observed)

Per the config-as-code cron caveat (section 6), set cron schedules in the dashboard, not in config-as-code.

1. Create or select the Railway project; ensure the Postgres service exists and is reachable over private networking.
2. For each of the six services: point it at the shared repo/image, set its start command from the section 1 table.
3. For each cron service (`balance-rollover`, `source-refresh`, `maintenance`, and `stripe-meter-export` once enabled): open Service → Settings → Cron Schedule, enter the crontab expression from the table.
4. For each cron service: set restart policy to `NEVER` (Service → Settings → Restart Policy).
5. Leave `web` and `worker` with NO cron schedule and restart policy `ON_FAILURE`.
6. Do NOT set a cron schedule on `stripe-meter-export` until overage launch (see section 2 gate).
7. Wire env via Railway reference variables so the Postgres connection and the `YUTOME_*`/`STRIPE_*` config have one source of truth across all six services (see section 7).

### Config-as-code snippets (use once config-as-code cron is personally confirmed firing)

Railway's config file does NOT follow a service's root directory; config paths must be absolute (e.g. `/entrypoints/worker/railway.toml`). The cron field is `deploy.cronSchedule`; the restart field is `deploy.restartPolicyType`.

`web` (always-on, no cron):

```toml
[deploy]
startCommand = "yutome hosted api"
restartPolicyType = "ON_FAILURE"
```

`worker` (always-on, cron exception):

```toml
[deploy]
startCommand = "yutome hosted run worker --poll-interval 5 --lease-owner $RAILWAY_REPLICA_ID"
restartPolicyType = "ON_FAILURE"
```

`balance-rollover` (hourly cron, launch-critical):

```toml
[deploy]
startCommand = "yutome hosted run balance-rollover --once --limit 100"
cronSchedule = "0 * * * *"
restartPolicyType = "NEVER"
```

`source-refresh` (every 5 min):

```toml
[deploy]
startCommand = "yutome hosted run source-refresh --once --lease-owner $RAILWAY_REPLICA_ID"
cronSchedule = "*/5 * * * *"
restartPolicyType = "NEVER"
```

`maintenance` (every 15 min):

```toml
[deploy]
startCommand = "yutome hosted run maintenance --once"
cronSchedule = "*/15 * * * *"
restartPolicyType = "NEVER"
```

`stripe-meter-export` (DISABLED — omit `cronSchedule` until overage launch; shown for when enabled):

```toml
[deploy]
startCommand = "yutome hosted run stripe-meter-export --once --lease-owner $RAILWAY_REPLICA_ID"
# cronSchedule = "*/15 * * * *"   # leave commented until overage billing launches
restartPolicyType = "NEVER"
```

---

## 4. Constraints and gotchas

- **5-minute floor.** The shortest interval between cron executions cannot be less than 5 minutes. The 5s ingest poll is why the worker is always-on, not cron.
- **UTC only.** All cron schedules are evaluated in UTC. The current schedule (`0 * * * *`, `*/5`, `*/15`) is DST-immune. Recorded so nobody later writes a "9am local" business-hours job and gets bitten by DST.
- **Timing is best-effort.** Railway does not guarantee execution to the minute; real spacing can drift by a few minutes. Harmless for hourly/15-min/5-min cadences; no exact jitter SLO is published.
- **Overlap = skip, not stack.** If a previous execution is still `Active` when the next is due, Railway skips the new fire and does NOT auto-terminate the previous run. There is no backfill or catch-up of a skipped/missed tick — assume missed ticks simply do not happen.
- **Process MUST exit.** Cron services are expected to terminate as soon as the task finishes, closing DB connections and leaving no open resources. `--once` does this; a hang stays `Active` and blocks all future fires of that job.
- **No platform per-run timeout.** Nothing in Railway docs describes a max-runtime kill for cron services. A tick that never exits wedges its schedule indefinitely. This is the single biggest operational risk (see section 7).
- **Web is never cron.** The web API is always-on; giving it a cron schedule would tear it down on the schedule.
- **`--lease-owner` no-op on two jobs.** Passing `--lease-owner` to `maintenance` or `balance-rollover` does not error; it is silently ignored. The start commands omit it for clarity.

---

## 5. Config-as-code cron caveat (read before using `deploy.cronSchedule`)

Railway staff publicly acknowledged (Dec 2025, station.railway.com) an issue with crons defined via `railway.json` / config-as-code causing mission-critical crons to get stuck or not fire, with the official workaround: remove the cron schedule from config-as-code and set it in the dashboard. A fix was marked done 2025-12-25 but we have NOT re-verified it on the current version. Recommendation: set Cron Schedule in the dashboard, not `deploy.cronSchedule`, until we personally confirm a config-as-code cron fires on the current Railway version. This is a deliberate exception to a GitOps-everything default.

---

## 6. Gaps / NOT yet verified / confirm before relying on it

This research is documentation-derived, not empirically tested. Everything in the lists below the first bullet must be proven by an operator before this is load-bearing for launch.

Resolved by research (sourced against current Railway docs):

- 5-minute floor, UTC-only, overlap=skip, no auto-terminate — confirmed in docs.
- No platform per-run timeout — confirmed by absence in docs.
- `NEVER` has no plan-tier restriction — confirmed.
- CLI job args, `--once`/loop semantics, and `--lease-owner` forwarding — confirmed by reading `src/yutome/cli/hosted.py`.
- `FOR UPDATE SKIP LOCKED` on job lease / policy lock / balance period row — confirmed present in `jobs.py`, `billing.py`, `runtime.py`.

Still unverified — should confirm before relying on it:

- **Restart-resurrects-one-shot is an inference, not a documented rule.** Both halves are documented (Active → skip; `ON_FAILURE` restarts on non-zero exit), but Railway nowhere states the causal "non-Never resurrects a cron and wedges it" rule. Present `NEVER` as our recommendation derived from documented behavior.
- **Plan tier required to use cron itself: UNVERIFIED.** Docs do not state a minimum plan to use cron (only that `NEVER` restart is unrestricted). Confirm the actual project plan tier in the Railway dashboard before relying on cron.
- **No tick has been observed firing.** Zero empirical runs. Before launch, deploy one cron service (e.g. `maintenance` `*/15`) and watch it fire twice, exit cleanly, and show `Active` → completed in deploy logs.
- **Overlap=skip on the current version is doc-derived only.** Verify by forcing a tick to run >5 min and confirming the next is skipped (not queued, not parallel).
- **Config-as-code cron is suspect (section 5).** The dashboard workaround is in force until we confirm a config-as-code cron fires.
- **Field reliability is unproven for our workload.** Multiple 2025 user reports of skipped/stuck crons (daily jobs missing for 4–5 days; a 2025-12-04 "stuck" incident). Railway claims a Nov 2025 scheduler rework eliminated missed crons but published no metrics. Design alerting accordingly (section 7).
- **No backfill / catch-up.** Railway does not re-run a skipped or missed tick; there is an open user feature request, but it does not exist.

---

## 7. Things to consider / future

- **Per-run timeout enforcement (highest-priority mitigation).** Because there is no platform timeout and overlap=skip, one stuck `Active` run halts that job forever with no error surfaced. Mitigate in our code: every `--once` job should enforce a hard wall-clock self-timeout (e.g. `signal.alarm` / `asyncio.wait_for`) shorter than its cron interval and exit non-zero rather than hang.
- **Lease TTL / steal-on-expiry.** `SKIP LOCKED` prevents double-processing but a lease held by a wedged run blocks others until it expires. Confirm leases have a TTL and steal-on-expiry before scaling — otherwise a dead/hung holder strands its claimed work.
- **Observability / alerting on missed runs (launch-critical for `balance-rollover`).** Railway has no native "cron missed" or "cron failed" alert; the only signal is deploy logs/status you must go look at. Add an external heartbeat: each successful tick writes a `last_success_at` row (or pings a dead-man's-switch); a separate check — or the always-on web service — alerts when `now - last_success_at` exceeds the interval. This is the only way to catch the silent-wedge and silent-skip modes, and it is mandatory for `balance-rollover`.
- **Env / secret parity across six services.** Six services each need the same Postgres + `YUTOME_*`/`STRIPE_*` config; plain copies drift. Use Railway reference variables (shared vars / referencing the Postgres service's vars) so there is one source of truth. Record which variables MUST be identical, and add a `doctor`-style preflight that fails a tick when a required env var is missing rather than half-running.
- **Deploy coupling.** With one shared image, a push may trigger all six to rebuild/redeploy. Services have their own root directory / watch patterns / config path, so they can deploy independently if configured that way. Decide explicitly: set watch patterns so unrelated changes do not churn all six, and accept that an image/code change should roll all six (they share code). Remember the config file does not follow root directory — use absolute config paths.
- **More than one worker replica.** The worker already uses per-replica `--lease-owner $RAILWAY_REPLICA_ID` + `SKIP LOCKED`, so multiple replicas are safe by construction. Before scaling, confirm lease TTL / steal-on-expiry (above) so a dead replica's claimed items get re-leased. Cost note: cron services bill only for the short time they run (marginal); the ongoing cost levers are the always-on `web` and `worker` plus Postgres — worker replica count is the lever to watch, not cron.
- **Backfill semantics, per job (carry-nothing reasoning):**
  - `balance-rollover`: carry-nothing is correct — a missed hourly tick is recovered by the next because rollover reads current period state, re-seeding `remaining_units` from the active `EntitlementPolicy`'s `included_units`, not a delta. A long outage self-heals on resume; the residual risk is latency, covered by the heartbeat. Launch-critical, so the heartbeat is mandatory here.
  - `source-refresh`: mostly self-healing — the next `*/5` tick re-claims due sources via `SKIP LOCKED`. Verify a refresh does not depend on having run in every window (no per-window dedup a skip would break); if it just refreshes whatever is stale, skips only add latency.
  - `maintenance`: idempotent housekeeping; a skipped run is harmless as long as work accumulates and the next run drains it. Confirm no maintenance step assumes a fixed cadence.
  - `stripe-meter-export`: carry-nothing is NOT obviously safe — usage export is cumulative. When enabled it must export from a durable high-water-mark cursor so a skip exports more next time, never drops usage. Precondition for enabling (section 2).
- **When to enable overage export.** Keep `stripe-meter-export` disabled until overage billing launches. Enable only after: (a) the high-water-mark cursor export, (b) the missed-run heartbeat wired up, and (c) one observed successful tick in staging.

---

## 8. Sources

Railway docs (fetched 2026-05-29):

- https://docs.railway.com/cron-jobs — 5-min floor, UTC, overlap=skip, no auto-terminate, must-exit, best-effort timing, dashboard + `cronSchedule` config.
- https://docs.railway.com/config-as-code/reference — `deploy.cronSchedule`, `deploy.restartPolicyType` (`ON_FAILURE`/`ALWAYS`/`NEVER`), per-environment overrides.
- https://docs.railway.com/deployments/restart-policy — default `ON_FAILURE` (max 10), `ON_FAILURE` restarts only on non-zero exit, `NEVER` unrestricted, `ALWAYS` and >10 retries paid-gated.
- https://docs.railway.com/guides/cron-workers-queues (dated 2026-03-30) — cron vs worker tradeoffs, failure modes, overlap=skip.
- https://docs.railway.com/guides/deploying-a-monorepo — per-service root dir / watch patterns; config file does not follow root directory (absolute paths).
- https://blog.railway.com/p/run-scheduled-and-recurring-tasks-with-cron — exit requirement, UTC, 5-min, skip; silent on restart policy for crons.

Railway community (staff answers):

- https://station.railway.com/questions/cron-jobs-are-stuck-and-not-executing-on-40255aab — config-as-code cron bug; dashboard workaround.
- https://station.railway.com/questions/scheduled-cron-job-occasionally-fails-to-26b6141a — intermittent misses; Nov 2025 scheduler rework, no published metrics.

Repo:

- CLI source verified: `/Users/sheikmeeran/yt-indexer/src/yutome/cli/hosted.py` — `run` subcommand lines 104–175 (job args, `--once`/`--poll-interval`, `--lease-owner` forwarding); `api` subcommand lines 13–21 (`yutome hosted api`, not `hosted run api`).
- `FOR UPDATE SKIP LOCKED` verified: `src/yutome/hosted/jobs.py:87`, `src/yutome/hosted/billing.py:1152`, `src/yutome/hosted/runtime.py:561,652,672,726`.
- Prior art: beads issue `yt-indexer-zjy` — "Hosted ops: codify Railway API, worker, cron, and maintenance services."
