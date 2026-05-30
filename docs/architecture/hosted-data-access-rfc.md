# RFC — Hosted Postgres data-access architecture

> **Status:** Resolved — Direction A adopted and implemented in `yt-indexer-e6g`
> **Authored by:** Claude Opus 4.7 (handoff to Opus 5.5 for evaluation + implementation)
> **Tracked in bd:** `yt-indexer-e6g`
> **Related docs:** [`hosted-sql.md`](hosted-sql.md) (current Core + psycopg conventions after Direction A)

---

## Resolution

Direction A was adopted. Hosted data access now uses a bounded
`psycopg_pool.ConnectionPool` behind a stable psycopg facade, and FastAPI installs a per-request
connection lease so all DB work in one request reuses the same checked-out connection. JSONB writes
now bind Python values with `psycopg.types.json.Jsonb(value)`, and JSONB reads arrive as decoded
Python values under psycopg's `dict_row`.

The hand-rolled `ThreadLocalConnection` and the old `_json_param` / JSON-string cast-helper pattern
were removed from hosted code. SQLAlchemy Core remains a construction tool for joins, CTEs, dynamic
filters, and upserts; execution still goes through psycopg. The analysis below is kept as historical
record of the pre-resolution state and should not be read as current implementation guidance.

## Historical TL;DR

The hosted backend uses **SQLAlchemy Core only as a SQL-string generator**: `compile_postgres_statement(stmt)` returns `(sql, params)`, then those are executed on a **raw psycopg3 connection** via a hand-rolled `ThreadLocalConnection`. No `Engine`, no SQLAlchemy `Connection`, no execution-time SQLAlchemy. This is the **awkward middle**: it pays for SQLAlchemy's compile cost + dependency, gets **none** of its execution-time value (typing, pooling, result mapping), and forces three things to be hand-rolled — JSONB serialization (`_json_param`/`_json_value` + `::jsonb` casts), per-thread connection lifecycle, and result-row extraction.

Two coherent fixes; the current state is the worst of both:

- **(A) psycopg3-native cleanup** — adopt `psycopg_pool.ConnectionPool`, use `psycopg.types.json.Jsonb` for writes, delete `_json_param`/`_json_value`/the cast rules. Keep Core for *construction* on the queries that genuinely earn it (joins/CTEs). **~3 modules + ~80 call sites; mechanical; ~1–2 days.**
- **(B) Full SQLAlchemy Engine** — `create_engine(postgresql+psycopg://…)`, `Connection.execute(statement)`, automatic typing/pooling/result mapping. **~38+ call sites; larger refactor; ~3–5 days.**

**My recommendation (uncertain, calibrated low–medium confidence):** (A). It directly removes the friction my own modernization just documented, is low-risk, and doesn't preclude (B) later. (B) is the right destination if you want SQLAlchemy to fully earn its keep — but given how much raw SQL must stay (VectorChord, locks, complex aggregations), (A) likely captures 80% of the value for 20% of the work. **A smarter agent should re-evaluate this — I have not measured production traffic, pool sizing pressure, or run benchmarks.**

---

## 1. Why this RFC exists

The user explicitly pushed back twice during a SQL-modernization sweep:

1. **"lol whats up with the 2-5 line functions that actually added more complexity?"** — about a balance UPDATE I wrapped in a builder function + helper. Caused me to add a "Trivial static statements stay raw" rule to [`hosted-sql.md`](hosted-sql.md) and revert `ledger.py`.
2. **"is that all you found during your research that was obvious?"** — about my initial research being limited to obvious Core *syntax* (JSONB `.astext`, `cast(..., JSONB)`, `on_conflict_do_update`), without questioning the *architecture* into which I was retrofitting it.

The second pushback exposed the real issue. This RFC captures the substantive finding, the options, and what a smarter agent needs to evaluate + execute without re-discovering. **It is honest about what I don't know.**

---

## 2. The diagnosis: the "awkward middle"

### 2.1 What the codebase actually does

1. **Query construction** uses SQLAlchemy Core (`select`, `insert`, `update`, `from_yutome.hosted.schema import jobs, sources, …`).
2. **SQL serialization** uses `compile_postgres_statement(stmt)` ([`sqlalchemy_core.py:12`](../../src/yutome/hosted/sqlalchemy_core.py)) — compiles to psycopg pyformat and extracts `(sql_string, params_dict)`.
3. **Execution** wraps `(sql, params)` in a `SqlStatement` dataclass ([`repositories.py:23`](../../src/yutome/hosted/repositories.py)) and calls `connection.execute(stmt.sql, stmt.params)` on a **raw psycopg3 connection** (`psycopg.connect(...)`, [`runtime.py:766`](../../src/yutome/hosted/runtime.py)).
4. **Connection management** uses a hand-rolled [`ThreadLocalConnection`](../../src/yutome/hosted/runtime.py) proxy (`runtime.py:769-811`) that hands each FastAPI worker thread its own permanent psycopg connection. **Unbounded; no health-check; no reset-on-return; no shutdown hook.**
5. **JSONB handling** is manual:
   - **Writes**: `_json_param(value)` → `json.dumps(value)` → bound as a Python `str`; raw SQL casts `%(x)s::jsonb`; Core builders use `cast(_json_param(x), JSONB)` in operator contexts.
   - **Reads**: `_json_value(row.get(...))` → `json.loads` (or pass-through if already a dict via `row_factory=dict_row`).
