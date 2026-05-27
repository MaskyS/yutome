# Hosted Yutome

Multi-tenant Yutome: a Postgres-backed control/ingest/query system with per-call metering, fronted
by a FastAPI that serves assistants (MCP), the dashboard (session), and the CLI (PKCE token). This
is the codex-built surface; everything below is anchored to `src/yutome/hosted/`.

Canonical vocabulary lives in [`docs/hosted-glossary.md`](../hosted-glossary.md) and is used verbatim
here — `workspace`, `subject`, `credential_mode`, `EntitlementPolicy` / `WorkspaceBalance` / `UsageGate`,
`reservation`, `search store`, `bridge`/`relay`/`replica`.

---

## 1. Three planes

Postgres is the system of record across all three. Cloudflare Workers never run ingest; Railway
workers never serve edge queries.

| Plane | Owns | Backed by |
|---|---|---|
| **Control** | identity, auth, billing, entitlements, balances, the usage ledger | Postgres |
| **Ingest** | discover → fetch → clean → chunk → embed → index, as durable jobs | Postgres queue + Railway workers |
| **Query** | serving `find`/`list`/`show`/`q` to assistants and the dashboard | Cloudflare edge (relay/replica) + the hosted adapter |

---

## 2. Postgres data model

The schema is defined in [`schema.py`](../../src/yutome/hosted/schema.py) as SQLAlchemy `Table`s on
one `MetaData` (`schema.py:8`). Every tenant-scoped row carries `workspace_id`; almost everything
fans out from `workspaces`.

### 2.1 Cluster map

```mermaid
flowchart TB
    ws["workspaces<br/>(+ users, members)"]
    subgraph A["Identity / Auth"]
        a1["account_sessions · account_grants<br/>email_login_tokens · youtube_grants"]
    end
    subgraph B["Entitlements / Billing"]
        b1["provider_allocations · service_allocations<br/>entitlement_policies · workspace_balances<br/>price_books · billing_customers"]
    end
    subgraph C["Usage / Metering (append-only)"]
        c1["usage_reservations · usage_events<br/>credit_ledger_entries · billing_exports<br/>polar_webhook_snapshots"]
    end
    subgraph D["Ingest / Jobs"]
        d1["sources · source_refresh_policies<br/>jobs · job_operations"]
    end
    subgraph E["Search / Transcripts"]
        e1["videos · transcript_versions<br/>search_index_profiles · chunks · chunk_embeddings"]
    end
    ws --> A & B & C & D & E
```

### 2.2 Identity / Auth

```mermaid
erDiagram
    users ||--o{ workspaces : owns
    users ||--o{ workspace_members : "is"
    workspaces ||--o{ workspace_members : has
    users ||--o{ account_sessions : authenticates
    workspaces ||--o{ account_sessions : scopes
    users ||--o{ account_grants : authorizes
    workspaces ||--o{ account_grants : scopes
    users ||--o{ youtube_grants : "links YT"
    workspaces ||--o{ youtube_grants : scopes

    users {
        text id PK
        text email
        text normalized_email "unique idx"
        text status "default active"
    }
    workspaces {
        text id PK
        text owner_user_id FK
        text name
        text status
    }
    account_sessions {
        text id PK
        text user_id FK
        text workspace_id FK
        text session_hash "unique idx"
        text_array scopes
        timestamptz expires_at
        timestamptz revoked_at
    }
    account_grants {
        text id PK
        text user_id FK
        text workspace_id FK
        text kind "mcp_client|cli_install|account_session"
        text_array scopes
        text install_id "unique partial idx"
        int token_version "default 1"
        text status
    }
    youtube_grants {
        text id PK
        text user_id FK
        text workspace_id FK
        text_array scopes "discovery only"
    }
    email_login_tokens {
        text id PK
        text token_hash "unique idx"
        text normalized_email
        timestamptz expires_at
        timestamptz consumed_at "one-time use"
    }
```

- `account_grants.kind` discriminates `mcp_client` (a connected assistant), `cli_install` (a CLI
  device), and `account_session`. `token_version` is the revocation lever; `install_id` is unique
  when present (`schema.py:185-211`).
- `youtube_grants` are for **discovery only** (subscriptions/uploads) and must not authorize provider
  spend — kept separate from `account_grants` (`schema.py:213-227`).
