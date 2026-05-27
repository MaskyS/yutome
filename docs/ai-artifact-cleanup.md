# AI Artifact Cleanup Brief

Last updated: 2026-05-27

This document is the implementation brief for Beads epic `yt-indexer-sk6`.
It replaces the earlier high-level cleanup plan with concrete repository
findings, cleanup slices, tradeoffs, and test gates.

## Why this exists

Yutome currently has a green baseline, but a green baseline is not the same as
maintainable code. The concern is not that GPT-5.5-xhigh necessarily inserted a
single obvious defect. The concern is that AI-generated code often passes tests
while leaving behind maintainability artifacts: excess volume, long
orchestration paths, duplicated contracts, compatibility branches, broad escape
hatches, type erasure, and terminology drift.

External evidence supports that failure mode:

- Sonar's GPT-5 code-quality analysis found strong functional output paired
  with unusually high verbosity, cyclomatic complexity, cognitive complexity,
  and code-smell density.
- The OpenAI GPT-5-Codex system-card addendum treats agentic coding as a
  capability that needs sandboxing, review, and verification, not automatic
  trust.
- NIST's Secure Software Development Framework profile for generative AI treats
  generated code as supply-chain input that still needs secure development
  controls.
- OpenSSF's AI software development security guidance calls out vulnerable
  generated results, false dependencies, and review burden as first-class risks.
- Academic work on LLM bug taxonomies and AI-generated smells identifies
  recurring issues around missed corner cases, wrong control flow,
  maintainability smells, and misleading-but-plausible code.

This brief applies those findings to this repository. It is not a generic
"clean up the code" request.

## Current repo baseline

These commands were run before this work started:

```bash
uv run ruff check src tests
uv run pytest -q
cd web && npx tsc -b --noEmit
cd cloudflare/yutome-capsule && npx tsc --noEmit
```

The observed baseline was:

- Ruff passed.
- Pytest collected 594 tests and reported 591 passed, 3 skipped.
- Web TypeScript no-emit typecheck passed.
- Cloudflare Worker TypeScript no-emit typecheck passed.

That means the cleanup must not be justified by vague claims that the project is
broken. The problem is reviewability, coupling, and future defect probability.

## First audit snapshot

After adding `scripts/audit_ai_artifacts.py`, the command below was run against
the repository:

```bash
uv run python scripts/audit_ai_artifacts.py --root .
```

The first snapshot reported:

| Metric | Count |
|---|---:|
| Scanned source files | 177 |
| Scanned source lines | 64,719 |
| Python files | 127 |
| TypeScript files | 50 |
| Module hotspots at 600+ lines | 29 |
| Function hotspots at 80+ lines or 20+ branches | 56 |
| Class hotspots at 120+ lines or 20+ branches | 12 |
| Broad Python exception handlers | 48 |
| Route declarations | 32 |
| Duplicate route declarations by method/path | 2 |
| Contract definition locations | 15 |

Top module hotspots from the snapshot:

| File | Lines | Why it matters |
|---|---:|---|
| `src/yutome/cli/_legacy.py` | 7,142 | Large mixed CLI/setup/remote/bridge/sync helper module behind the newer namespaced CLI |
| `src/yutome/hosted/indexing.py` | 2,964 | Provider calls, UsageGate flow, transcript normalization, persistence, retry behavior, and source discovery share one file |
| `tests/test_config_paths_db.py` | 2,572 | Tests are broad integration scaffolding around legacy CLI and connector setup behavior |
| `src/yutome/hosted/mcp_query.py` | 2,095 | Auth, argument parsing, usage reservation, search-store calls, and result shaping are concentrated |
| `src/yutome/hosted/billing.py` | 1,936 | Billing model, SQL, webhook, export, and debug snapshot behavior are concentrated |
| `src/yutome/hosted/http_api.py` | 1,699 | One API module owns multiple auth and route families |
| `src/yutome/indexer.py` | 1,211 | Local indexing orchestration still has long provider/fallback control flow |
| `src/yutome/youtube.py` | 1,193 | YouTube metadata, transcript, yt-dlp, proxy, and error-classification behavior share one module |
| `src/yutome/hosted/runtime.py` | 1,113 | Hosted runner commands, SQL helpers, worker ticks, and operator flows share one module |
| `src/yutome/query.py` | 1,104 | Retrieval request compilation and local query behavior remain dense |

Top function/class hotspots from the snapshot:

| Symbol | Location | Size / branches | Cleanup implication |
|---|---|---:|---|
| `build_app` | `src/yutome/hosted/http_api.py` | 834 lines / 91 branches | Split route registration and auth-boundary helpers |
| `sync_channel` | `src/yutome/indexer.py` | 466 lines / 111 branches | Split staged sync orchestration from provider/fallback policy |
| `HostedIndexingExecutor` | `src/yutome/hosted/indexing.py` | 1,008 lines / 72 branches | Split usage, provider, persistence, and result phases |
| `HostedMcpQueryAdapter` | `src/yutome/hosted/mcp_query.py` | 782 lines / 58 branches | Split request parsing, gate reservation, search, and formatting |
| `setup` | `src/yutome/cli/_legacy.py` | 313 lines / 38 branches | Move first-run setup into focused setup modules |
| `_acquire_transcript` | `src/yutome/indexer.py` | 157 lines / 31 branches | Separate provider choice from transcript normalization/failure mapping |
| `_find_vector` | `src/yutome/hosted/mcp_query.py` | 146 lines / 13 branches | Split embedding reservation, lexical fallback, vector search, and result assembly |
| `apply_env_to_config` | `src/yutome/env.py` | 114 lines / 39 branches | Consider table-driven env parsing after higher-priority splits |

Marker totals from the snapshot:

| Marker | Count | Interpretation |
|---|---:|---|
| `Any` / TypeScript `any` | 974 | Mostly a triage signal at framework/test/JSON boundaries; do not turn the raw count into a work queue without a concrete drift or safety bug |
| `legacy` | 248 | Expected around `_legacy.py`, suspicious where it preserves removed behavior |
| `capsule` | 183 | Current-use retired terminology; should be renamed or isolated as historical |
| `noqa` | 134 | Mostly tests, but each production suppression should be justified |
| `type: ignore` / TypeScript ignore | 33 | Acceptable at boundary shims; suspicious in core logic |
| compatibility terms | 7 | Needs review in preproduction code when they preserve removed behavior |
| `shim` | 1 | Must be justified or removed in preproduction code |
| temporary/TODO/FIXME/HACK | 3 | Small count, easy to triage |

The duplicate route report found `/healthz` and `/readyz` in both the hosted API
and local HTTP server. That is not automatically wrong because they are separate
apps, but it proves the audit can find repeated route names and should be used
carefully rather than blindly.

The contract-definition report found canonical definitions in `contract.py`,
runtime supported-tool definitions in hosted MCP query code, MCP server
registration references, and parity tests. That is a map, not a mandate to
adjudicate 15 sites. Only consolidate contract definitions when there is a
specific drift bug, a repeated edit burden, or an active change that needs one
source of truth.

## Repo philosophy this cleanup must preserve

The cleanup is constrained by project docs and Beads memories:

- `docs/cli-architecture.md` says the command-line interface (CLI) should stay
  small, composable, explicit, and grouped under namespaces.
- CLI and Model Context Protocol (MCP) are also preproduction. The boundary is
  not backwards compatibility with every existing local command behavior. The
  boundary is the current composable design in `docs/cli-architecture.md`: root
  jobs stay small, retrieval lives under `search`, source/indexing lives under
  `corpus`, transports live under `serve`, and removed old paths should fail
  instead of growing aliases.
- The MCP surface remains separate from CLI nesting: the current intended tools
  are `find`, `list`, `show`, and `q`, and the current intended resource URI
  templates are `yutome://chunk/{id}`, `yutome://video/{id}`,
  `yutome://channel/{id}`, and `yutome://transcript/{id}`. That shape can change
  by explicit Beads decision, but it should not drift as an accidental side
  effect of CLI cleanup.
- Hosted Yutome is pre-production. Beads memory says not to preserve backwards
  compatibility for hosted account, dashboard, CLI, API, or worker behavior
  unless explicitly requested.
- `docs/hosted-glossary.md` is canonical for hosted terms. It explicitly says
  greenfield renames should have no alias shims and retires "capsule" as a
  current concept.
- Yutome UsageGate and the usage ledger own pre-call enforcement; Polar is a
  billing mirror, not an authorization dependency.
- Hosted account/session auth, YouTube source-discovery grants, and provider
  credentials are separate boundaries and must not be collapsed.

These constraints are why the cleanup should be aggressive about deleting
preproduction ghosts and stale aliases, but conservative about introducing
unproven architecture. A long function is not a defect by itself. A split is
worth doing only when it protects a named boundary or makes a specific change
safer to review.

## How to reason about the audit numbers

The line counts and branch counts are triage signals, not the goal. A large file
is not automatically bad, and a small file is not automatically good. The
systems question is whether a module forces one reader to hold unrelated state
machines in their head at the same time.