6. **Result rows** are extracted via hand-rolled `_rows_from_result` / `_one_row_from_result` helpers (multiple copies across modules) that switch on `result.mappings()` / `result.fetchall()` / iteration.

### 2.2 What SQLAlchemy is **not** being used for

| SQLAlchemy execution feature | What the codebase does instead |
|---|---|
| `Engine` with `QueuePool` connection pooling | Hand-rolled `ThreadLocalConnection` (unbounded per-thread, no reset, no health-check) |
| `Connection.execute(statement)` with type bind/result processing | `psycopg.Connection.execute(sql, params)` — bypasses every SQLAlchemy type processor |
| JSONB column type adapter (automatic `dict ↔ jsonb`) | `_json_param` (`json.dumps`), `_json_value` (`json.loads`), `::jsonb` casts |
| Result row mapping (`Row._mapping`, attribute access) | Hand-rolled `_rows_from_result`, `_one_row_from_result` |
| Transaction context manager (`Connection.begin()`) | Explicit `connection.transaction()` via psycopg3 directly |
| Stable parameter contract | SQLAlchemy auto-names params (`workspace_id_1`); tests fakes that keyed off `params["source_id"]` had to be rewritten |

### 2.3 Why "the awkward middle"

The codebase **pays for** SQLAlchemy's:
- Dependency (`sqlalchemy>=2.0`)
- Compile-time cost (`statement.compile(dialect=postgresql, render_postcompile=True)` per query)
- Conceptual surface (the team has to know Core to add queries)

…and **gets, in return**:
- Compile-time column-name checking against `schema.py` (a real but modest win)
- A composable query builder for the genuinely-complex queries (`account_jobs_sql`, `account_read`/`resources` CTEs, billing upserts) — also real