- `email_login_tokens` is the magic-link store: a hashed, single-use token with an expiry
  (`schema.py:88-104`).

### 2.3 Entitlements / Billing

```mermaid
erDiagram
    workspaces ||--o{ provider_allocations : has
    workspaces ||--o{ service_allocations : has
    price_books ||--o{ entitlement_policies : prices
    workspaces ||--o{ entitlement_policies : has
    entitlement_policies ||--|| workspace_balances : funds
    workspaces ||--|| workspace_balances : has
    workspaces ||--o{ billing_customers : has

    provider_allocations {
        text id PK
        text workspace_id FK
        text provider "gemini|voyage|webshare|youtube"
        text operation
        text credential_mode
        text status
    }
    service_allocations {
        text id PK
        text workspace_id FK
        text service "search_store"
        text operation
        text credential_mode "default service_internal"
        text backend
    }
    entitlement_policies {
        text id PK
        text workspace_id FK
        text plan_key
        text price_book_id FK
        text_array allowed_operations
        jsonb hard_limits_jsonb
        jsonb soft_limits_jsonb
        jsonb grace_policy_jsonb
    }
    workspace_balances {
        text workspace_id PK
        text entitlement_policy_id FK
        timestamptz period_start_at
        timestamptz period_end_at
        jsonb used_units_jsonb
        jsonb reserved_units_jsonb
        jsonb remaining_units_jsonb
        text_array unlimited_units
    }
    price_books {
        text id PK
        text version "unique"
        jsonb products_jsonb
        jsonb unit_mapping_jsonb
    }
```

The four roles here are distinct and easy to confuse — keep them straight:

| Role | Table | Question it answers |
|---|---|---|
| **allocation** | `provider_allocations` / `service_allocations` | *Is this operation authorized, and where do its credentials come from?* (`credential_mode`) |
| **EntitlementPolicy** | `entitlement_policies` | *Which operations are allowed, and what are the hard/soft per-operation ceilings?* |
| **WorkspaceBalance** | `workspace_balances` | *How many prepaid units remain this period (and which units are unlimited)?* |
| **price_book** | `price_books` | *What does a unit cost / how do product units map?* |

### 2.4 Usage / Metering (append-only)

```mermaid
erDiagram
    workspaces ||--o{ usage_reservations : holds
    usage_reservations ||--o{ usage_events : "settled by"
    workspaces ||--o{ usage_events : records
    usage_events ||--o{ billing_exports : mirrors
    usage_reservations ||--o{ billing_exports : references
    billing_customers ||--o{ billing_exports : bills
    workspaces ||--o{ credit_ledger_entries : credits
    workspaces ||--o{ polar_webhook_snapshots : "webhooks for"

    usage_reservations {
        text id PK
        text workspace_id FK
        text subject
        text operation
        text credential_mode
        jsonb estimated_units_json
        text idempotency_key "unique per ws"
        text status "reserved|denied|released|reconciled"
        jsonb decision_json
    }
    usage_events {
        text id PK
        text reservation_id FK
        text workspace_id FK
        text subject
        text operation
        text event_type
        text status "started|succeeded|failed|unknown|denied|released"
        jsonb actual_units_json
        text provider_request_id
    }
    billing_exports {
        text id PK
        text usage_event_id FK
        text source_event_dedupe_key "unique per provider"
        text external_event_id "unique per provider"
        text status "pending|failed|exported"
        int attempt_count
    }
    credit_ledger_entries {
        text id PK
        text workspace_id FK
        text idempotency_key "unique per ws"
        text direction "in|out"
        text unit
        text quantity_text
    }
    polar_webhook_snapshots {
        text id PK
        text webhook_event_id "unique"
        text replay_status
    }
```

The **usage ledger** (`usage_reservations` + `usage_events`) is the source of truth for pre-call
authorization. `billing_exports` is a *mirror* of settled events shipped to Polar — Polar webhooks
(`polar_webhook_snapshots` → `credit_ledger_entries`) are the source of truth for credits. Billing
is therefore decoupled from authorization. Idempotency is enforced by unique constraints:
`(workspace_id, idempotency_key)` on reservations and credits, and `(provider, source_event_dedupe_key)`
+ `(provider, external_event_id)` on exports (`schema.py:151, 313, 342-343`).

### 2.5 Search / Transcripts