For this cleanup, a hotspot matters when it crosses one of these boundaries:

- **A public contract boundary**: CLI commands, MCP tool names, resource URI
  templates, hosted HTTP paths, account-session tokens, CLI authorization
  tokens, and checked-in Worker contract JSON.
- **An authority boundary**: account/session auth, CLI grant auth, YouTube
  source-discovery grants, provider credentials, service allocations, and Polar
  webhook verification.
- **A metering boundary**: every provider or search-store call that can spend
  Yutome-owned units must be preceded by a UsageGate reservation and followed by
  one clear reconciliation path.
- **A tenant boundary**: workspace identity must come from verified auth/session
  context, never from user-supplied tool arguments or dashboard request bodies.
- **An execution boundary**: a worker claims a job, an executor runs one job
  type, job operations hold idempotency, and retry/final failure classification
  must be reviewable.

The first implementation passes should reduce boundary mixing. They should not
optimize for vanity metrics such as "make every file under N lines." A split is
worth doing only when the new module has one reason to change and lets tests
assert a load-bearing invariant more directly.

## Current system shape

Yutome currently has three product paths sharing code:

1. **Local-first CLI/MCP path**

   Local commands load `yutome.toml`, operate on SQLite and LanceDB, and expose
   local MCP tools through `api.py` and `contract.py`. This path is
   preproduction, but it has an intentional design: the CLI should be small and
   composable, and MCP tool names should not be mechanically renamed to mirror
   CLI nesting. Cleanup may remove old paths and helper ghosts. It should not
   casually replace the current command/tool algebra without a Beads decision.

2. **User-owned remote connector path**

   `yutome connect --deploy` prepares and deploys the Cloudflare Worker project
   under `cloudflare/yutome-capsule/`, then runs a local bridge so hosted
   clients can reach the laptop corpus. This is operationally real, but the term
   "capsule" is retired as a product concept. Until the coordinated rename
   lands, code should treat the directory name as packaging history, not a
   vocabulary source.

3. **Hosted Yutome path**

   Hosted account/session auth, hosted MCP, hosted jobs, provider broker,
   UsageGate, usage ledger, billing export, and Postgres search store are still
   pre-production. This is where aggressive simplification is allowed. Hosted
   code should prefer a clean contract over compatibility with earlier generated
   shapes.

This matters because "cleanup" means different things in each path. The local
path gets deletion and extraction under the current CLI/MCP design, not
backwards-compatibility shims. The hosted path can delete shims and reshape
internals. The remote connector path gets careful terminology cleanup without
turning a package directory rename into unnecessary deploy churn.

## Non-negotiable invariants

Every cleanup slice must preserve or improve these invariants:

| Area | Invariant |
|---|---|
| CLI | Current commands follow `docs/cli-architecture.md`; removed top-level command paths still fail; no old-path aliases are added. |
| MCP | Current tools remain `find`, `list`, `show`, `q` unless a Beads decision changes them; CLI nesting does not accidentally leak into MCP names. |
| Hosted account reads | Dashboard reads are authenticated by account-session verification and are not metered through the hosted MCP list/search tools. |
| Hosted MCP | Tool calls derive workspace identity from hosted MCP auth context and reject workspace injection in arguments. |
| Usage | Provider/search-store calls are denied before execution when UsageGate denies; unconfigured production usage contexts fail closed. |
| Billing | Polar mirrors usage and customer state; it does not decide authorization before provider calls. |
| Source auth | YouTube OAuth grants authorize source discovery only; they are not Yutome login and not provider credentials. |
| Jobs | Worker claim/lease behavior remains idempotent; retries must not double-charge or duplicate durable outputs. |
| Secrets | Provider credentials, relay tokens, account tokens, and webhook secrets are not returned in readiness, errors, dashboard JSON, or MCP responses. |
| Terminology | Hosted concepts use `docs/hosted-glossary.md`; new synonyms are treated as defects. |

## What is concretely wrong

### 1. The CLI has a large legacy core behind a newer public surface

`src/yutome/cli/_legacy.py` is over 7,000 lines. The public CLI now lives in
namespaced modules such as `src/yutome/cli/search.py`,
`src/yutome/cli/corpus.py`, and `src/yutome/cli/serve.py`, but `_legacy.py`
still carries a large mix of setup flow, remote connector deployment, bridge
management, corpus sync, hosted helpers, command streaming, and old Typer app
objects.

Why this is risky:

- The public CLI architecture says commands should be small and composable.
- A change to one setup or bridge behavior can accidentally affect unrelated
  helpers because the file is a shared dumping ground.
