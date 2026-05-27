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
| `Any` / TypeScript `any` | 974 | Many are framework/test/JSON boundaries, but hosted domain logic needs review |
| `legacy` | 248 | Expected around `_legacy.py`, suspicious where it preserves removed behavior |
| `capsule` | 183 | Current-use retired terminology; should be renamed or isolated as historical |
| `noqa` | 134 | Mostly tests, but each production suppression should be justified |
| `type: ignore` / TypeScript ignore | 33 | Acceptable at boundary shims; suspicious in core logic |
| compatibility terms | 7 | Needs review because hosted is pre-production |
| `shim` | 1 | Must be justified or removed in hosted pre-production code |
| temporary/TODO/FIXME/HACK | 3 | Small count, easy to triage |

The duplicate route report found `/healthz` and `/readyz` in both the hosted API
and local HTTP server. That is not automatically wrong because they are separate
apps, but it proves the audit can find repeated route names and should be used
carefully rather than blindly.

The contract-definition report found canonical definitions in `contract.py`,
runtime supported-tool definitions in hosted MCP query code, MCP server
registration references, and parity tests. The next cleanup should decide which
of those are source-of-truth definitions and which are derived or test
assertions.

## Repo philosophy this cleanup must preserve

The cleanup is constrained by project docs and Beads memories:

- `docs/cli-architecture.md` says the command-line interface (CLI) should stay
  small, composable, explicit, and grouped under namespaces.
- The public local CLI surface is `setup`, `connect`, `disconnect`, `status`,
  `search`, `corpus`, `serve`, `hosted`, `doctor`, and `export`.
- The Model Context Protocol (MCP) surface remains separate from CLI nesting:
  tools stay `find`, `list`, `show`, and `q`; resource URI templates stay
  stable.
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

These constraints are why the cleanup is aggressive in hosted internals but
conservative around the published local CLI and MCP surfaces.

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
   local MCP tools through `api.py` and `contract.py`. This path is already
   published behavior, so cleanup must be conservative. The stable contract is
   the CLI namespace in `docs/cli-architecture.md` plus MCP tools
   `find`/`list`/`show`/`q`.

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
path mostly gets extraction and test-preserving simplification. The hosted path
can delete shims and reshape internals. The remote connector path needs careful
terminology cleanup because packaging names, docs, and tests are currently
entangled.

## Non-negotiable invariants

Every cleanup slice must preserve or improve these invariants:

| Area | Invariant |
|---|---|
| CLI | Public commands remain `setup`, `connect`, `disconnect`, `status`, `search`, `corpus`, `serve`, `hosted`, `doctor`, and `export`; removed top-level command paths still fail. |
| MCP | Tools remain `find`, `list`, `show`, `q`; resource URI templates stay stable; CLI nesting does not leak into MCP names. |
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

### 2. Hosted HTTP routing is too concentrated

`src/yutome/hosted/http_api.py` contains a long `build_app()` function that
mixes FastAPI app creation, security headers, health/readiness, hosted MCP
tool/resource calls, account bootstrap, dashboard account reads, source import,
session verification, and billing webhooks.

Why this is risky:

- Auth boundaries are load-bearing here. MCP API token auth, dashboard token
  auth, account-session cookie verification, and Polar webhook verification are
  separate concerns.
- A route-level change is hard to review because the function is too large.
- Long route factories encourage adding another nested route instead of
  defining a clear boundary.

What to do:

- Split route registration by domain: health/readiness, MCP calls/resources,
  account/bootstrap/dashboard reads, source import, and billing webhooks.
- Keep dashboard reads unmetered and tenant-derived from verified account
  sessions.
- Keep MCP tools protected by the MCP token and workspace auth context.
- Keep Polar webhook verification isolated from usage authorization.

The safe target shape is:

| Module responsibility | Owns | Must not own |
|---|---|---|
| hosted app composition | FastAPI creation, middleware installation, state wiring, route group registration | Route bodies, SQL, token parsing details |
| MCP route group | `/tools/call`, `/resources/read`, hosted MCP auth dependency | Dashboard account reads, billing webhook verification |
| account route group | bootstrap, login verification, account summary/library/assistants/search/show | Provider metering, MCP workspace header auth |
| source import route group | descriptor validation, source import actor, enqueue/import response | Account session token signing, billing |
| billing route group | Polar signature verification, webhook processing statements | UsageGate decisions |

This shape is better because reviewers can check an auth boundary without
scrolling through unrelated routes. It is worse only in the sense that there
will be more small modules and imports; that is an acceptable cost because the
auth and metering boundaries are more important than file count.

### 3. Hosted indexing and hosted MCP query classes do too many jobs

`HostedIndexingExecutor` and `HostedMcpQueryAdapter` are large classes that
combine request parsing, usage estimation, UsageGate reservation, provider or
search-store calls, persistence, retry/failure classification, and result
formatting.

Why this is risky:

- Usage enforcement must fail closed. Large methods make it easier to reorder a
  provider/search call before a reservation.
- Workspace scoping must come only from verified auth/session context. Large
  request adapters make argument-injection defenses harder to audit.
- Retry and idempotency behavior becomes scattered inside orchestration logic.

What to do:

- Extract request parsing from execution.
- Extract UsageGate orchestration from provider/search-store calls.
- Extract result shaping from tenant and usage enforcement.
- Preserve fail-closed tests and workspace-injection tests.

The target shape is not a generic service layer. It should reflect the actual
job/query lifecycle:

1. Parse and validate the request.
2. Derive tenant and subject from trusted context.
3. Estimate units.
4. Reserve through UsageGate.
5. Execute provider/search-store call.
6. Persist or normalize output.
7. Reconcile reservation and append usage event.
8. Shape response without leaking internal provider state.

The critical rule is that steps 4 and 5 must not be separated by hidden callback
or inheritance behavior. A future reviewer should be able to prove from the code
that a paid call cannot happen before the reservation.

### 4. Local indexing has the worst control-flow concentration

`src/yutome/indexer.py` is local-first published behavior, so it cannot be
reshaped as aggressively as hosted pre-production code. But conservative is not
the same as unscheduled. The audit identified `sync_channel()` as the highest
branch-count function in the repository, and `src/yutome/youtube.py` is another
top module hotspot in the same local acquisition path.

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

- Add a dedicated local-path slice for `indexer.py` and `youtube.py`.
- Preserve published local CLI and MCP behavior.
- Split along behavior boundaries: sync orchestration, provider/fallback
  policy, transcript acquisition, metadata backfill, yt-dlp command/profile
  construction, proxy diagnostics, and error classification.
- Keep tests focused on observed local behavior rather than chasing a lower
  branch-count number.

This slice is required for the epic done criteria. The local path may move more
slowly than hosted cleanup, but it is not out of scope.

### 5. Retired terms and compatibility shims remain active

The hosted glossary retires "capsule" as a current concept and says greenfield
renames should have no alias shims. The repo still has active package paths,
tests, and comments that use capsule terminology.

Why this is risky:

- Terminology drift produces duplicate concepts. A future maintainer has to ask
  whether capsule, worker, connector, relay, and replica are separate things.
- Compatibility shims in pre-production hosted code preserve old mistakes.
- Packaging paths can become accidental public contracts.

What to do:

- Rename current-use capsule paths and helpers to the specific thing they are:
  worker, connector, relay, or replica.
- Leave historical docs only when they explicitly label the old name.
- Remove unnecessary hosted alias shims.

This pass should be coordinated, not search-and-replace. The directory
`cloudflare/yutome-capsule/` is currently part of packaging and tests, so the
rename must account for `pyproject.toml` force-includes, package resource
lookup, contract export, docs, and tests in the same slice. Until then, new code
must not introduce more conceptual uses of "capsule."