```mermaid
erDiagram
    workspaces ||--o{ videos : has
    sources ||--o{ videos : produced
    videos ||--o{ transcript_versions : "versions"
    videos ||--o{ chunks : "chunked into"
    transcript_versions ||--o{ chunks : segments
    search_index_profiles ||--o{ chunks : "indexed under"
    chunks ||--o{ chunk_embeddings : "embedded as"
    search_index_profiles ||--o{ chunk_embeddings : "profile of"

    videos {
        text id PK
        text workspace_id FK
        text youtube_video_id "unique per ws"
        text active_transcript_version_id "pointer"
        text channel_id
        text title
        timestamptz published_at
        int duration_seconds
    }
    transcript_versions {
        text id PK
        text video_id FK
        text source "youtube|hosted|..."
        text language_code
        text content_hash
    }
    search_index_profiles {
        text id PK
        text backend "default postgres_vectorchord"
        text embedding_model "default voyage-4-lite"
        int embedding_dimension "default 1024"
        text chunking_version
        text tokenizer "default yutome_llmlingua2"
    }
    chunks {
        text id PK
        text video_id FK
        text transcript_version_id FK
        text index_profile_id FK
        int chunk_index
        numeric start_seconds
        numeric end_seconds
        text text
        bm25vector bm25_document
        jsonb metadata_json
    }
    chunk_embeddings {
        text id PK
        text chunk_id FK
        text index_profile_id FK
        vector embedding "vector(1024)"
    }
```

Three subtleties worth holding onto:
- **Active transcript is a pointer.** `videos.active_transcript_version_id` selects which immutable
  `transcript_versions` row is live; swapping it atomically switches what gets retrieved
  (`schema.py:504`).
- **Lexical and dense are separate columns/tables.** `chunks.bm25_document` (VectorChord `bm25vector`)
  lives on the chunk row; the dense `vector(1024)` lives in the **separate `chunk_embeddings` table**
  (`schema.py:566-567, 577-589`). Local mode uses this same schema.
- **A search index profile is immutable identity.** Changing backend, model, dimension, chunking
  version, *or* tokenizer yields a new `search_index_profiles` row (unique across all of them,
  `schema.py:551`) and a backfill — never a silent in-place change.

---

## 3. Metering: reserve → settle → release

This is the heart of hosted mode. Every paid or scarce call (provider spend *and* search-store
recall) passes a **pre-call gate** that reserves units, then settles to actual afterward.

### 3.1 Reservation lifecycle

```mermaid
stateDiagram-v2
    [*] --> reserved: gate allows
    [*] --> denied: gate denies
    reserved --> reconciled: settle to actual (refund estimate − actual)
    reserved --> released: cancelled / provider denied / downgraded
    denied --> [*]
    reconciled --> [*]
    released --> [*]
```

`ReservationStatus = reserved | denied | released | reconciled` (`models.py:14`).

### 3.2 The gate decision (pure function)

`UsageGate.reserve` builds a `UsageReservation`; the decision logic in `_decide` is a fixed ladder
(`gate.py:56-104`). It is a pure function of `(allocation, policy, balance, estimate)` — it does not
touch the database. The Postgres path (`ledger.py` `PostgresUsageGate`/`PostgresUsageLedger`) wraps
it in a transaction that `SELECT … FOR UPDATE`s the balance and reservation rows and persists the
unit movements.

```mermaid
flowchart TD
    start([reserve]) --> a{allocation present?}
    a -->|no| dMissing[deny: allocation_missing · hard]
    a -->|yes| b{workspace matches?}
    b -->|no| dMismatch[deny: workspace_mismatch · hard]
    b -->|yes| c{credential_mode≠disabled<br/>and status∉ disabled,invalid?}
    c -->|no| dDisabled[deny: allocation_disabled · hard]
    c -->|yes| d{policy.operation_allowed?}
    d -->|no| dOp[deny: operation_not_allowed · hard]
    d -->|yes| e{estimate ≤ hard limit?}
    e -->|no| dHard[deny: usage_limit_exceeded · hard]
    e -->|yes| f{estimate ≤ soft limit?}
    f -->|no| dSoft[deny: soft_limit_exceeded · SOFT]
    f -->|yes| g{balance has units?}
    g -->|no| dBal[deny: insufficient_balance · hard]
    g -->|yes| ok([allow → status=reserved])
```