It pays **none** of the execution-time price and gets **none** of the execution-time value. Per [SQLAlchemy's own SQL Expressions FAQ](https://docs.sqlalchemy.org/en/20/faq/sqlexpressions.html), compiling statements to strings *"for execution outside the SQLAlchemy connection"* is something the project *does not encourage*. The intended path is `Connection.execute(statement)` through an `Engine` ([Working with Engines and Connections](https://docs.sqlalchemy.org/en/20/core/connections.html)).

---

## 3. Evidence in the codebase (file:line anchors)

| Symptom | Anchor | Notes |
|---|---|---|
| Compile-to-string helper | `src/yutome/hosted/sqlalchemy_core.py:12` | Returns `(sql_str, params_dict)` via psycopg pyformat |
| `SqlStatement` dataclass (the bridge artifact) | `src/yutome/hosted/repositories.py:23` | `sql: str; params: dict[str, Any]` |
| Raw psycopg connection factory | `src/yutome/hosted/runtime.py:766` | `psycopg.connect(resolved, autocommit=True, row_factory=dict_row)` |
| Hand-rolled per-thread connection | `src/yutome/hosted/runtime.py:769–811` | `ThreadLocalConnection`; comment at `:773` explains the threadpool/transaction motivation |
| Wired in via `HostedCommandRunner.connect` | `src/yutome/hosted/runtime.py:106–117` | One `ThreadLocalConnection` per process; threads create connections on first use; **never closed** |
| `_json_param` (one of two definitions; the other is in `ledger.py`/`runtime.py`/etc.) | `src/yutome/hosted/repositories.py:195`, `src/yutome/hosted/runtime.py:891` | Manual `json.dumps` ; called from 10 hosted modules |
| Explicit transaction wrappers | `src/yutome/hosted/ledger.py:54`, `:120`, `:409`; `src/yutome/hosted/indexing.py:719`, `:1310`, `:1498`; `src/yutome/hosted/http_api.py:1771`, `:1782` | These are the *reason* per-thread connections exist |
| Compile call sites | 38 functions across hosted modules | Found via `grep -rn compile_postgres_statement src/yutome/hosted/*.py` |
| Existing Core upsert pattern (proven path) | `src/yutome/hosted/billing.py:1045` (`upsert_workspace_balance_sql`) | Validated live by `tests/test_hosted_postgres.py::test_live_postgres_executes_core_built_billing_upserts` |
| Canonical Core reference impl (joins + JSONB) | `src/yutome/hosted/source_import.py:347` (`account_jobs_sql`) | Postgres-tested in `tests/test_hosted_postgres.py::test_account_jobs_query_returns_enriched_source_and_video_context` |

**Counts (from grep, current working tree):**
- `_json_param` call sites: 80 across 10 modules (account_cli, account, billing, runtime, jobs, repositories, indexing, ledger, search_store, youtube_oauth_service).
- `compile_postgres_statement` call sites: 38 functions.
- `psycopg_pool` or `ConnectionPool` usage anywhere in repo: **zero**.
- `create_engine` / SQLAlchemy `Engine` usage anywhere in repo: **zero**.
- `ThreadLocalConnection.execute` is the *only* execution path in hosted code (via `__getattr__` proxy to the underlying psycopg connection).

---

## 4. What's been done leading up to this RFC

A SQL-modernization sweep was in progress when this issue surfaced. State as of the most recent commit + uncommitted working tree:

**Converted to Core (already in working tree, validated, lint clean):**
- `account_jobs_sql` in `source_import.py` (catalyst; Postgres-tested).
- `account_cli.py`, `auth_login.py` (Agent B).
- `account_read.py`, `resources.py`, `entitlements.py` (Agent A).
- `runtime.py` — 3 upserts (`ensure_workspace_sql`, `upsert_hosted_source_sql`, `upsert_source_refresh_policy_sql`).
- Plus the pre-existing Core in `billing.py` (upserts), `repositories.py` (usage events/reservations), parts of `indexing.py` (some `insert` upserts).

**Reverted after user pushback (kept raw):**
- All of `ledger.py` — 4 simple/static queries plus 4 lock queries. Now byte-identical to HEAD.

**Examined and left raw (correct outcome under the "earns its keep" rule):**
- `jobs.py` — all UPDATEs are trivial status transitions or lock queries.
- `indexing.py` non-VectorChord — same; the agent rejected `update_job_operation_status_sql` and `complete_job_operation_success_sql` because Core renders `<> ALL(array)` as `NOT IN`, which has *different NULL semantics* and would silently break a lease-guard. **Smarter agent: confirm this judgment.**

**Test fallout from auto-named params (fixed):**
- 4 assertions in `test_hosted_search_store.py` that asserted on `%(workspace_id)s` text / `params["video_id"]` keys → rewritten to assert bound values (`statement.params.values()`) and behavior.
- 1 fake connection (`RecordingSourceImportConnection.execute`) that filtered jobs by `params.get("source_id")` → rewritten to match by value membership.

**Doc + bd memory written:**
- [`docs/architecture/hosted-sql.md`](hosted-sql.md) — the Core conventions guide. **Partly obsolete** under Direction A (the entire "JSONB" section becomes moot) and largely obsolete under Direction B.
- `bd memory hosted-sqlalchemy-core` — same content, surfaces in `bd` sessions.
- Pointers added to `docs/architecture/README.md` and `CLAUDE.md`.

**Suite state:** `tests/test_hosted_postgres.py` + `tests/test_hosted_account.py` + `tests/test_hosted_search_store.py` + `tests/test_hosted_http_api.py` + `tests/test_hosted_mcp_query.py` + `tests/test_postgres_helper_modules.py` + `tests/test_hosted_usage.py` → **143 passed, 0 failed** as of the last validation.

**Concurrency caveat:** a concurrent codex process is also editing the working tree — adding YouTube OAuth subscription import (`dashboard.home.tsx`, `hosted-api.server.ts`, `routes.ts`, new `oauth-testing.md`, `youtube_oauth.py`, `control_plane.py`, `test_hosted_cli_account_api.py`). These touch areas adjacent to but **not overlapping** the hosted query/connection code that this RFC concerns. Any execution-layer refactor needs to coordinate (or wait for codex to land first).

---

## 5. Historical options evaluated

### Option A — Lean psycopg3-native (small, high leverage)

**Thesis:** psycopg3 *already* solves the two friction points I documented (JSONB serialization, connection pooling) with first-class native features. Use them. Stop reinventing them. Keep SQLAlchemy Core for *query construction* where it earns its keep (joins/CTEs/on_conflict — i.e., what's already there post-sweep), but drop the cargo-cult JSONB rules and the hand-rolled pool.

**Concrete deliverables:**
1. **Pool**: replace `ThreadLocalConnection` with a single global [`psycopg_pool.ConnectionPool`](https://www.psycopg.org/psycopg3/docs/advanced/pool.html). Bounded `min_size`/`max_size`, health-check, reset-on-return, timeout. Each call site changes from `self.connection.execute(...)` to `with self.pool.connection() as conn: conn.execute(...)`. Alternative: wrap the pool with the same `__getattr__` proxy interface as `ThreadLocalConnection` so call sites don't change, only the connection acquisition semantics. **Smarter agent: pick.**
2. **JSONB writes**: swap `_json_param(x)` → `Jsonb(x)` across the 80 call sites. Drop the `::jsonb` cast from raw SQL. Drop `cast(..., JSONB)` from Core builders. Delete the two `_json_param` definitions.
3. **JSONB reads**: psycopg3 with `row_factory=dict_row` (already in use) returns jsonb columns as Python dicts automatically — `_json_value` is mostly redundant. Verify and delete where it is.
4. **Doc**: rewrite [`hosted-sql.md`](hosted-sql.md) — the JSONB section reduces to "pass `Jsonb(value)`; reads return dicts." The parameter section (auto-named, never assert on keys) still applies because Core construction still emits the same SQL/params shape.
5. **bd memory `hosted-sqlalchemy-core`**: update or replace with `hosted-data-access-standard`.

**Scope estimate:** ~3 modules touched substantially (`runtime.py` for the pool; `repositories.py` + every module using `_json_param` for the JSONB swap); ~80 mechanical call-site swaps for `_json_param`; ~38 call sites unchanged (Core construction stays). Removes ~80 lines of helper code + ~30 lines of doc rules. **~1–2 days of careful work.**

**What it does *not* fix:**
- Hand-rolled `_rows_from_result` / `_one_row_from_result` (still needed since we're still using psycopg directly).
- The auto-named-param test coupling friction (still applies — but it's a one-time pain that's already done).
- The fact that the codebase will still execute via raw psycopg, so SQLAlchemy's typing/result-mapping value remains untapped.

### Option B — Commit to SQLAlchemy Engine (bigger refactor, full value)

**Thesis:** if you're going to take the SQLAlchemy dependency, use it the way it's designed. An `Engine` + `Connection.execute(statement)` gives you connection pooling, type bind/result processing (auto JSONB ↔ dict), result row mapping, transaction management — all of it.

**Concrete deliverables:**
1. **Engine**: `create_engine("postgresql+psycopg://...", pool_size=..., max_overflow=..., pool_pre_ping=True, ...)` using the [psycopg3 driver](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#psycopg). Replace `ThreadLocalConnection` with the Engine's pool (engine.connect() per request, or `engine.begin()` for transactional work).
2. **Call-site migration**: every `connection.execute(stmt.sql, stmt.params)` → `connection.execute(statement)` (the Core `Statement` object directly). Raw SQL (VectorChord, locks, anything left raw) wrapped in `text("...").bindparams(...)`. **~38+ call sites in Core builders + every raw `connection.execute(sql, params)` site (need to count).**
3. **Drop `SqlStatement`** as a public abstraction — the unit of work becomes the Core `Statement` itself. (Or keep `SqlStatement` as an internal wrapper if there's a portability reason — see Unknowns.)
4. **Drop `_json_param` / `_json_value`** — JSONB columns declared in `schema.py` (`Column("metadata_json", JSONB, ...)`) get automatic `dict ↔ jsonb` bind/result. `_rows_from_result` / `_one_row_from_result` also retire — use `result.mappings().all()` / `.first()`.
5. **Doc**: rewrite [`hosted-sql.md`](hosted-sql.md) from scratch. The whole "JSONB rules" section and "parameter contract" section disappear. The "leave raw" list (VectorChord/locks/DDL) survives but wrapped in `text()`.
6. **Migration strategy**: SqlStatement can coexist with new `Statement`-based execution during transition (an adapter that takes the `Statement`, compiles, executes via the new Connection).

**Scope estimate:** ~38+ Core call sites + all raw `connection.execute(sql, params)` sites (need to enumerate; likely 40–60 total); modifications to every helper that currently takes a `SqlStatement`; possibly invasive changes to `ledger.py` / `indexing.py` / `http_api.py` transaction wrappers. **~3–5 days of careful work; needs an integration test pass against live Postgres for every changed path.**

**What it unlocks:**
- The doc burden I documented becomes ~70% smaller.
- Auto-named-param coupling becomes irrelevant (no one reads `statement.params` from outside).
- Connection-pool semantics are correct by default.
- Future addition of async (`AsyncEngine`) is straightforward if any path needs it.
- Observability hooks (`event.listen(engine, "before_cursor_execute", ...)`) are first-class.

**What it adds:**
- Larger blast radius means higher risk of regression. Needs careful validation.
- Some psycopg3-native features (pipeline mode, server-side cursors) become harder/different to access.
- Transaction semantics through SQLAlchemy `Connection.begin()` differ subtly from psycopg3's `connection.transaction()`. The existing `_transaction` helpers in `ledger.py` need rewrite.

### Option C — Status quo (the awkward middle)

Keep doing what's there. Continue the Core-conversion sweep using the "earns its keep" rule. Continue the JSONB cast rules. Continue the hand-rolled pool.

**This is currently the path.** It is *not* untenable — 143 tests pass; production is presumably working. It's just suboptimal: it pays for two stacks while getting the benefit of neither, and the friction shows up as repeated rules in [`hosted-sql.md`](hosted-sql.md) that exist *because of* the architecture mismatch.

**When C is the right call:**
- If production traffic is low enough that the unbounded-connection risk isn't real.
- If the team has reasons to avoid `Engine` (dependency philosophy, control over execution path) and `psycopg_pool` (one more dependency).
- If the SqlStatement abstraction is load-bearing for the bridge/relay/replica architecture in ways I haven't surfaced (see Unknowns).

### Sub-options worth considering

- **A-minus**: just the pool, skip the `Jsonb()` migration. Lowest scope; biggest immediate production-safety win.
- **A-plus**: A + also drop the `_rows_from_result` helpers in favor of `cursor.fetchall()` directly (already returns dicts via `dict_row`). Tiny additional scope; cleaner.
- **B-staged**: introduce the Engine + use it for *new* queries; migrate existing call sites incrementally. Lower risk but longer in-flight period with two patterns coexisting.
- **B-with-SqlStatement-kept**: keep `SqlStatement` as a passive carrier for the bridge/replica code paths, but execute via Engine in the hosted path. Bridges the local CLI and hosted seams.

---

## 6. Decision factors (matrix)

| Factor | A | B | C |
|---|---|---|---|
| Production-safety win (bounded pool) | ✅ | ✅ | ❌ |
| Eliminates JSONB cast/dumps friction | ✅ | ✅ | ❌ |
| Eliminates auto-named-param test pain | ❌ (one-time done) | ✅ going forward | partially |
| Eliminates `_rows_from_result` helpers | ❌ (or A-plus) | ✅ | ❌ |
| Code surface touched | small (~3 modules) | medium-large (~10 modules) | none |
| Regression risk | low | medium | none (status quo) |
| Doc burden after | reduced (~50%) | reduced (~80%) | unchanged |
| New dependency | `psycopg-pool` (small, already psycopg's siblings) | none new (already have SQLAlchemy) | none |
| Reversible | yes — Pool can be swapped back | harder once call sites change | n/a |
| Unlocks async path later | partially (need AsyncConnectionPool) | yes (`AsyncEngine`) | no |

---

## 7. What I ruled out (and why)

| Ruled out | Why |
|---|---|
| Mass-revert all Core conversions to raw | The user only objected to *trivial* conversions; the join/CTE/upsert ones (account_jobs_sql, billing upserts, account_read CTEs) genuinely earn their keep. Reverting them would lose real value. |
| Continue converting trivial static UPDATEs/SELECTs to Core | Captured in `hosted-sql.md` under the "Trivial static statements stay raw" rule, prompted by the ledger pushback. |
| `update_job_operation_status_sql` / `complete_job_operation_success_sql` to Core | SQLAlchemy renders `<> ALL(array)` as `NOT IN`, which has different NULL semantics and would silently break a lease-guard. Agent C flagged; I concur. **Smarter agent: please verify this by writing a Postgres-backed test with a NULL array element and observing behavior under both renderings.** |
| Inlining literal values (`literal_binds=True` style) | The [SQL Expressions FAQ](https://docs.sqlalchemy.org/en/20/faq/sqlexpressions.html) flags this as "unnecessary and insecure" — passing the resulting strings to the DB. The codebase doesn't do this; we use pyformat + params dict. Not a question; just noting I considered and dismissed. |
| psycopg2 fallback | psycopg3 is already in use everywhere (`runtime.py:766`). psycopg2 doesn't have the same `Jsonb` wrapper or async story. Not worth considering. |
| ORM (declarative classes, sessions) | Out of scope. The codebase uses Core, not ORM. ORM would be a third option ("C+") and is a much bigger conceptual change. **Smarter agent: probably skip unless there's a strong reason.** |
| Async (`AsyncConnectionPool`, `AsyncEngine`) | The FastAPI handlers I read are sync (`def account_source_jobs`, etc.). Adding async would be its own RFC. Direction A/B should preserve the option (psycopg_pool and SQLAlchemy both have async siblings). |

---

## 8. What I searched (with sources)

- **SQLAlchemy 2.0 compiling statements / executing externally**:
  [SQL Expressions FAQ](https://docs.sqlalchemy.org/en/20/faq/sqlexpressions.html) — confirms compile-to-string-for-external-execution is *not* the intended path.
- **SQLAlchemy 2.0 connections + pooling**:
  [Working with Engines and Connections](https://docs.sqlalchemy.org/en/20/core/connections.html);
  [Connection Pooling](https://docs.sqlalchemy.org/en/20/core/pooling.html).
- **psycopg3 connection pool**:
  [`psycopg_pool` docs](https://www.psycopg.org/psycopg3/docs/advanced/pool.html);
  [`psycopg_pool` API](https://www.psycopg.org/psycopg3/docs/api/pool.html);
  [PyPI `psycopg-pool`](https://pypi.org/project/psycopg-pool/).
- **psycopg3 JSON adaptation**:
  [Basic type adaptation — JSON section](https://www.psycopg.org/psycopg3/docs/basic/adapt.html) — `psycopg.types.json.Jsonb(value)` for writes; `row_factory=dict_row` returns dicts for reads.
- **SQLAlchemy ↔ psycopg3 pool integration**:
  [Discussion #12522](https://github.com/sqlalchemy/sqlalchemy/discussions/12522) — confirms you can plug `psycopg_pool` into SQLAlchemy `create_engine` if going Direction B.
- **on_conflict + JSONB binding**:
  [Discussion thread on bindparam JSONB with on_conflict](https://groups.google.com/g/sqlalchemy/c/S-mRpZD4ED4) — informed the cast rules now in `hosted-sql.md`.
- **Codebase search**:
  `grep -rn "ThreadLocalConnection\|connect_postgres\|psycopg.connect" src/yutome/hosted/` — confirmed single execution path.
  `grep -rn "_json_param\b" src/yutome/hosted/` — confirmed 80 call sites across 10 modules.
  `grep -rn "compile_postgres_statement" src/yutome/hosted/` — confirmed 38 call sites.
  `grep -rn "psycopg_pool\|ConnectionPool" src/` — confirmed zero existing usage.

---

## 9. What I haven't searched but matters

A smarter agent should hit these before committing to a direction:

- **psycopg3 + FastAPI threadpool best practices (2025)** — does the official guidance still recommend one global ConnectionPool for sync FastAPI? Any gotchas with worker-thread lifetimes and connection eviction?
- **`AsyncConnectionPool` vs sync** — are any FastAPI handlers in this codebase actually `async def` (I read only a sample)? If any are, the sync pool blocks the event loop, and `AsyncConnectionPool` is required. **`grep -rn "async def" src/yutome/hosted/` is the first check.**
- **VectorChord + psycopg3 wire-protocol gotchas** — VectorChord introduces custom types (`bm25vector`, `vector`). Does psycopg3 need any session-level adapter setup that happens at connection creation? If so, the pool needs a connection-init callback. Check `search_store.py` for SET-level calls.
- **`row_factory=dict_row` + `Jsonb` load behavior** — verify that under `dict_row`, jsonb columns *do* come back as Python dicts (not as wrapped `Jsonb` objects). Should be true but please confirm with a 5-line test.
- **`autocommit=True` + `psycopg_pool` reset behavior** — psycopg_pool by default calls `connection.rollback()` on return. With autocommit, this is a no-op outside explicit transactions; with explicit `connection.transaction()` blocks, the transaction is committed/rolled back inside the `with` block, so by the time the connection returns to the pool there's nothing to roll back. *Should* be fine but verify.
- **Search-store extension state per-connection** — `extension_check_sql` in `search_store.py` queries `pg_extension`. If any code path issues `CREATE EXTENSION` or `SET search_path` per-connection, pool reuse changes semantics. Search the codebase.
- **Connection-pool sizing under Railway's container limits** — the hosted FastAPI runs on Railway. What's the configured DB connection limit? Pool `max_size` must respect it.
- **`bridge/relay/replica` connection model** — the [hosted glossary](../hosted-glossary.md) mentions bridge/relay/replica. Do those use the same `connect_postgres` path or something different? If they share, this RFC affects them too. **`grep -rn "bridge\|relay\|replica" src/yutome/` should orient.**
- **Local CLI mode (`yutome` command)** — does the local CLI mode go through `HostedCommandRunner` (and thus `ThreadLocalConnection`)? Or a separate local-engine path (`api.py`)? If shared, this RFC affects both; if separate, only hosted is in scope.
- **Polar webhook handler concurrency** — `polar_webhook_processing_statements` issues multiple SQL statements that need to land atomically. Verify the transaction semantics survive whichever direction is chosen.
- **Has anyone benchmarked the current connection-create-on-first-use vs a real pool?** No baseline I'm aware of. A 5-minute load test would settle a lot of the "is this an actual production problem" question.
- **SQLAlchemy 2.0 + psycopg3: are there any open known issues with `on_conflict_do_update` + JSONB through the Engine path?** [SQLAlchemy issue #3888](https://github.com/sqlalchemy/sqlalchemy/issues/3888) (referenced earlier) is old (1.x) — confirm resolved in 2.0.
- **Pipeline mode** (psycopg3) for bulk index_video enqueues. Not central to this RFC but worth flagging if doing a refactor anyway.
- **Server-side cursors** for the transcript-chunks read path. Same — flag, not central.

---

## 10. Unknowns / questions for the smarter agent

1. **Is production actually hitting connection-exhaustion?** If yes, A-minus (just the pool) is the urgent fix; the rest can wait. If no, this is a quality/clarity refactor and either A or B is fine on its own merits.
2. **What's the FastAPI threadpool size on Railway?** Default is 40 (Starlette's default). With ThreadLocalConnection that's up to 40 connections per worker process, never reclaimed.
3. **What's the Railway Postgres connection limit?** Need this to size `psycopg_pool.ConnectionPool(min_size=..., max_size=...)`.
4. **Is the `SqlStatement` dataclass load-bearing for the bridge/relay/replica architecture?** I see one use in [`http_api.py:_billing_export_tick`] that constructs statements and passes them to a worker — does the worker live in a different process/connection context? If `SqlStatement` is a portable wire format for "here is some SQL to execute later," that constrains how aggressively we can replace it under Direction B.
5. **Does the bridge/replica path share the hosted connection layer or have its own?** If shared, A or B applies; if separate, this RFC scopes to hosted only.
6. **Is there an async path I missed?** `grep -rn "async def" src/yutome/hosted/` will answer.
7. **Is the `update_job_operation_status_sql` `<> ALL(array)` → `NOT IN` NULL-semantics divergence I'm asserting actually verified?** Should be tested with a NULL element under both renderings before any conversion happens.
8. **Is the team comfortable with one more dependency (`psycopg-pool`) under Direction A?** It's a small, official psycopg sibling; should be fine but flag.
9. **Should `_rows_from_result` / `_one_row_from_result` be unified across modules first** (they appear duplicated in `ledger.py`, `runtime.py`, `source_import.py`)? Independent cleanup; could happen now or as part of the refactor.
10. **What does the hosted runtime shutdown look like?** If the FastAPI app is gracefully shut down (SIGTERM), `psycopg_pool.ConnectionPool` needs `pool.close()` to drain connections. Currently `ThreadLocalConnection` has no shutdown hook — connections just die with the process.

---

## 11. Implementation sketches

### 11.1 Direction A — concrete steps

**Step 1: introduce the pool.**

```python
# src/yutome/hosted/runtime.py
from psycopg_pool import ConnectionPool

class HostedCommandRunner:
    ...
    def connect(self) -> Any:
        if self._pool is None:
            url = postgres_url_from_env(url_env=self.config.database.postgres_url_env)
            if url is None:
                raise HostedRuntimeError(...)
            # min_size/max_size chosen to respect Railway PG connection limit /
            # FastAPI threadpool size. Reset-on-return defaults to rollback().
            self._pool = ConnectionPool(
                conninfo=url,
                min_size=4,
                max_size=20,                              # tune from Railway PG limit
                kwargs={"autocommit": True, "row_factory": dict_row},
                open=True,
                # configure=lambda conn: ...               # extension warmup if needed
            )
        return _PooledConnectionProxy(self._pool)         # see below

class _PooledConnectionProxy:
    """Drop-in replacement for ThreadLocalConnection: every method acquires from
    the pool, runs, releases. Preserves the .execute() / .transaction() facade so
    call sites don't change."""
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
    def execute(self, query, params=None, **kw):
        with self._pool.connection() as conn:
            return conn.execute(query, params, **kw).fetchall()  # adjust to match current contract
    def transaction(self):
        @contextmanager
        def _txn():
            with self._pool.connection() as conn:
                with conn.transaction():
                    yield conn
        return _txn()
```

Trade-off in that sketch: every `.execute()` acquires + releases a connection. That's expensive for chatty paths (the gate/ledger reserves many statements in one logical operation). The *better* shape is to acquire a connection at the start of a hosted request, hold it across all SQL, release at end. That's how SQLAlchemy `Engine.begin()` works; reproducing it under FastAPI means a per-request dependency (`Depends(get_connection)`) that yields a pooled connection. **This is the load-bearing detail Direction A needs to get right.** A smarter agent should design this; my sketch above is a strawman.

**Step 2: swap JSONB writes to `Jsonb`.**

```python
# Before — repositories.py / runtime.py / etc.
def _json_param(value): return json.dumps(value, ...)
# Used as: .values(metadata_json=_json_param(source.metadata_jsonb))

# After
from psycopg.types.json import Jsonb
# Used as: .values(metadata_json=Jsonb(source.metadata_jsonb))
```

For raw SQL: drop the `::jsonb` cast and pass `Jsonb(value)` in the params dict.

For Core builders that use `cast(_json_param(x), JSONB)` in `||` contexts: simply `Jsonb(value)` — `Jsonb` binds with the jsonb oid so `||` works without cast.

**Step 3: delete `_json_param` and `_json_value` everywhere. Update doc.**

**Validation:** existing test suite covers most paths. Add an explicit test that round-trips a complex nested dict through `Jsonb` write → DB → `dict_row` read.

### 11.2 Direction B — concrete steps

**Step 1: Engine.**

```python
# src/yutome/hosted/runtime.py
from sqlalchemy import create_engine

class HostedCommandRunner:
    def engine(self) -> Engine:
        if self._engine is None:
            url = postgres_url_from_env(...)
            self._engine = create_engine(
                url.replace("postgresql://", "postgresql+psycopg://"),
                pool_size=8, max_overflow=12, pool_pre_ping=True,
                connect_args={"autocommit": False},  # use SA transactions
            )
        return self._engine
```

**Step 2: convert call sites.**

```python
# Before
rows = connection.execute(statement.sql, statement.params).fetchall()

# After
with engine.connect() as conn:
    rows = conn.execute(statement).mappings().all()    # statement is a Core Statement
```

Per-request dependency in FastAPI:
```python
def get_conn(request):
    with engine.connect() as conn:
        yield conn
```

**Step 3: raw SQL** (VectorChord, locks) wrapped in `text("...").bindparams(...)`.

**Step 4: drop `SqlStatement`** unless it's load-bearing elsewhere (see Unknowns).

**Step 5: rewrite `_transaction`** helpers in `ledger.py` / `indexing.py` to use `connection.begin()`.

**Validation:** every call site needs eyes. Add live-PG round-trip tests for ledger/billing/jobs paths if they don't already exist.

---

## 12. Risks / sharp edges

| Risk | Direction affected | Mitigation |
|---|---|---|
| Per-request connection-lifetime change breaks `with connection.transaction()` patterns in ledger/indexing | A and B | Both need to make sure a hosted request holds *one* connection across its whole SQL flow, not one per statement |
| VectorChord types need session-level setup at connection creation | A and B | `ConnectionPool(configure=...)` callback (A); `event.listen(engine, "connect", ...)` (B) |
| autocommit + pool rollback-on-return semantics | A | Verify with a focused test before rollout |
| `row_factory=dict_row` returning `Jsonb`-wrapped values instead of dicts | A | One-line test; almost certainly returns dicts |
| `<> ALL(array)` semantic break if jobs.py UPDATEs are ever converted | B (if pursued for those queries) | Postgres-backed test with a NULL element under both renderings |
| Concurrent codex process still editing some files | A and B | Coordinate (this RFC is scoped to query/connection layer; codex is in OAuth/frontend) |
| Test fakes (`RecordingConnection`, `RecordingSourceImportConnection`) depend on the current `.execute(sql, params)` shape | A | Stays the same. |
|  ↑ same | B | Needs rewrite to the SA `Connection` shape (or keep as adapters) |
| Loss of compile-time visibility into params under Engine | B | SA log facilities + `connection.execute` events provide good observability |
| Connection-pool exhaustion if `max_size` too low | A and B | Tune from Railway PG limit; alert |
| Pool not closed on shutdown | A and B | Add a FastAPI shutdown event |

---

## 13. Test strategy

Whichever direction:

1. **Baseline**: run the existing hosted suite, capture green.
2. **Targeted live-PG tests** (use `tests/test_hosted_postgres.py` as the harness — it auto-starts a disposable `postgres:16-alpine`):
   - JSONB round-trip for `Jsonb` writes (Direction A) or auto type processing (Direction B).
   - Balance UPDATE with concurrent transactions (ledger path).
   - Job claim under simulated contention (skip-locked path).
   - `<> ALL(array)` with a NULL element vs `NOT IN` (if any conversion of those queries is attempted).
3. **Smoke test** the hosted FastAPI app end-to-end (the dashboard at `localhost:5173/dashboard` against the hosted API at `:8000`):
   - Sign-in, add a source, watch jobs land.
   - Run a search; check no regressions in the Activity feed.
4. **Pool sizing**: brief load test (e.g., `hey` or `k6`) hitting `/account/source-jobs` to verify pool acquisition under burst and graceful return.
5. **Shutdown**: SIGTERM the API process; verify pool drains without errors.

Do **not** stop at "143 tests still pass" — much of that suite uses fake connections that won't catch live-PG behavior changes.

---

## 14. Sources

### SQLAlchemy
- [SQL Expressions FAQ — including the "compiling to string" caveats](https://docs.sqlalchemy.org/en/20/faq/sqlexpressions.html)
- [Working with Engines and Connections](https://docs.sqlalchemy.org/en/20/core/connections.html)
- [Connection Pooling](https://docs.sqlalchemy.org/en/20/core/pooling.html)
- [Engine Configuration (psycopg3 driver: `postgresql+psycopg`)](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#module-sqlalchemy.dialects.postgresql.psycopg)
- [Using the Psycopg 3 connection pool in SQLAlchemy — discussion #12522](https://github.com/sqlalchemy/sqlalchemy/discussions/12522)
- [Psycopg 3 vs SQLAlchemy connection pool — discussion #7478](https://github.com/sqlalchemy/sqlalchemy/discussions/7478)
- [bindparam + on_conflict + JSONB column](https://groups.google.com/g/sqlalchemy/c/S-mRpZD4ED4)

### psycopg3
- [`psycopg_pool` — Connection Pool](https://www.psycopg.org/psycopg3/docs/advanced/pool.html)
- [`psycopg_pool` API reference](https://www.psycopg.org/psycopg3/docs/api/pool.html)
- [PyPI `psycopg-pool`](https://pypi.org/project/psycopg-pool/)
- [Basic type adaptation — JSON section (`Jsonb`, `Json`)](https://www.psycopg.org/psycopg3/docs/basic/adapt.html)

### Codebase
- [`docs/architecture/hosted-sql.md`](hosted-sql.md) — current Core conventions (partly obsolete under A or B)
- [`docs/architecture/hosted.md`](hosted.md) — broader hosted architecture
- [`docs/hosted-glossary.md`](../hosted-glossary.md) — terminology (bridge/relay/replica)
- [`CLAUDE.md`](../../CLAUDE.md) — Postgres-diagnostics section + writing standard

---

## 15. Appendix — codebase realities the agent should know

These are constraints + conventions that aren't obvious from the code alone:

1. **Greenfield, pre-launch.** No backwards-compat constraints. Per the user memory `feedback_no_backcompat_yutome`: pick the right design, don't migrate gradually for compat reasons.
2. **`bd` issue tracker, not TodoWrite/markdown.** Per `CLAUDE.md`. File issues for any follow-ups discovered during execution.
3. **Hosted SQL is built with SQLAlchemy Core (per `CLAUDE.md` Postgres section)** — that note will need updating after either direction.
4. **VectorChord** (`tensorchord/vchord-suite:pg17`) is the Postgres distribution. It adds `bm25vector` and `vector` column types. Raw SQL is required for those — neither A nor B eliminates it.
5. **Disposable Postgres for tests**: `tests/test_hosted_postgres.py::live_postgres_dsn` fixture auto-starts `postgres:16-alpine` via docker. Set `YUTOME_TEST_POSTGRES_DSN` to use a running instance instead. There's a long-running `yutome-vchord-live` container on `127.0.0.1:5432` but **its credentials should not be extracted** — use the auto-fixture or your own throwaway.
6. **The user prefers**:
   - Direct, calibrated responses (no overselling).
   - "Don't pin all CPU cores without asking" — heavy benchmarks/ML stays explicit. A 5-minute `hey` load test against localhost is fine; a sustained benchmark needs sign-off.
   - Greenfield design > closest-fork migration.
   - "Don't overuse multi-option questions; propose a recommendation up front."
7. **Concurrent codex process** is also editing the working tree (YouTube OAuth subscription import flow). Stay out of `youtube_oauth.py`, `control_plane.py` OAuth changes, `dashboard.home.tsx`/`hosted-api.server.ts` OAuth additions, `oauth-testing.md`. Coordinate or wait for them to land.
8. **Frontend `SourceJob` type** in `web/app/lib/hosted-api.server.ts` and **`activity.ts`** consume the enriched job rows from `account_jobs_sql`. If Direction B is chosen, the result-row shape going over the HTTP wire stays the same (FastAPI serializes the dict either way) — frontend is unaffected. **Verify this assumption.**
9. **Two competing `_json_param` definitions** exist (`repositories.py` and `runtime.py`/`ledger.py`). They're effectively identical (`json.dumps`) but unify them while you're there.
10. **`_rows_from_result` / `_one_row_from_result` helpers are duplicated** across `ledger.py`, `runtime.py`, `source_import.py`, possibly elsewhere. Worth unifying in the same pass.
11. **Writing standard** (in `CLAUDE.md`): define a term before first use, prefer precise conditions, expand acronyms first use, cut marketing prose, be verbose where load-bearing.
12. **The doc you're reading right now**: written under the same standard. If you (smarter agent) find it imprecise anywhere, fix it.

---

*Authored 2026-05-27 by Claude Opus 4.7. Calibrated low-medium confidence on the recommendation; high confidence on the diagnosis.*