### 6. Quality gates are too weak for this risk profile

`pyproject.toml` currently configures Ruff with only `E9` and `F`. That catches
syntax errors and undefined names, but it does not catch broad exceptions,
unused arguments, complexity, dead code, excessive boolean branches, or most
typing-related erosion.

Why this is risky:

- AI-generated code can be syntactically valid and test-passing while still
  hard to review.
- Stricter gates added before simplification would create noisy churn.
- No measurable audit means cleanup can regress silently.

What to do:

- Add an audit script first.
- Use the audit output to drive cleanup.
- Add stricter lint checks after the worst hotspot modules shrink.
- Keep exceptions local and justified, especially in tests with fakes.

The quality gates should be added last because they should enforce the simpler
shape, not become the work itself. A useful gate prevents recurrence of a
specific artifact class. A bad gate produces mass suppressions and teaches the
repo to ignore warnings.

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

It does not immediately split `_legacy.py` or hosted executors. That is
intentional.

Reason:

- The first slice creates the measurement and durable work queue.
- Splitting the largest modules without a baseline would make it hard to tell
  whether the change reduced risk or just moved code around.
- The large splits should each be small, reviewable, behavior-preserving PRs
  with targeted tests.

This is the main tradeoff: one small upfront tooling/documentation change before
larger simplification. The alternative is faster deletion but weaker evidence.

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

### Slice 2: CLI legacy decomposition

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
3. Keep plain helper functions importable through `yutome.cli.__getattr__` until
   tests are moved to import from narrower modules.
4. Preserve direct `yutome.cli._legacy.<symbol>` patch paths until the tests are
   updated in the same PR. `tests/test_setup_helpers.py` and
   `tests/test_config_paths_db.py` monkeypatch many bridge/setup symbols through
   the `_legacy` module namespace; `yutome.cli.__getattr__` does not cover those
   dotted string paths. If a helper moves, either re-import it in `_legacy.py` as
   a temporary compatibility bridge for tests or update the tests to patch the
   new module in the same commit.
5. Move remote connector deployment helpers into a new private module only after
   the ghost command tree is gone; otherwise the extraction hides the stale
   command surface instead of removing it.

Acceptance:

- `_legacy.py` materially shrinks.
- Public CLI command tree still matches `docs/cli-architecture.md`.
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

### Slice 3: Hosted HTTP route split

Beads: `yt-indexer-sk6.3`

Target module:

- `src/yutome/hosted/http_api.py`

Precondition:

- Do not start this slice while unrelated hosted signup/email work is dirty in
  `src/yutome/hosted/http_api.py`, account-read tests, or web signup routes.
  Rebase after that work lands, then split routes. This prevents mixing product
  auth changes with mechanical route extraction.

First code edits:

1. Extract health/readiness registration to a helper that takes only the
   readiness check and sanitized readiness helpers.
2. Extract hosted MCP route registration to a helper that takes the adapter and
   MCP auth dependency. This helper should own only `/tools/call`,
   `/mcp/tools/call`, `/resources/read`, and `/mcp/resources/read`.
3. Extract dashboard/account route registration separately. It must depend on
   account-session verification and account read helpers, not the metered MCP
   adapter.
4. Extract Polar webhook routes separately. They should depend on webhook secret
   verification and billing statements only.
5. Leave `build_app()` as state wiring plus route group registration.

Acceptance:

- `build_app()` becomes composition, not route-body storage.
- Route groups are isolated by auth boundary.
- Dashboard reads are still unmetered and session-derived.
- MCP tools/resources still use hosted MCP auth.

Primary tests:

```bash
uv run pytest tests/test_hosted_http_api.py tests/test_hosted_account_read.py tests/test_hosted_cli_account_api.py -q
```

### Slice 4: Hosted indexing and MCP query split