The same ladder as a truth table (first failing row wins):

| # | Condition checked | On failure → `reason` | `denial_effect` |
|---|---|---|---|
| 1 | allocation is not `None` | `allocation_missing` | hard |
| 2 | `allocation.workspace_id == workspace_id` | `workspace_mismatch` | hard |
| 3 | `credential_mode ≠ disabled` and `status ∉ {disabled, invalid}` | `allocation_disabled` | hard |
| 4 | `policy.operation_allowed("{subject}.{operation}")` | `operation_not_allowed` | hard |
| 5 | estimate ≤ `hard_limits_by_operation[op]` | `usage_limit_exceeded` | hard |
| 6 | estimate ≤ `soft_limits_by_operation[op]` | `soft_limit_exceeded` | **soft** |
| 7 | `balance.has_units(estimate)` | `insufficient_balance` | hard |
| 8 | — all passed — | `allowed` | — |

`operation_allowed` matches `allow_all_operations`, `"*"`, the exact `"{subject}.{operation}"`, or
the `"{subject}.*"` wildcard (`models.py:154-161`). The hard/soft distinction matters downstream:
only a **soft** denial on a **hybrid** query lets the hosted adapter fall back to lexical (see §7).

> **Fail-closed default.** If the adapter is built without an injected Postgres usage-context
> provider, the defaults return `allocation=None` (`mcp_query.py:1317-1344`) → the gate denies at
> row 1. An unconfigured workspace serves nothing rather than serving unlimited.

### 3.3 The full reserve→settle round trip

```mermaid
sequenceDiagram
    participant A as HostedMcpQueryAdapter
    participant CP as usage_context_provider
    participant G as UsageGate (Postgres)
    participant SS as search store / provider
    participant L as usage ledger (Postgres)

    A->>CP: load allocation + policy + balance
    A->>G: reserve(estimate, idempotency_key)
    alt denied
        G-->>A: reservation(status=denied)
        A->>L: append denied usage_event
        A-->>A: raise usage_denied (403)
    else reserved
        G-->>A: reservation(status=reserved)
        Note over G: balance −= estimate (FOR UPDATE, in txn)
        A->>SS: execute operation
        alt success
            SS-->>A: rows + actual usage
            A->>L: append succeeded event (actual_units)
            Note over L: reconcile: balance += (estimate − actual)
        else failure
            SS-->>A: error
            A->>L: append failed event
        else release (cancel/downgrade)
            A->>L: append released event
            Note over L: refund full estimate
        end
    end
```

### 3.4 Subjects, operations, credential modes

A `subject` is the metered provider or internal service; an `operation` is the action within it; the
`operation_key` is `"{subject}.{operation}"`.

| `subject` (`UsageSubject`, `models.py:11`) | Kind | Representative operations |
|---|---|---|
| `gemini` | external provider | `cleanup_transcript`, `transcribe_media`, `metadata_fetch` |
| `voyage` | external provider | `embed_documents`, `embed_query` |
| `webshare` | external provider | `proxy_fetch` |
| `youtube` | external provider | `transcript_fetch`, `metadata_fetch` |
| `search_store` | internal service | `lexical_query`, `semantic_query`, `hybrid_query`, `list_read`, `resource_read`, `index_write`, `replace_active_transcript` |

`CredentialMode` (`models.py:12`) decides where credentials come from:

| `credential_mode` | Meaning |
|---|---|
| `hosted` | Yutome's own provider keys fund the call (default for `ProviderAllocation`) |
| `byo_hosted` | user-supplied keys on hosted infra (**deferred**) |
| `service_internal` | no external credential (default for `ServiceAllocation`, e.g. `search_store`) |
| `disabled` | deny immediately (gate row 3) |

### 3.5 Idempotency keys & stable hashing

Retries must not double-charge. The idempotency key is built from canonicalized inputs
(`ids.py:37-55`):

```python
# ids.py:37
idempotency_key(
    workspace_id=...,            # tenant
    subject_id=auth.client_id,   # who/what the call is for (client id, or video id for ingest)
    operation="search_store.hybrid_query",
    input_hash_value=input_hash({...}),  # sha256 of canonical JSON (ids.py:21-34)
    extras=[grant_id, client_id, session_id],
)
# components escape ":" and "%" so the joined key is unambiguous (ids.py:54)
```