- The module name itself encourages compatibility thinking even where the docs
  say removed command paths should not get aliases.

What to do:

- First remove the stale internal Typer command tree from `_legacy.py`. Tests
  import `yutome.cli.app`, not `_legacy.app`; the old `app`, `export_app`,
  `list_app`, `show_app`, `quality_app`, `mcp_app`, `http_app`, `eval_app`,
  `remote_app`, `bridge_app`, `contract_app`, and `hosted_app` definitions now
  mostly preserve removed command paths as an internal ghost surface.
- Convert functions that are still called by the public namespaced modules into
  plain helpers. Remove their old `_legacy.py` decorators when the public
  command lives in `src/yutome/cli/*.py`.
- Extract setup flow, remote connector deployment, bridge service management,
  command streaming, and corpus sync helpers into focused private modules.
- Keep `src/yutome/cli/__init__.py` as the public composition point.
- Keep removed top-level command tests green.
- Do not add old-path aliases.

The intended first cut is not "split this file into smaller files." The first
cut is to delete the unused command-registration surface, because that removes a
real second command tree and makes the remaining helper imports honest.

### 2. Hosted HTTP routing is concentrated but not the cleanup target

`src/yutome/hosted/http_api.py` contains a long `build_app()` function that
mixes FastAPI app creation, security headers, health/readiness, hosted MCP
tool/resource calls, account bootstrap, dashboard account reads, source import,
session verification, and billing webhooks.

What is real:

- Auth boundaries are load-bearing here. MCP API token auth, dashboard token
  auth, account-session cookie verification, and Polar webhook verification are
  separate concerns.
- Keep dashboard reads unmetered and tenant-derived from verified account
  sessions.
- Keep MCP tools protected by the MCP token and workspace auth context.
- Keep Polar webhook verification isolated from usage authorization.
- Readiness, errors, dashboard JSON, and MCP responses must not leak secrets.

What is churn:

- Splitting `build_app()` into several route modules is high-churn and
  low-value right now. A long route factory is not a defect by itself. It
  becomes dangerous only when it obscures an auth, tenant, metering, or
  secret-redaction invariant.
- Mechanical route extraction can silently reorder middleware, dependency
  wiring, app state, or exception behavior. That risk is higher while hosted
  signup/email work is active nearby.

What to do:

- Do not start a route-module split as a standalone cleanup.
- Add or tighten invariant tests for auth separation, tenant derivation,
  unmetered dashboard reads, and secret redaction.
- Extract a route helper only when a concrete product/security change is already
  touching that route family and the helper carries a real invariant.
- Keep `build_app()` as a route factory until the route factory itself blocks a
  specific change.

### 3. Hosted indexing and hosted MCP query need invariant proof, not generic layers

`HostedIndexingExecutor` and `HostedMcpQueryAdapter` are large classes that
combine request parsing, usage estimation, UsageGate reservation, provider or
search-store calls, persistence, retry/failure classification, and result
formatting.

What is real:

- Usage enforcement must fail closed. Large methods make it easier to reorder a
  provider/search call before a reservation.
- Workspace scoping must come only from verified auth/session context. Large
  request adapters make argument-injection defenses harder to audit.
- Retry and idempotency behavior becomes scattered inside orchestration logic.

What is churn:

- Extracting request parsing, gate reservation, search, persistence, and
  formatting into a set of generic collaborators is the same service-layering
  this document rejects elsewhere. It spreads one lifecycle across files before
  there is a concrete change forcing that shape.

What to do:

- Prove the invariant that no provider or search-store call can execute before
  a UsageGate reservation succeeds.
- Extract only the smallest invariant-bearing function if that makes the proof
  local in code, for example a reserve-then-call path whose failure mode is
  fail-closed.
- Add targeted tests for fail-closed metering and workspace-injection rejection.
- Leave broader executor decomposition until a specific indexing/query change
  is hard because of the current shape.

The critical rule is that reservation and paid call execution must not be
separated by hidden callback or inheritance behavior. A future reviewer should
be able to prove from the code and tests that a paid call cannot happen before
the reservation.

### 4. Local indexing has the worst control-flow concentration

`src/yutome/indexer.py` is local-first preproduction behavior, but it still
serves the current CLI/MCP path. The audit identified `sync_channel()` as the
highest branch-count function in the repository, and `src/yutome/youtube.py` is
another top module hotspot in the same local acquisition path.

Why this is risky:

- Channel sync owns source selection, staged provider policy, per-video
  processing, fallback gating, metadata backfill, status transitions, and output
  reporting in one control-flow object.
