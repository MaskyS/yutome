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
| Scanned source lines | 64,696 |
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
| `capsule` | 139 | Current-use retired terminology; should be renamed or isolated as historical |
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

- Extract setup flow, remote connector deployment, bridge service management,
  command streaming, and corpus sync helpers into focused private modules.
- Keep `src/yutome/cli/__init__.py` as the public composition point.
- Keep removed top-level command tests green.
- Do not add old-path aliases.

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

### 4. Retired terms and compatibility shims remain active

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

### 5. Quality gates are too weak for this risk profile

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

### Slice 2: CLI legacy decomposition

Beads: `yt-indexer-sk6.2`

Target modules:

- `src/yutome/cli/_legacy.py`
- `src/yutome/cli/__init__.py`
- existing focused CLI modules under `src/yutome/cli/`

Acceptance:

- `_legacy.py` materially shrinks.
- Public CLI command tree still matches `docs/cli-architecture.md`.
- Removed old top-level paths still fail.
- No new compatibility aliases are added.

Primary tests:

```bash
uv run pytest tests/test_cli_surface.py tests/test_config_paths_db.py tests/test_setup_helpers.py -q
```

### Slice 3: Hosted HTTP route split

Beads: `yt-indexer-sk6.3`

Target module:

- `src/yutome/hosted/http_api.py`

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

Acceptance:

- Request parsing, usage reservation, provider/search calls, and result shaping
  are separate enough to review independently.
- UsageGate remains fail-closed.
- Workspace scoping still rejects tool argument injection.

Primary tests:

```bash
uv run pytest tests/test_hosted_indexing_smoke.py tests/test_hosted_mcp_query.py tests/test_hosted_usage.py tests/test_hosted_search_store.py -q
```

### Slice 5: Retired terminology and stale shim removal

Beads: `yt-indexer-sk6.5`

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

### Slice 6: Quality gate expansion

Beads: `yt-indexer-sk6.6`

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

## Done criteria for the epic

The epic is done when:

- The audit script exists and remains green.
- The biggest hotspot modules have been split along actual responsibility
  boundaries.
- Hosted terminology matches the glossary.
- Hosted pre-production shims are removed unless explicitly justified.
- Public local CLI and MCP contracts remain stable.
- The expanded gates prevent the same artifact classes from silently returning.
- Every completed slice is committed, pushed, and closed in Beads.