`input_hash` sorts keys and uses compact separators so harmless key-order differences don't produce
duplicate billable IDs (`ids.py:21-29`). The `(workspace_id, idempotency_key)` unique constraint then
makes a retried reserve return the existing reservation instead of creating a second one.

---

## 4. Authentication

Three distinct credential types, three dependencies in `http_api.py`. Token/secret env vars are
defined at `http_api.py:82-92`.

| Caller | Dependency (`http_api.py`) | Credential | Required headers | Context type |
|---|---|---|---|---|
| Assistant (MCP) | `default_auth_dependency` (`:364`) | `YUTOME_HOSTED_API_TOKEN` bearer | `Authorization`, `X-Yutome-Workspace-Id` (+ optional grant/client/user/session) | `HostedMcpAuthContext` |
| Dashboard | `account_auth_dependency` (`:753`) | `YUTOME_DASHBOARD_API_TOKEN` bearer + session JWT | `Authorization`, `X-Yutome-Account-Session` | `AccountApiContext` |
| CLI | `cli_auth_dependency` (`:813`) | CLI JWT bearer (verified vs `account_grants`) | `Authorization` | `AccountCliApiContext` |

Session/CLI JWTs are signed with `YUTOME_ACCOUNT_SESSION_HMAC_SECRET` and audience-scoped
(`YUTOME_ACCOUNT_SESSION_AUDIENCE`).

### 4.1 Magic-link login (dashboard)

```mermaid
sequenceDiagram
    actor U as User
    participant W as Web app
    participant API as Hosted API
    participant DB as Postgres
    U->>W: enter email (/signup)
    W->>API: POST /account/login/start
    API->>DB: insert email_login_tokens (hashed, TTL)
    API-->>U: email with /auth/verify?token=…
    U->>W: click link (/auth/verify)
    W->>API: POST /account/login/verify {token}
    API->>DB: consume token (one-time), upsert user+workspace+session
    API-->>W: signed session JWT + max_age
    W-->>U: Set-Cookie yutome_account_session; redirect /dashboard
```

Anchors: `/account/login/start` (`http_api.py:542`), `/account/login/verify` (`:616`),
`/account/bootstrap` legacy pairing (`:467`).

### 4.2 CLI authorization (PKCE)

```mermaid
sequenceDiagram
    participant CLI
    participant W as Web app (session)
    participant API as Hosted API
    CLI->>CLI: generate code_verifier + code_challenge
    CLI->>W: open /cli/authorize (browser, dashboard session)
    W->>API: POST /account/cli/authorize {code_challenge, scopes}
    API-->>W: authorization code (pending account_grant, kind=cli_install)
    CLI->>API: POST /account/cli/token {code, code_verifier}
    API->>API: verify PKCE, activate grant, sign CLI JWT
    API-->>CLI: access_token (JWT) + grant_id + expires_at
```

Anchors: `/account/cli/authorize` (`http_api.py:871`, dashboard session), `/account/cli/token`
(`:921`, public PKCE exchange).

### 4.3 MCP auth

The assistant call carries the symmetric `YUTOME_HOSTED_API_TOKEN` plus `X-Yutome-Workspace-Id`;
`HostedMcpAuthContext.validated()` requires a non-empty workspace and the `yutome.search.read` scope
(`mcp_query.py:101-130`). Hosted source writes additionally require `yutome.source.write` and
`yutome.job.write`. Tenant scope, connector grant, assistant client, session, and user identity come
only from this context — never from tool arguments (see §7).

---

## 5. Jobs & sources (ingest plane)

### 5.1 Source import → job enqueue

```mermaid
sequenceDiagram
    participant C as "Dashboard / CLI / MCP index"
    participant API as Hosted API
    participant DB as Postgres
    C->>API: POST /account/sources, /account/sources/import, or tools/call index
    API->>API: reject any credential-shaped descriptor
    loop each source
        API->>DB: upsert sources row
        alt single video
            API->>DB: enqueue index_video job (priority 100)
        else channel / playlist
            API->>DB: create source_refresh_policy
            API->>DB: enqueue discover_source job (priority 100)
        end
    end
    API-->>C: imported sources + jobs + policies
```

Anchors: dashboard import `/account/sources` (`http_api.py:1048`), CLI import
`/account/sources/import` (`:1062`), hosted MCP `index` (`mcp_query.py`).