- Transcript acquisition mixes provider choice, retry classification, fallback
  eligibility, proxy diagnostics, and transcript normalization.
- YouTube/yt-dlp/Webshare behavior is operationally sensitive and has recent
  real-world tuning. A future local fix should not require reading hosted
  provider-broker code, and a hosted fix should not accidentally change local
  CLI behavior.

What to do:

- Keep a dedicated local-path issue for `indexer.py` and `youtube.py` so the
  hotspot is explicit rather than forgotten.
- Treat branch count as a prompt for review, not a command to refactor.
- Preserve the current composable CLI/MCP philosophy and current behavior
  unless a Beads decision changes it.
- Split along behavior boundaries: sync orchestration, provider/fallback
  policy, transcript acquisition, metadata backfill, yt-dlp command/profile
  construction, proxy diagnostics, and error classification.
- Keep tests focused on observed local behavior rather than chasing a lower
  branch-count number.

This slice is not a prerequisite for closing the audit/planning epic. It is
deferred until a concrete local acquisition change is hard to make safely, or
until the team deliberately chooses to spend a cleanup pass on local sync.

### 5. Retired terms and packaging history are different problems

The hosted glossary retires "capsule" as a current concept and says greenfield
renames should have no alias shims. The repo still has active package paths,
tests, and comments that use capsule terminology.

Why this is risky:

- Terminology drift produces duplicate concepts. A future maintainer has to ask
  whether capsule, worker, connector, relay, and replica are separate things.
- Compatibility shims in preproduction code preserve old mistakes.
- Packaging paths can become accidental public contracts, but a working
  deployment directory is not worth renaming just to fix a word.

What to do:

- Rename current-use helper/function names such as `_tracked_capsule_path` only
  when the local change is small and the new name says what the thing does:
  worker, connector, relay, or replica.
- Leave historical docs only when they explicitly label the old name.
- Remove unnecessary hosted alias shims.
- Do not rename `cloudflare/yutome-capsule/` as part of this cleanup epic. That
  directory rename touches packaging, checked-in contract export paths,
  Wrangler project configuration, deploy scripts, docs, and tests. Do it at a
  natural deploy boundary, or never.

Until then, new code must not introduce more conceptual uses of "capsule."

### 6. Quality gates should stay conservative

`pyproject.toml` currently configures Ruff with only `E9` and `F`. That catches
syntax errors and undefined names, but it does not catch many real Python
footguns.

Why this is risky:

- AI-generated code can be syntactically valid and test-passing while still
  hard to review.
- Broad stricter gates added before simplification would create noisy churn.
- No measurable audit means cleanup can regress silently.

What to do:

- Add an audit script first.
- Keep the audit output as a local review aid, not a raw-count failure gate.
- Add Ruff Bugbear (`B`) as the useful next lint family because it catches real
  footguns with relatively low stylistic churn.
- Do not add complexity/branch-count rules (`C901`, `PLR09xx`) for this epic.
  They punish long-but-linear functions and incentivize hiding control flow.
- Do not add annotation (`ANN`) or broad blind-except (`BLE001`) sweeps across
  legacy code and test fakes. That would produce suppression churn rather than
  better software.

The quality gate should be small enough that a maintainer can keep it green
without losing a day to suppressions. A useful gate prevents recurrence of a
specific artifact class. A bad gate teaches the repo to ignore warnings.

## What is being implemented first

The first implementation slice is Beads issue `yt-indexer-sk6.1`:

1. Add `scripts/audit_ai_artifacts.py`.
2. Add tests for that audit script.
3. Add this document.
4. Run targeted and baseline gates.
5. Close only the audit/documentation child issue if the implementation passes.

The audit script reports JSON to stdout and is deliberately non-public. It is a
maintenance aid, not a product command.

It reports:

- source file count and total lines,
- module line-count hotspots,
- long Python and TypeScript functions/classes,
- branch-count hotspots,
- broad Python exception handlers,
- `Any`, `type: ignore`, `noqa`, legacy, compatibility, shim, capsule, and
  temporary marker counts,
- duplicate route declarations,
- likely duplicated contract definition locations,
- generated artifact drift for common generated paths.

This is better than another prose-only plan because it makes the next cleanup
reviewable. The team can run one command and see whether a proposed cleanup
actually reduced the artifact surface.

## What this first slice intentionally does not do

It does not immediately split `_legacy.py`, hosted routes, or hosted executors.
That is intentional.

Reason:

- The first slice creates the measurement and durable work queue.
- Splitting the largest modules would make it hard to tell whether the change
  reduced risk or just moved code around.
- The best first code change is the dead `_legacy.py` Typer command tree
  deletion because it removes a real duplicate surface.
