# Yutome docs

For usage and install, see the [top-level README](../README.md). The files in this directory are reference material — some user-facing, some internal design history.

## User-facing

- [`remote-access.md`](remote-access.md) — connecting Claude, ChatGPT, and other agents to Yutome (both local stdio MCP and the remote Cloudflare path).
- [`cli-architecture.md`](cli-architecture.md) — CLI namespace rules and shared command architecture.
- [`query-api.md`](query-api.md) — the query schema that `yutome search find` and `yutome search q` speak; useful when you want richer filtering than the CLI flags expose.
- [`oauth-testing.md`](oauth-testing.md) — how to validate the OAuth + pairing flow against a deployed Cloudflare capsule.
- [`evals.md`](evals.md) — running retrieval-quality evaluations with `yutome doctor eval`.
- [`proxy-strategy.md`](proxy-strategy.md) — when transcript fetches get rate-limited, how to wire up a proxy.

## Internal / design history

These are not getting-started material. They explain *why* things are the way they are.

- [`architecture/README.md`](architecture/README.md) — current architecture map and links to the detailed hosted, frontend, CLI, and engine docs.
- [`plan.md`](plan.md) — current pointer to the architecture docs plus the archived pre-cutover guide.
- [`ytdlp-webshare-decision-log.md`](ytdlp-webshare-decision-log.md) — current `yt-dlp`/Webshare indexing findings, benchmark results, and production-default decisions.
- [`product-design.md`](product-design.md) — user model, design principles, scope decisions.
- [`cloud-capsule-strategy.md`](cloud-capsule-strategy.md) — why the remote connector is a Cloudflare Worker, what milestones look like.
- [`reviewer-handoff.md`](reviewer-handoff.md) — current review entry point and quality gates.
- [`archive/`](archive/) — historical design docs kept for context only.