Beads: `yt-indexer-sk6.4`

Target modules:

- `src/yutome/hosted/indexing.py`
- `src/yutome/hosted/mcp_query.py`
- supporting hosted models/gate/search-store modules as needed

First code edits for `HostedMcpQueryAdapter`:

1. Move `HostedFindRequest`, `HostedShowRequest`, `HostedListRequest`, and
   `HostedQRequest` parsing into a request module. Keep the public parsed shapes
   identical.
2. Move workspace-injection rejection helpers with request parsing, because they
   protect the request boundary.
3. Extract search-store usage reservation and event recording into a small
   collaborator. It should expose operations such as reserve query, record
   success, record failure, and record release.
4. Keep `call_tool()` and `read_resource()` as thin dispatchers.

First code edits for `HostedIndexingExecutor`:

1. Extract job lease helpers and operation persistence helpers into explicit
   collaborators or narrow functions.
2. Extract provider contexts and UsageGate reservation helpers so the executor
   reads as a job lifecycle, not as raw usage plumbing.
3. Keep provider fetch/clean/embed call sites visibly after reservation. Do not
   hide a provider call inside an object constructor or lazy property.
4. Only then split source discovery from index-video execution if the shared
   base class is still carrying too much behavior.

Acceptance:

- Request parsing, usage reservation, provider/search calls, and result shaping
  are separate enough to review independently.
- UsageGate remains fail-closed.
- Workspace scoping still rejects tool argument injection.

Primary tests:

```bash
uv run pytest tests/test_hosted_indexing_smoke.py tests/test_hosted_mcp_query.py tests/test_hosted_usage.py tests/test_hosted_search_store.py -q
```

### Slice 5: Local indexing and YouTube acquisition split

Beads: `yt-indexer-sk6.9`

Target modules:

- `src/yutome/indexer.py`
- `src/yutome/youtube.py`
- local tests around staged sync, yt-dlp runtime, metadata validation, retrieval,
  and CLI corpus sync

Precondition:

- Do not mix this with hosted provider-broker changes. Local-first behavior is a
  published path; hosted search-store and UsageGate contracts should not be
  rewritten in the same slice.

First code edits:

1. Extract the channel-sync stage plan from `sync_channel()`: discovery/source
   selection, primary transcript pass, fallback pass, metadata backfill, and
   quality cleanup should be named phases.
2. Extract provider/fallback policy from transcript acquisition. The code should
   make it obvious when transcript API, yt-dlp subtitle fallback, Gemini, or ASR
   is allowed.
3. Keep recent yt-dlp/Webshare retry decisions intact: python-no-js default,
   current/full fallback, `en` before `en-orig`, and same-language retries for
   retryable process failures.
4. Move YouTube command/profile construction and error classification toward
   pure helpers with targeted tests.
5. Preserve local database writes and status values unless a separate Beads
   issue changes the public behavior.

Acceptance:

- `sync_channel()` no longer owns provider policy, metadata backfill, and output
  reporting as one monolithic control-flow object.
- `youtube.py` separates command/profile construction from error
  classification and transcript parsing.
- Published local CLI/MCP behavior remains stable.
- The audit no longer reports `sync_channel()` as the worst branch-count hotspot,
  or the remaining branches are justified because splitting would hide the
  actual sync phase order.

Primary tests:

```bash
uv run pytest tests/test_indexer_stages.py tests/test_youtube_ytdlp.py tests/test_ytdlp_benchmark_script.py tests/test_config_paths_db.py tests/test_retrieval_exports.py -q
```

### Slice 6: Retired terminology and stale shim removal

Beads: `yt-indexer-sk6.5`

Precondition:

- Run the audit marker report and separate references into four buckets:
  historical docs, package path, current concept, and test fixture. Only current
  concept usage is renamed blindly. Package-path changes require coordinated
  packaging/test updates.

First code edits:

1. Rename helper/function names such as `_tracked_capsule_path` and
   `_deploy_tracked_capsule` only in the same change that updates imports/tests.
2. Update `contract_export.py` and package include paths if the directory is
   renamed.
3. Leave historical strategy docs alone unless the text claims "capsule" is the
   current product concept.
4. Remove hosted pre-production aliases only when tests prove no local published
   CLI/MCP surface depends on them.

Acceptance:

- Current-use capsule terminology is renamed to worker, connector, relay, or
  replica as appropriate.
- Historical docs may keep old names only when clearly labeled historical.
- Hosted pre-production shims are removed instead of preserved.

Primary tests:

```bash
uv run pytest tests/test_contract_parity.py tests/test_config_paths_db.py -q
cd cloudflare/yutome-capsule && npx tsc --noEmit
```

### Slice 7: Quality gate expansion

Beads: `yt-indexer-sk6.6`

Precondition:

- Do not expand lint rules while the largest known generated surfaces are still
  in their current shape. The likely result would be hundreds of suppressions
  and no architectural improvement.

First code edits:

1. Start with rules that catch unused imports/variables and accidental broad
   exception patterns in production code.
2. Exempt tests and generated boundary code explicitly, not globally.
3. Add one rule family at a time and fix violations in the same slice.
4. Make the audit script a documented local check, but do not fail CI on raw
   hotspot counts until the epic has reduced the baseline.

Acceptance:

- Ruff catches selected maintainability issues beyond syntax/undefined names.
- TypeScript gates keep unused/dead code from accumulating where practical.
- Exceptions are local, explicit, and justified.
- The audit script is documented as a local maintenance check.

Primary tests:

```bash
uv run ruff check src tests scripts
uv run pytest -q
cd web && npx tsc -b --noEmit
cd cloudflare/yutome-capsule && npx tsc --noEmit
```

## How to interpret audit results

The audit script is not a linter. A hit is not automatically a bug.

Interpretation rules:

- A large module is a refactor candidate if it mixes responsibilities.
- A long function is acceptable only when splitting would hide the actual flow.
- A broad exception is acceptable in an outer boundary if it sanitizes output
  and preserves failure semantics.
- `Any` is acceptable at third-party boundaries, test fakes, JSON payload edges,
  or framework hooks; it is suspicious inside domain logic.
- Compatibility terms are acceptable in historical docs; they are suspicious in
  current hosted code.
- Generated artifact drift should not become a source of truth unless the file
  is deliberately checked in as a contract snapshot.

## Rejected approaches

The following approaches are explicitly rejected for this epic:

- **Big-bang rewrite.** It would lose the green baseline and mix unrelated
  contract changes.
- **Line-count target as the goal.** A smaller file can still hide the same
  state machine. The goal is clearer authority, tenant, metering, and execution
  boundaries.
- **Compatibility-preserving hosted cleanup.** Hosted is pre-production; keeping
  stale aliases there preserves mistakes. The exception is local published
  CLI/MCP behavior.
- **Generic service/repository layering.** The splits must follow Yutome's real
  lifecycle: request, auth, UsageGate, provider/search call, persistence,
  reconciliation, response.
- **Static analysis first.** Stricter linting before simplification creates
  suppression churn. Add gates after the code shape is better.
- **Terminology-only cleanup.** Renaming without boundary fixes makes the code
  look cleaner while preserving the same coupling.

## Done criteria for the epic

The epic is done when:

- The audit script exists and remains green.
- The biggest hotspot modules have been split along actual responsibility
  boundaries, including the local `indexer.py`/`youtube.py` acquisition path and
  hosted route/query/indexing paths.
- Hosted terminology matches the glossary.
- Hosted pre-production shims are removed unless explicitly justified.
- Public local CLI and MCP contracts remain stable.
- The expanded gates prevent the same artifact classes from silently returning.
- Every completed slice is committed, pushed, and closed in Beads.
