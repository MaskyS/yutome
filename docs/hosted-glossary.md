# Hosted Yutome — design glossary

This is the **canonical vocabulary** for hosted-mode Yutome (the provider broker and the
Cloudflare capsule). It exists because the same word had come to mean several things
(`mode`, `grant`, `entitlement`) and the same concept had several names (search
store/substrate, bridge/relay). Code, docs, and beads issues should use the term in the
**Canonical** column and nothing else.

How to use it: when you introduce a hosted concept in prose or name it in code, check
here first. If a concept is missing, add it here in the same change rather than coining a
synonym. Writing rules that go with this glossary live in the `writing-and-ontology-standard`
bd memory.

Scope note: this covers hosted mode. A few core (non-hosted) identifiers are named
inconsistently with the concepts below (most notably `QueryRequest.mode`); those renames
are deferred to a whole-project pass and are called out inline.

---

## 1. Identity and tenancy

- **workspace** — the unit of tenancy. Owns sources, jobs, usage, entitlements, and
  balances. Every hosted query, idempotency key, and usage event is scoped by
  `workspace_id`. [code: `control_plane.Workspace`]
- **local_workspace_id** — the generated fallback tenant key (`ws_<24hex>`) used in local
  mode when no signed-in personal `workspace_id` is set. `yutome setup` generates and
  persists it to `yutome.toml` once so local corpus/search commands resolve a workspace
  without the user managing the concept; a personal `workspace_id` (written by
  `yutome hosted login`) always takes precedence. [code: `config.HostedConfig.local_workspace_id`]
- **user** — a person who signs in and belongs to one or more workspaces.
  [code: `control_plane.User`]
- **connector grant** — the OAuth authorization that binds **one assistant client to one
  workspace**. The domain identifier is **`connector_grant_id`**; `grant_id` is only the
  storage-key form of the same value. Across the language boundary this concept has two
  records, intentionally not identical:
  - `control_plane.AccountGrant` (Python) — the control-plane record. Carries a `kind`
    discriminator (`mcp_client | cli_install | account_session`), a four-state `status`,
    and `token_version: int`.
  - `account-grants.HostedAccountGrant` (TypeScript) — the Cloudflare edge's KV cache of
    the connector grant. Two-state `status`, `token_version: string`.
  - When you need "the OAuth grant for an assistant," say **connector grant**; reserve
    "account grant" for the broader Python record that also covers CLI and session kinds.
  - Hosted MCP grants default to `yutome.search.read`, `yutome.source.write`, and
    `yutome.job.write`. Read-only legacy grants can still query, but `index` fails with
    `insufficient_scope` until the user reconnects.
- **account session** — the signed, account-backed proof of "this user, these
  workspaces" presented at OAuth consent. Replaces the old printed pairing code in hosted
  mode. [code: `account-grants.HostedAccountSession`, `pairing.ts`]
- **YouTube grant** — a stored YouTube OAuth authorization used **only** to discover
  sources (subscriptions, etc.). It must not authorize provider spend.
  [code: `control_plane.YouTubeGrant`, `youtube_grants` table]
- **assistant client** — the external app (Claude, ChatGPT) that calls the MCP endpoint.
  Prefer this over the bare word "connector" when you mean the app rather than the
  endpoint.

## 2. Metering, entitlements, and billing

These four are distinct roles. Keep them distinct — do not let "entitlement" stand in for
all of them.

- **subject** — the provider or internal service being metered: `gemini`, `voyage`,
  `webshare`, or `search_store`. The first three are external providers; `search_store`
  is an internal service. [code: `models.UsageSubject`]
- **operation** — the specific metered action within a subject. The current literals are
  `transcript_fetch`, `metadata_fetch`, `proxy_fetch`, `cleanup_transcript`,
  `transcribe_media`, `embed_documents`, `embed_query`, `lexical_query`, `semantic_query`,
  `hybrid_query`, `index_write`, `replace_active_transcript`, `list_read`, `resource_read`.
  **operation_key** is `"{subject}.{operation}"`.
- **subject_id** — the optional scope id baked into an idempotency key: a video id for
  ingest operations, a client id for query operations. It is the grammatical *subject* of
  the key and is intentionally **distinct from the metered `subject`** (the
  provider/service). Kept as-is — it is genuinely generic, so a single concrete name like
  `target_id` would misdescribe the query (client-id) usage. [code: `ids.idempotency_key`]