- The best hosted changes are invariant tests: fail-closed metering, tenant
  identity from auth context rather than arguments, and secret absence from
  readiness/error/dashboard JSON.

This is the main tradeoff: one small upfront tooling/documentation change before
deletion and invariant tests. The alternative is faster refactoring but weaker
evidence and more churn.

## Churn decisions

The following are explicitly deferred or dropped from the cleanup epic unless a
future Beads issue identifies a concrete bug or blocked change:

- Standalone `build_app()` route-module split.
- Broad `HostedIndexingExecutor` or `HostedMcpQueryAdapter` collaborator
  decomposition.
- `cloudflare/yutome-capsule/` directory rename.
- Ruff complexity/branch-count, annotation, or broad blind-except sweeps.
- Raw `Any` and contract-definition count reviews without a concrete drift bug.

## Concrete cleanup order

### Slice 1: Audit tooling and baseline brief

Beads: `yt-indexer-sk6.1`

Commands:

```bash
uv run python scripts/audit_ai_artifacts.py
uv run pytest tests/test_ai_artifact_audit.py -q
uv run ruff check src tests scripts
```

Acceptance:

- Audit JSON is deterministic and useful.
- Tests cover hotspot detection, duplicate routes, marker counts, broad
  exceptions, and generated artifact drift.
- This brief exists and names actual repo risks.

Status: completed. Do not keep iterating on audit metrics before making the
first simplification cut. The audit exists to support code changes, not to
become another analysis project.

### Slice 2: CLI legacy dead command tree deletion

Beads: `yt-indexer-sk6.2`

Target modules:

- `src/yutome/cli/_legacy.py`
- `src/yutome/cli/__init__.py`
- existing focused CLI modules under `src/yutome/cli/`

First code edits:

1. Remove the unused `_legacy.py` internal Typer app tree:
   `app`, `export_app`, `list_app`, `show_app`, `quality_app`, `mcp_app`,
   `http_app`, `eval_app`, `remote_app`, `bridge_app`, `contract_app`,
   `hosted_app`, and their `add_typer` wiring.
2. Remove decorators from helper functions whose public command is already
   registered in focused modules such as `search.py`, `corpus.py`, `serve.py`,
   `doctor.py`, `hosted.py`, and `export.py`.
3. Do not move setup, bridge, or deployment helpers in the same first cut unless
   the move is required to delete the ghost command tree. Deletion is the value;
   helper relocation is a separate change.
4. Keep plain helper functions importable through `yutome.cli.__getattr__` until
   tests are moved to import from narrower modules.
5. Preserve direct `yutome.cli._legacy.<symbol>` patch paths until the tests are
   updated in the same PR. `tests/test_setup_helpers.py` and
   `tests/test_config_paths_db.py` monkeypatch many bridge/setup symbols through
   the `_legacy` module namespace; `yutome.cli.__getattr__` does not cover those
   dotted string paths. If a helper moves, either re-import it in `_legacy.py` as
   a temporary compatibility bridge for tests or update the tests to patch the
   new module in the same commit.
6. Move remote connector deployment helpers into a new private module only after
   the ghost command tree is gone and only if a concrete follow-up change needs
   that extraction.

Acceptance:

- `_legacy.py` materially shrinks.
- Current CLI command tree still matches `docs/cli-architecture.md`.
- Removed old top-level paths still fail.
- No new compatibility aliases are added.

Do not start by moving `setup()` wholesale. The first-run setup flow is a real
state machine with prompting, environment writes, config creation, hosted setup,
source import, and first sync. Moving it before deleting the unused command tree
creates churn without reducing the actual duplicate surface.

Primary tests:

```bash
uv run pytest tests/test_cli_surface.py tests/test_config_paths_db.py tests/test_setup_helpers.py -q
```

### Slice 3: Hosted HTTP boundary invariant tests

Beads: `yt-indexer-sk6.3`

Target module:

- `src/yutome/hosted/http_api.py`

First code edits:

1. Add or tighten tests proving dashboard account reads derive tenant identity
   from the verified account session, not a workspace id in request input.
2. Add or tighten tests proving hosted MCP routes derive workspace identity from
   hosted MCP auth context and reject workspace injection in tool arguments.
3. Add or tighten tests proving readiness, error responses, dashboard JSON, and
   MCP responses do not include provider credentials, relay tokens, account
   tokens, webhook secrets, or raw connection strings.
4. Do not split route modules in this slice. If a helper extraction is needed to
   make one invariant testable, keep it local and avoid moving middleware or app
   state wiring.