### 5.2 Job lifecycle

A `jobs` row is leased by a worker (`lease_owner` / `leased_at` / `lease_expires_at`), runs its
`job_operations` sub-steps (each with its own `usage_reservation_id`), and ends terminal.

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> discovering: discover_source
    queued --> preparing: index_video
    discovering --> queued_video_jobs
    preparing --> reserving_usage
    reserving_usage --> fetching_transcript
    fetching_transcript --> fallback_transcription
    fetching_transcript --> cleaning
    fallback_transcription --> cleaning
    cleaning --> embedding
    embedding --> writing_index
    writing_index --> reconciling_usage
    reconciling_usage --> succeeded
    queued --> retry_wait: transient failure
    retry_wait --> queued
    queued --> denied: metering gate denied
    preparing --> failed
    queued --> cancelled
    succeeded --> [*]
    failed --> [*]
    denied --> [*]
    cancelled --> [*]
```

The **scheduler** (one shared loop, not per-workspace) reads `source_refresh_policies` where
`enabled` and `next_run_at ≤ now()` (index `idx_source_refresh_due`, `schema.py:431-436`), locks the
row (`locked_by`/`locked_until`), enqueues a `discover_source` job, and advances `next_run_at`.
**Workers** claim jobs via the claimable index ordered by `(priority, run_after, created_at)` over
`status ∈ {queued, retry_wait}` (`schema.py:465-471`).

---

## 6. HTTP API catalog

Grouped by auth dependency. Method/path anchored to `http_api.py`.

**No auth**
| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` (`:405`) | contract metadata |
| GET | `/readyz` (`:417`) | readiness checks |

**MCP token** (`default_auth_dependency`)
| Method | Path | Purpose |
|---|---|---|
| POST | `/tools/call`, `/mcp/tools/call` (`:439`) | invoke `find`/`list`/`show`/`index`/`jobs`/`q` |
| POST | `/resources/read`, `/mcp/resources/read` (`:455`) | read a `yutome://` resource |

**MCP or dashboard token**
| Method | Path | Purpose |
|---|---|---|
| POST | `/account/bootstrap` (`:467`) | legacy edge OAuth pairing |
| POST | `/account/login/start` (`:542`) | send magic link |
| POST | `/account/login/verify` (`:616`) | redeem magic link → session |

**Dashboard session** (`account_auth_dependency`)
| Method | Path | Purpose |
|---|---|---|
| POST | `/account/cli/authorize` (`:871`) | begin CLI PKCE |
| POST | `/account/sources` (`:1048`) | import sources |
| GET | `/account/source-jobs` (`:1079`) | recent ingest jobs |
| GET | `/account/summary` (`:1101`) | workspace summary |
| GET | `/account/library` (`:1106`) | library overview |
| GET | `/account/assistants` (`:1111`) | connected grants |
| POST | `/account/search` (`:1116`) | dashboard `find` (Phase-1 search slice) |
| POST | `/account/show` (`:1134`) | dashboard `show` |
| POST | `/account/list` (`:1149`) | dashboard `list` |

**CLI token** (`cli_auth_dependency`)
| Method | Path | Purpose |
|---|---|---|
| POST | `/account/sources/import` (`:1062`) | CLI source import |
| GET | `/account/jobs` (`:1089`) | CLI job list |

**Public / signed**
| Method | Path | Purpose |
|---|---|---|
| POST | `/account/cli/token` (`:921`) | PKCE token exchange (no auth) |
| POST | `/billing/polar/webhook`, `/webhooks/polar` (`:697`) | Polar webhook (signature-verified) |

The dashboard `/account/{search,show,list}` endpoints derive the workspace from the session and call
`adapter.call_tool(...)` — the **tenant scope is never read from the request body**.

---

## 7. The hosted query path

`HostedMcpQueryAdapter.call_tool` validates auth, strips forbidden tenant-identifying arguments, and
dispatches by tool name (`mcp_query.py:197-215`). `find` is the interesting one.

- **Default mode is `lexical`** (`HostedFindRequest`, `mcp_query.py:1105, 1128-1135`) — note the contrast
  with the local engine's hybrid default.
- `lexical` → `_find_lexical`; `semantic`/`hybrid` → `_find_vector`.
- **Forbidden arguments**: any `workspace_id`/`tenant`/`grant_id`/`client_id`/`session`/… in the args
  (even nested) is rejected (`FORBIDDEN_TOOL_ARGUMENT_KEYS`, `mcp_query.py:37-63`).