- **credential_mode** — how the credentials for a call are sourced: `hosted` (Yutome's own
  provider keys), `byo_hosted` (user-supplied keys on hosted infra; deferred),
  `service_internal` (a Yutome-internal service such as the search store; no external
  credential), or `disabled` (not allocated → deny). (**Renamed from `allocation_kind`,
  and from the bare `mode` field on allocations.**) [code: `models.CredentialMode`]
- **allocation** — a workspace's authorization and routing for a subject's operations,
  carrying `credential_mode` and a `status`. **ProviderAllocation** for external
  providers; **ServiceAllocation** for the search store. [code: `models.ProviderAllocation`,
  `models.ServiceAllocation`]
- **EntitlementPolicy** — the **rules** for a workspace: which operations are allowed, and
  the **hard_limits_by_operation** / **soft_limits_by_operation** unit ceilings.
  Rules only — not balances, not the check. (Limit fields **renamed from
  `max_units_by_operation` / `soft_units_by_operation`** to match the
  `hard_limits_jsonb` / `soft_limits_jsonb` columns.) [code: `models.EntitlementPolicy`]
- **WorkspaceBalance** — the workspace's **remaining prepaid units** (and which units are
  unlimited). Balances only — not rules. [code: `models.WorkspaceBalance`]
- **UsageGate** — the **pre-call check** that returns allow / soft-deny / hard-deny from an
  allocation + policy + balance + estimate, before any paid or scarce call starts.
  [code: `gate.UsageGate`]
- **reservation** — a pre-call hold recording the estimate, the `UsageGate` decision,
  the `credential_mode`, an idempotency key, and a status. Lifecycle:
  **reserve → settle → release** (see below). [code: `models.UsageReservation`]
- **settle / reconcile** — after a call, true the reservation up to **actual** units and
  refund the unused reserve. Use **settle** in prose; the reservation status it produces is
  `reconciled`.
- **release** — refund a reservation in full (work was denied before execution, or
  cancelled). Reservation status `released`.
- **usage event** — an append-only record of an attempt or outcome (`started`,
  `succeeded`, `failed`, `unknown`, `denied`, `released`) with actual units.
  [code: `models.UsageEvent`]
- **ledger (usage ledger)** — the append-only log of reservations and usage events. It is
  the **source of truth** for pre-call authorization.
- **billable meter** — an internal usage unit (or composite of units) that is reported to
  Stripe. Yutome reports a single composite `credits` meter: the billable units
  (`media_seconds`, `total_tokens`, `vectors`, `queries`) are collapsed into one `credits`
  value via fixed per-unit weights. All other units (`candidate_limit`,
  `query_vector_dimensions`, `request_count`, `bytes`, …) are cost-visibility/quota units
  and are never reported to Stripe. The weights are calibrated so **1 credit ≈ 1 indexed
  video-hour ≈ ~$0.10 retail**: `media_seconds` 3e-4, `total_tokens` 5e-5, `vectors` 7e-3,
  `queries` 1e-3. [code: `billing.STRIPE_CREDIT_UNIT_WEIGHTS`]
- **Personal plan / seat** — the single paid plan: a **$4/mo flat recurring seat** plus a
  metered overage price. Stripe Checkout (`mode=subscription`) subscribes the customer to
  **both** line items — the flat seat Price (`STRIPE_SEAT_PRICE_ID`, quantity 1) and the
  metered `credits` Price (`STRIPE_PRICE_ID`, no quantity). The `starter` entitlement plan is
  reused as this seat's included allowance, not a free tier. [code: `STRIPE_SEAT_PRICE_ID`]
- **included allowance** — the monthly usage the seat covers before any overage is metered:
  ~25 indexed video-hours, tracked as billable units in `WorkspaceBalance.remaining_units`
  (`account.STARTER_INCLUDED_UNITS`). The reserve/settle accounting that already debits this
  balance is the single source of truth for "remaining included units".
- **metered overage** — usage **beyond** the included allowance, reported to the Stripe
  `credits` meter. Each settled event meters only `max(0, event_credits −
  included_remaining_credits)` (the allowance still free, in credits, read under the balance
  lock before the event). While the allowance covers the event nothing is enqueued; once
  exhausted only the excess is. [code: `billing.overage_credits_for_event`,
  `ledger._enqueue_stripe_meter_exports`]
- **trial** — a **14-day, card-gated** evaluation set at Checkout via
  `subscription_data[trial_period_days]=14`. There is **no perpetual free tier**: a new
  workspace bootstraps a trial (`workspaces.subscription_status = 'trialing'`, a
  `workspaces.trial_ends_at` set 14 days out). The entitlement layer treats Stripe status
  `trialing` exactly like `active`.