Acceptance:

- Dashboard reads are still unmetered and session-derived.
- MCP tools/resources still use hosted MCP auth.
- Secret redaction tests cover hosted HTTP boundary outputs.
- `build_app()` is not mechanically split unless a testable invariant requires
  a narrow helper.

Primary tests:

```bash
uv run pytest tests/test_hosted_http_api.py tests/test_hosted_account_read.py tests/test_hosted_cli_account_api.py -q
```

### Slice 4: Hosted metering and tenant invariant proof

Beads: `yt-indexer-sk6.4`

Target modules:

- `src/yutome/hosted/indexing.py`
- `src/yutome/hosted/mcp_query.py`
- supporting hosted gate/search-store modules as needed

First code edits:

1. Add a fail-closed test proving a denied or unavailable UsageGate prevents the
   provider/search-store call from executing.
2. Add a call-order test proving reservation happens before the paid
   provider/search-store call for hosted indexing and hosted MCP search paths.
3. Add a workspace-injection test proving tool arguments cannot override the
   workspace derived from auth context.
4. Extract only the smallest reserve-then-call helper if it makes the invariant
   local and easier to test. Do not split request parsing, search, persistence,
   and formatting into generic collaborator modules as standalone cleanup.

Acceptance:

- UsageGate remains fail-closed.
- Tests prove reservation happens before provider/search-store execution.
- Workspace scoping still rejects tool argument injection.
- No broad executor decomposition is mixed into this slice.

Primary tests:

```bash
uv run pytest tests/test_hosted_indexing_smoke.py tests/test_hosted_mcp_query.py tests/test_hosted_usage.py tests/test_hosted_search_store.py -q
```

### Slice 5: Local indexing and YouTube acquisition tactical cleanup

Beads: `yt-indexer-sk6.9`

Target modules:

- `src/yutome/indexer.py`
- `src/yutome/youtube.py`
- local tests around staged sync, yt-dlp runtime, metadata validation, retrieval,
  and CLI corpus sync

Precondition:

- Do not mix this with hosted provider-broker changes. Local-first CLI/MCP is
  preproduction, but it follows a different runtime path; hosted search-store
  and UsageGate contracts should not be rewritten in the same slice.
- Do not start this only to reduce the branch-count number. Start it when a
  local acquisition change is hard to make safely, or when the team explicitly
  chooses a local sync cleanup pass.

First code edits:

1. If touching `sync_channel()`, extract the channel-sync stage plan only where
   it makes the real flow clearer: discovery/source selection, primary
   transcript pass, fallback pass, metadata backfill, and quality cleanup should
   be named phases when they are otherwise interleaved.
2. Extract provider/fallback policy from transcript acquisition. The code should
   make it obvious when transcript API, yt-dlp subtitle fallback, Gemini, or ASR
   is allowed.
3. Keep recent yt-dlp/Webshare retry decisions intact: python-no-js default,
   current/full fallback, `en` before `en-orig`, and same-language retries for
   retryable process failures.
4. Move YouTube command/profile construction and error classification toward
   pure helpers with targeted tests.
5. Preserve local database writes and status values unless a separate Beads
   issue changes the intended CLI/MCP behavior.

Acceptance:

- `sync_channel()` no longer owns provider policy, metadata backfill, and output
  reporting as one monolithic control-flow object when this slice is actually
  implemented.
- `youtube.py` separates command/profile construction from error
  classification and transcript parsing.
- Current CLI/MCP behavior remains aligned with `docs/cli-architecture.md`.
- The work is justified by a concrete local acquisition maintenance need, not by
  the audit number alone.

Primary tests:

```bash
uv run pytest tests/test_indexer_stages.py tests/test_youtube_ytdlp.py tests/test_ytdlp_benchmark_script.py tests/test_config_paths_db.py tests/test_retrieval_exports.py -q
```

### Slice 6: Retired terminology and stale shim removal without directory rename

Beads: `yt-indexer-sk6.5`

Precondition:

- Run the audit marker report and separate references into four buckets:
  historical docs, package path, current concept, and test fixture. Only current
  concept usage is renamed directly.
- Treat `cloudflare/yutome-capsule/` as package/deployment history for this
  epic. Do not rename the directory in this slice.

First code edits:

1. Rename helper/function names such as `_tracked_capsule_path` and
   `_deploy_tracked_capsule` only in the same change that updates imports/tests.
2. Leave `contract_export.py`, package include paths, Wrangler project naming,
   and Worker directory names alone unless a separate deployment-boundary issue
   explicitly owns the coordinated rename.
