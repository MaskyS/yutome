# Rendered diagrams

Scalable SVG exports of every Mermaid diagram in the architecture docs (rendered with the
`beautiful-mermaid` skill, `github-light` theme). The Markdown chapters embed the same diagrams
inline as Mermaid source — these files are the zoomable, open-anywhere copies. Regenerate by
re-running the render step in the architecture docs' verification flow.

> The command-surface **mindmap** in `cli-and-engine.md` §1 is inline-only — the offline renderer
> doesn't support `mindmap`, though GitHub/VS Code render it.

## System

| File | Diagram |
|---|---|
| `system-context-contract-spine.svg` | Two modes over one contract (README) |
| `hosted-three-plane-topology.svg` | Control / ingest / query planes (README) |

## Hosted (`hosted.md`)

| File | Diagram |
|---|---|
| `hosted-schema-cluster-map.svg` | Schema clusters fanning out from `workspaces` |
| `hosted-er-identity-auth.svg` | ER: users, workspaces, sessions, grants |
| `hosted-er-entitlements-billing.svg` | ER: allocations, policies, balances, price books |
| `hosted-er-usage-metering.svg` | ER: reservations, events, credits, exports |
| `hosted-er-search-transcripts.svg` | ER: videos, transcripts, profiles, chunks, embeddings |
| `hosted-reservation-lifecycle.svg` | State: reserved → reconciled / released / denied |
| `hosted-usagegate-decision.svg` | Flowchart: the gate decision ladder |
| `hosted-reserve-settle-sequence.svg` | Sequence: reserve → settle → release |
| `hosted-auth-magic-link.svg` | Sequence: dashboard magic-link login |
| `hosted-auth-cli-pkce.svg` | Sequence: CLI PKCE authorization |
| `hosted-source-import.svg` | Sequence: source import → job enqueue |
| `hosted-job-lifecycle.svg` | State: ingest job status machine |
| `hosted-query-path-fallback.svg` | Sequence: hosted find with hybrid→lexical fallback |
| `hosted-domain-models.svg` | Class: metering Pydantic models |

## Frontend (`frontend.md`)

| File | Diagram |
|---|---|
| `frontend-route-map.svg` | Route tree (dashboard layout + children) |
| `frontend-bff-data-flow.svg` | Sequence: browser → loader → BFF → hosted API |
| `frontend-session-auth.svg` | Sequence: signup → verify → session cookie |

## CLI & engine (`cli-and-engine.md`)

| File | Diagram |
|---|---|
| `engine-retrieval-algebra-layers.svg` | Primitive / presets / surface layering |
| `engine-queryrequest-model.svg` | Class: QueryRequest + Search + Filter + OrderBy |
| `engine-query-compilation.svg` | Flowchart: QueryRequest → store call |
| `engine-context-expansion.svg` | Sequence: show(kind=context) citation expansion |
| `engine-ingest-pipeline.svg` | Pipeline: discover → … → index |
| `engine-transcript-fetch-chain.svg` | Flowchart: transcript fetch fallback chain |
| `engine-local-postgres-er.svg` | ER: local Postgres workspace schema |
| `engine-contract-adapters.svg` | contract.py feeding the four adapters |
| `engine-remote-access-topology.svg` | Bridge / relay / replica topology |