- **trial-expiry read-only** — the entitlement state of a workspace whose trial has ended
  with no active/trialing subscription: ingest (`source.write` / `index`) and MCP/API tool
  calls **hard-deny** (fail closed), while the existing corpus stays readable from the
  dashboard `/account/*` read endpoints. [code: `entitlements.PostgresUsageContextProvider`]
- **Stripe meter export** — a queued usage→meter-event row: one settled usage event maps to
  one pending `stripe_meter_exports` row, claimed by the `stripe-meter-export` worker and
  POSTed to Stripe `/v1/billing/meter_events`. [code: `stripe_meter_exports` table,
  `billing.StripeMeterExportEvent`]
- **meter event identifier** — the deterministic dedupe key
  (`stripe:{workspace_id}:{usage_event_id}:{meter_unit}`) used as the row id AND the Stripe
  meter_event `identifier`; Stripe dedupes identical identifiers over a rolling ≥24h window,
  so re-enqueues and retries are no-ops.
- **billing mirror (Stripe)** — the external billing provider. A **mirror only**: it never
  authorizes a call and its availability never changes a `UsageGate` decision. Subscriptions
  are created via Stripe Checkout (`mode=subscription`) on **two** line items — the flat seat
  Price and the metered `credits` Price (see **Personal plan / seat**) — with a 14-day
  card-gated trial. Overage usage is charged in arrears by reporting the `credits` meter (see
  **metered overage**). A workspace has no Stripe Customer until its first `/billing/checkout`
  (lazy creation); usage with no active/trialing subscription is enqueued as `skipped`, not
  billed.
- **provider broker** — the component that holds provider credentials and meters and
  authorizes provider/service calls on a workspace's behalf. (Kept; it is the name of the
  plan and the `yt-indexer-pvq` epic. Defined here so it stops being used vaguely.)

## 3. Ingest

- **source** — a channel, playlist, handle, or single video a workspace has indexed.
  [code: `control_plane.Source`]
- **source refresh policy** — the row that decides when a source is re-checked
  (cadence, next-run time, retry state). [code: `source_refresh_policies` table]
- **job** — a durable, idempotent unit of ingest work claimed from a Postgres queue
  (`discover_source`, `index_video`, embedding/index maintenance). [code: `control_plane.Job`]
- **job operation** — a sub-step of a job, with its own status and reservation.
- **worker** — a process that **claims jobs** from the queue and runs them.
- **executor** — the class that **runs one job type** (e.g. `HostedIndexingExecutor`,
  `HostedSourceDiscoveryExecutor`). A worker invokes an executor.
- **scheduler** — the always-on loop that reads source refresh policies and enqueues due
  jobs into the Postgres queue. One shared scheduler, not one cron per workspace.
- **transcript version** — an immutable snapshot of a video's transcript (raw, cleaned,
  etc.). [code: `transcript_versions` table]
- **active transcript version** — the transcript version currently used for indexing and
  retrieval, selected by the `videos.active_transcript_version_id` pointer (swapped
  atomically; never by deleting chunks).
- **chunk** / **chunk embedding** — a transcript segment and its dense vector for a given
  embedding model + dimension.
- **search index profile** — the row identifying one indexable configuration
  (backend + embedding model + dimension + chunking version + tokenizer). Changing any of
  these is a new profile and a backfill, not a silent in-place change.
  [code: `search_index_profiles` table]

## 4. Search and retrieval

