# CLI Architecture

Yutome's CLI is a human/operator shell over library APIs. It should stay small,
composable, and explicit: add parameters or namespace subcommands over existing
operations before adding a new top-level command.

## Design Rule

New capability = a parameter or subcommand over an existing operation unless it
is a genuinely new user/operator job. There is no plugin registry, command
entry-point system, or third-party command layer. Yutome is a standalone app; the
CLI is not an extension framework.

The model is the read path:

- `QueryRequest` describes the retrieval algebra: entity, search mode, filters,
  projection, grouping, ordering, and pagination.
- `compile_query()` derives the execution plan from that request.
- `api.q()` is the raw primitive.
- `api.find()`, `api.list_()`, and `api.show()` are ergonomic presets over the
  same retrieval contract.

`show` is intentionally documented as a mixed surface. `show video` can route
through the query layer, while `show chunk`, `show transcript`, `show context`,
and `show source` use direct resource lookup, transcript paging, citation
resolution, or neighbor expansion. Tests must cover both the query presets and
these resource/retrieval paths.

## Public Namespaces

Root commands are only first-run and high-traffic status/connection jobs:

- `yutome setup [SOURCE]`
- `yutome connect`
- `yutome disconnect`
- `yutome status`

Retrieval lives under `search`:

- `yutome search find QUERY`
- `yutome search list <videos|channels|attention|status>`
- `yutome search show <chunk|video|channel|transcript|context|source> [ID]`
- `yutome search q REQUEST`

Source registration and indexing live under `corpus`:

- `yutome corpus add SOURCE...`
- `yutome corpus import FILE`
- `yutome corpus import-youtube [TARGET]`
- `yutome corpus select SELECTOR [--off]`
- `yutome corpus sync [SOURCE]`
- `yutome corpus rebuild <vectors|chunks|all>`
- `yutome corpus quality`

Transport adapters live under `serve`:

- `yutome serve mcp`
- `yutome serve http`
- `yutome serve bridge <start|stop|status|install|uninstall>`
- `yutome serve remote <prepare|sync|http|mcp>`

Hosted operations live under `hosted`, with diagnostics under `doctor`:

- `yutome hosted <api|migrate|login|jobs|usage>`
- `yutome hosted source add SOURCE`
- `yutome hosted run <worker|stripe-meter-export|source-refresh|maintenance>`
- `yutome doctor <local|proxy|gemini|eval|contract|remote|hosted-db>`

Exports live under `export`:

- `yutome export <markdown|obsidian>`

## Shared Plumbing

`InvocationContext` is the per-invocation CLI context. The root callback records
the global `--config PATH`; commands read it from Typer's context and load the
runtime lazily. Setup must be able to create a config before other commands
require one, so root setup stays lightweight.

Option groups should be shared where commands have the same cross-cutting shape:
output (`--json`, quiet flags), pagination (`--limit`, `--offset`), corpus
filters, and source selection. Reusing option definitions keeps defaults and help
text from drifting.

Rendering should pass through one output path. JSON output must preserve the
schemas returned by `api.py` and hosted runtime helpers. Text output can change
when it improves operator ergonomics.

## CLI And MCP Boundary

The CLI command tree and MCP tool names are separate surfaces over the same
transport-neutral APIs.

- MCP tools remain `find`, `list`, `show`, and `q`.
- MCP resource URIs remain `yutome://chunk/{id}`,
  `yutome://video/{id}`, `yutome://channel/{id}`, and
  `yutome://transcript/{id}`.
- CLI nesting (`yutome search find`) must not rename or reshape the agent
  contract.

Parity tests should ensure CLI search presets and MCP tools continue to resolve
to the same `api.py` functions and compatible defaults. Full contract generation
from one source can be improved separately.

## Glossary

- InvocationContext: the per-command object holding global CLI state such as the
  config path and lazily loaded runtime.
- Namespace: a Typer sub-application grouping one job family, such as `search`,
  `corpus`, `serve`, `hosted`, `doctor`, or `export`.
- Primitive: the lowest-level operation exposed intentionally, such as
  `search q` over `QueryRequest`.
- Preset: an ergonomic command that builds a primitive request or calls a
  transport-neutral API with defaults, such as `search find`.
- Option group: a reusable set of related CLI parameters with one source for
  defaults and help text.
- Renderer: the shared CLI output path for JSON and text results.

## Guardrails

- No backwards-compatible aliases for removed command paths in development mode.
- No hidden plugin or registry mechanism.
- No `doctor` command should silently mutate state. Contract drift is checked
  with `doctor contract`; deploy flows may refresh artifacts internally.
- No hosted terminology outside `docs/hosted-glossary.md`.
- Do not touch hosted runtime, web, or Cloudflare implementation areas from a
  CLI-only change unless the Beads work is explicitly coordinated.