- `show` supports `chunk`, `context`, `video`, `channel`, `transcript`, and `source`
  (`mcp_query.py:794-867`).
- `find` passes offsets and supported filters through to lexical, semantic, and hybrid searches
  (`mcp_query.py:314-323, 443-469`).
- `list attention` is still outside the hosted query contract; hosted lists support `status`, `videos`,
  and `channels` (`mcp_query.py:1238-1271`).

```mermaid
sequenceDiagram
    participant A as adapter._find_vector
    participant G as UsageGate
    participant V as Voyage (embed_query)
    participant SS as search store
    A->>G: reserve search_store (semantic|hybrid_query)
    alt hybrid AND soft-deny
        G-->>A: soft denial
        A->>A: fall back to _find_lexical (tagged)
    else reserved
        A->>G: reserve voyage.embed_query (via ProviderCallContext)
        alt provider usage denied
            A->>A: release search_store reservation
            alt hybrid AND soft-deny
                A->>A: fall back to _find_lexical (tagged)
            else
                A-->>A: raise usage_denied (403)
            end
        else embedded
            V-->>A: query vector
            A->>SS: semantic_search OR hybrid_search
            alt success
                SS-->>A: rows + usage
                A->>A: record success (settle)
            else hybrid AND recoverable error
                A->>A: record failure + fall back to lexical
            else
                A->>A: record failure + raise
            end
        end
    end
```

**The fallback rule, stated precisely** (`_find_lexical_fallback`, `mcp_query.py:420-446`): only a
**hybrid** query degrades to lexical, and only on a soft denial or a recoverable vector-path failure;
the response carries a `hosted_find_fallback_to_lexical` note so the downgrade is visible. A
**semantic** query never silently falls back — it raises instead.

---

## 8. Domain models

The Pydantic models in [`models.py`](../../src/yutome/hosted/models.py) are what the gate and adapter
operate on. Note they are *shaped for the decision*, not 1:1 with the DB columns — e.g.
`EntitlementPolicy` exposes `hard_limits_by_operation` (a `dict[op, UnitMap]`) where the table stores
`hard_limits_jsonb`; `WorkspaceBalance` exposes `remaining_units` + `unlimited_units` where the table
also tracks `used`/`reserved` jsonb.

```mermaid
classDiagram
    class ProviderAllocation {
        +str provider
        +str operation
        +CredentialMode credential_mode = hosted
        +AllocationStatus status = active
    }
    class ServiceAllocation {
        +str service = search_store
        +str operation
        +CredentialMode credential_mode = service_internal
        +str backend
    }
    class EntitlementPolicy {
        +bool allow_all_operations
        +set~str~ allowed_operations
        +dict hard_limits_by_operation
        +dict soft_limits_by_operation
        +operation_allowed(op) bool
    }
    class WorkspaceBalance {
        +UnitMap remaining_units
        +set~str~ unlimited_units
        +has_units(estimate) bool,str
    }
    class UsageDecision {
        +bool allowed
        +str reason
        +UsageDenialEffect denial_effect = hard
    }
    class UsageReservation {
        +UsageSubject subject
        +str operation
        +CredentialMode credential_mode
        +UnitMap estimated_units
        +str idempotency_key
        +ReservationStatus status
        +UsageDecision decision
        +operation_key() str
    }
    class UsageEvent {
        +str reservation_id
        +UsageSubject subject
        +str event_type
        +EventStatus status
        +dict actual_units
    }
    UsageReservation --> UsageDecision : carries
    UsageReservation ..> UsageEvent : settled by
    EntitlementPolicy ..> UsageReservation : gates
    WorkspaceBalance ..> UsageReservation : funds
```

---

## 9. Deferred (documented, not built)

- `byo_hosted` credential mode (user-supplied provider keys on hosted infra).
- **cloud / offline replica** — read-only Cloudflare mirror that answers when the laptop bridge is
  offline. Call it "cloud replica" or "offline replica", never "always-on".
- multi-region Postgres failover / geo-distributed workers.
- entitlement **grace policies** (`grace_policy_jsonb` exists; enforcement deferred).
- hosted `list attention` (not part of the current hosted adapter contract).