- **search store** — the Postgres + VectorChord database that holds chunks,
  embeddings, and indexes and answers queries. (**Canonical** — replaces "search
  substrate" and "hosted database.") [code: `search_store.SearchStore`]
- **lexical query** — full-text / BM25 search. Needs no provider allocation but still
  requires a `search_store` service reservation.
- **semantic query** — vector / approximate-nearest-neighbour search over embeddings.
- **hybrid query** — lexical and semantic candidates fused with reciprocal-rank fusion
  (RRF). May fall back to lexical when the vector path is unavailable, *unless* policy
  requires a hard denial; **semantic must not silently fall back** (it fails or soft-denies).
- **query mode** — the search strategy selector with values `lexical | semantic | hybrid`.
  This is a distinct concept from `credential_mode` and `worker_mode`. (Code rename
  `QueryRequest.mode → query_mode` is **deferred** to the whole-project pass because
  `QueryRequest` is core, not hosted.)

## 5. Remote access

- **bridge** — the local desktop process that maintains a WebSocket to the Cloudflare
  Worker and answers queries from the local corpus. A device/install credential, separate
  from any assistant credential.
- **relay** — the Cloudflare Durable Object that brokers a bridge's WebSocket session and
  routes requests to it. Authenticated by a relay token. Named per tenant
  (`workspace_id` + `install_id`/`connector_grant_id`), never `"default"`.
- **replica** — a read-only, Cloudflare-hosted mirror of a workspace's corpus that answers
  **when the desktop bridge is offline**. (Call it a **cloud replica** or **offline
  replica**; do **not** call it "always-on" — that misdescribes a feature whose whole point
  is the desktop being off.)
- **served_from** — which backend answered a request: `bridge` or `replica`. Recorded in
  responses and usage events.
- **connector** — the assistant-facing MCP/HTTP endpoint. When you mean the calling app,
  say *assistant client* instead.
- **capsule** — **retired as a concept.** It had come to mean the whole architecture, a
  single deployed Worker, and a code package all at once, and product copy already avoids
  it. Name the specific thing instead (relay, replica, worker, connector). The only
  surviving use is the literal Cloudflare package/directory name `yutome-capsule`.

## 6. Deployment and runtime

- **hosted** — the Yutome-operated, multi-tenant service (Cloudflare edge + Railway
  workers + Postgres). Distinct from **connector** (single-owner, user-deployed) mode.
- **worker_mode** — the deployment mode of the Cloudflare Worker: `hosted` (multi-tenant)
  or `connector` (single-owner). Distinct from `credential_mode` and `query mode`.
  [code: `YUTOME_WORKER_MODE`, `isHostedWorkerMode`]
- **control plane / ingest plane / query plane** — the three tiers: identity/auth/billing,
  fetch-clean-chunk-embed-index, and serve-MCP-queries respectively. Postgres is the system
  of record across all three; Cloudflare Workers never run ingest.

---

## Status enums (reference)

The exact literals, so prose and tests stop inventing variants.

| Enum | Values | Meaning |
|---|---|---|
| `CredentialMode` | `hosted`, `byo_hosted`, `disabled`, `service_internal` | how a call's credentials are sourced |
| `UsageSubject` | `gemini`, `voyage`, `webshare`, `search_store` | provider/service being metered |
| `AllocationStatus` | `active`, `limited`, `disabled`, `invalid` | allocation health |
| `ReservationStatus` | `reserved`, `denied`, `released`, `reconciled` | reservation lifecycle |
| `EventStatus` | `started`, `succeeded`, `failed`, `unknown`, `denied`, `released` | usage-event outcome |
| `UsageDenialEffect` | `hard`, `soft` | whether a denial blocks or warns |
| `AccountGrantStatus` | `active`, `expired`, `revoked`, `disabled` | connector/account grant (Python) |
| `YouTubeGrantStatus` | `active`, `expired`, `revoked`, `invalid` | YouTube grant |
| `JobStatus` | `queued`, `discovering`, `queued_video_jobs`, `preparing`, `reserving_usage`, `fetching_transcript`, `fallback_transcription`, `cleaning`, `embedding`, `writing_index`, `reconciling_usage`, `retry_wait`, `denied`, `failed`, `succeeded`, `cancelled` | job lifecycle (terminal: `denied`/`failed`/`succeeded`/`cancelled`) |
| `JobOperationStatus` | `planned`, `denied`, `reserved`, `started`, `succeeded`, `failed_retryable`, `failed_final`, `reconciled`, `released` | per-operation lifecycle |

---

## Renamed in code (for the ontology pass)

Greenfield renames — no alias shims (pre-launch, no back-compat).

| Old | Canonical | Where |
|---|---|---|
| `UsageReservation.allocation_kind` | `credential_mode` | `models.py`, `usage_reservations.allocation_kind` column, `gate.py`, `ledger.py`, `repositories.py`, `entitlements.py` |
| `ProviderAllocation.mode`, `ServiceAllocation.mode` | `credential_mode` | `models.py`, `provider_allocations.mode` / `service_allocations.mode` columns |
| `EntitlementPolicy.max_units_by_operation` | `hard_limits_by_operation` | `models.py`, `gate.py`, `entitlements.py` |
| `EntitlementPolicy.soft_units_by_operation` | `soft_limits_by_operation` | `models.py`, `gate.py`, `entitlements.py` |

---

## Retired terms (do not use)

| Avoid | Use instead |
|---|---|
| "capsule" (as a concept) | the specific thing: relay, replica, worker, connector |
| "search substrate", "hosted database" | search store |
| "Always-On (Search) Replica" | cloud replica / offline replica |
| "Remote Connector Only" mode | bridge mode (laptop-backed) |
| "Production follow-up:" (issue titles) | "Hosted: \<verb\> …" |
| "block obviously unaffordable work" | "deny when balance < estimated units" |