3. Leave historical strategy docs alone unless the text claims "capsule" is the
   current product concept.
4. Remove pre-production aliases only when tests prove the current intended
   CLI/MCP or hosted surface does not depend on them.

Acceptance:

- Current-use capsule terminology is renamed to worker, connector, relay, or
  replica as appropriate.
- Historical docs may keep old names only when clearly labeled historical.
- Pre-production shims are removed instead of preserved when they are not the
  current intended surface.
- The Cloudflare Worker directory is not renamed by this slice.

Primary tests:

```bash
uv run pytest tests/test_contract_parity.py tests/test_config_paths_db.py -q
```

### Slice 7: Conservative Bugbear and audit gate

Beads: `yt-indexer-sk6.6`

Precondition:

- Do not add rules whose main effect is suppression churn. Complexity,
  branch-count, annotation, and broad blind-except sweeps are out of scope for
  this epic.

First code edits:

1. Add Ruff Bugbear (`B`) if the existing code can be made green without broad
   suppressions.
2. Keep `F` for unused imports/undefined names; do not add a separate unused
   ontology project.
3. Make the audit script a documented local check, but do not fail CI on raw
   hotspot counts.
4. If Bugbear produces a noisy legacy/test finding, either fix the real footgun
   or add a narrow per-file exception with a reason.

Acceptance:

- Ruff catches Bugbear footguns beyond syntax/undefined names.
- No complexity, annotation, or broad blind-except lint sweep is mixed in.
- Exceptions, if any, are local, explicit, and justified.
- The audit script remains a local maintenance check.

Primary tests:

```bash
uv run ruff check src tests scripts
uv run pytest tests/test_ai_artifact_audit.py -q
```

## How to interpret audit results

The audit script is not a linter. A hit is not automatically a bug.

Interpretation rules:

- A large module is a refactor candidate if it mixes responsibilities.
- A long function is acceptable only when splitting would hide the actual flow.
- A broad exception is acceptable in an outer boundary if it sanitizes output
  and preserves failure semantics.
- `Any` is acceptable at third-party boundaries, test fakes, JSON payload edges,
  or framework hooks; it is suspicious inside domain logic only when a concrete
  call path would benefit from a stronger type.
- Contract-definition locations are only suspicious when they drift, duplicate a
  repeated edit, or define the same behavior inconsistently.
- Compatibility terms are acceptable in historical docs; they are suspicious in
  current preproduction code when they preserve removed behavior.
- Generated artifact drift should not become a source of truth unless the file
  is deliberately checked in as a contract snapshot.

## Rejected approaches

The following approaches are explicitly rejected for this epic:

- **Big-bang rewrite.** It would lose the green baseline and mix unrelated
  contract changes.
- **Line-count or branch-count target as the goal.** A smaller file can still
  hide the same state machine. The goal is clearer authority, tenant, metering,
  and execution boundaries.
- **Compatibility-preserving preproduction cleanup.** Hosted, CLI, and MCP are
  preproduction. Keeping stale aliases preserves mistakes. The current
  composable CLI/MCP shape is intentional, but old paths should not be kept as
  compatibility shims.
- **Generic service/repository layering.** The splits must follow Yutome's real
  lifecycle: request, auth, UsageGate, provider/search call, persistence,
  reconciliation, response.
- **Static-analysis maximalism.** Complexity, branch-count, annotation, and
  broad blind-except sweeps create suppression churn. Use Bugbear and the audit
  script first.
- **Deployment-affecting terminology cleanup.** Renaming
  `cloudflare/yutome-capsule/` just to retire a word risks packaging and deploy
  churn. Rename active concepts and helpers; defer the directory rename to a
  natural deploy boundary, or never.

## Done criteria for the epic

The epic is done when:

- The audit script exists and remains green.
- The dead `_legacy.py` Typer command tree is removed or explicitly re-scoped by
  a Beads decision before implementation starts.
- Targeted invariant tests cover fail-closed metering, auth-derived tenant
  identity, workspace-injection rejection, and secret redaction on hosted
  boundary outputs.
- Current CLI/MCP behavior follows the composable design in
  `docs/cli-architecture.md`; removed old paths fail and no alias shims are
  added.
- Current-use terminology follows the hosted glossary without a
  deployment-affecting Worker directory rename.
- Ruff Bugbear and the audit script provide a low-churn recurring check.
- High-churn route splits, broad executor decomposition, Cloudflare Worker
  directory rename, complexity lint, annotation lint, raw `Any` review, and raw
  contract-count review are explicitly deferred unless a concrete future issue
  justifies them.
- Every completed slice is committed, pushed, and closed in Beads.
