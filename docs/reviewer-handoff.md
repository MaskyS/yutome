# Fresh review handoff

Status: current entry point. The old reviewer checklist was archived after the
Postgres + VectorChord cutover because it referenced deleted pre-cutover modules
and tests.

Review in this order:

1. [`architecture/README.md`](architecture/README.md)
2. [`architecture/cli-and-engine.md`](architecture/cli-and-engine.md)
3. [`architecture/hosted.md`](architecture/hosted.md)
4. [`architecture/frontend.md`](architecture/frontend.md)
5. [`hosted-glossary.md`](hosted-glossary.md)
6. [`query-api.md`](query-api.md)
7. [`remote-access.md`](remote-access.md)
8. [`ytdlp-webshare-decision-log.md`](ytdlp-webshare-decision-log.md)

Run the same gates used by the cutover:

```bash
uv run ruff check src tests
uv run python -m compileall -q src tests
uv run pytest -q
uv run yutome doctor contract --json
```

For web and Worker changes, also run:

```bash
(cd web && npm run typecheck)
(cd cloudflare/yutome-capsule && npm run typecheck && npm run test:ts)
```

Historical reference:

- [`archive/reviewer-handoff-pre-postgres-cutover.md`](archive/reviewer-handoff-pre-postgres-cutover.md)
  preserves the old checklist as design history only.
