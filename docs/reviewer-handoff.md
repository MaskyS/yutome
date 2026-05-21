# Fresh Review Handoff

This checklist is for an engineer or agent reviewing `ytkb` from a clean start. The canonical guide is `docs/plan.md`; read it first. This handoff is a task-oriented companion that says where to look, what to run, and what questions to answer.

## Review Objective

Review the project as a retrieval-first YouTube knowledge base indexer.

Primary questions:

- Does the current design make the channel corpus durable and rebuildable?
- Does retrieval return useful, citation-ready hits without bloating agent context?
- Are transcript acquisition, fallback, proxy, and rate-limit behaviors safe enough for channel-scale indexing?
- Are exports useful to humans and tools without depending on one proprietary workflow?
- Are implemented commands clearly separated from target commands that still need work?
- Are scheduler/job assumptions realistic for old channels with thousands of videos?
- Is the next implementation slice correctly prioritized?

Do not begin with a UI, a built-in answer command, or topic-card generation. Those depend on retrieval quality and inspectability.

Corpus scope assumptions to verify:

- Public videos and completed lives are in scope.
- Shorts are skipped unless explicit support is added.
- Comments and related website crawling are out of scope for the current product.
- Audio/video files are temporary fallback inputs, not durable artifacts by default.

## First Files To Read

Read in this order:

1. `docs/plan.md`
2. `docs/query-api.md`
3. `docs/proxy-strategy.md`
4. `pyproject.toml`
5. `ytkb.toml`
6. `src/ytkb/cli.py`
7. `src/ytkb/config.py`
8. `src/ytkb/db.py`
9. `src/ytkb/indexer.py`
10. `src/ytkb/youtube.py`
11. `src/ytkb/transcripts.py`
12. `src/ytkb/chunking.py`
13. `src/ytkb/store.py`
14. `src/ytkb/embeddings.py`
15. `src/ytkb/query.py`
16. `src/ytkb/api.py`
17. `src/ytkb/retrieval.py`
18. `src/ytkb/mcp_server.py`
19. `src/ytkb/http_server.py`
20. `src/ytkb/exports.py`
21. `src/ytkb/maintenance.py`
22. `tests/test_config_paths_db.py`
23. `tests/test_retrieval_exports.py`

Why these files matter:

| File | Review focus |
| --- | --- |
| `docs/plan.md` | Product shape, architecture, decisions, open questions, next work. |
| `docs/query-api.md` | Current query verbs, raw primitive, and projections. |
| `docs/proxy-strategy.md` | Provider order, proxy policy, block handling, ASR policy. |
| `pyproject.toml` | Dependency boundaries and optional extras. |
| `ytkb.toml` | Active local config and defaults supplied by code. |
| `cli.py` | Current user-facing API and option semantics. |
| `config.py` | Defaults and validation. |
| `db.py` | Canonical schema and invariants. |
| `indexer.py` | Per-video processing, retries, fallback selection, statuses. |
| `youtube.py` | YouTube provider integrations, proxy behavior, block detection. |
| `transcripts.py` | Normalized segment model and artifact writes. |
| `chunking.py` | Chunk size, overlap, forced splitting, chunk ids. |
| `store.py` | Catalog writes, FTS rebuilds, active transcript handling. |
| `embeddings.py` | Voyage batching, retries, LanceDB rows, FTS index creation. |
| `query.py` | Declarative QueryRequest schema, compiler, SQL/Lance execution, projections. |
| `api.py` | Transport-neutral find/list/show/q verbs and resource helpers. |
| `retrieval.py` | Shared citation/context formatting and chunk lookup helpers. |
| `mcp_server.py` | MCP tool/resource wiring for find/list/show/q. |
| `http_server.py` | Loopback REST wiring for the same verbs and resources. |
| `exports.py` | Portable and Obsidian Markdown output. |
| `maintenance.py` | Rebuild behavior from canonical artifacts. |
| `tests/` | Current coverage and missing coverage. |

## Commands To Run

Health and status:

```bash
uv run ytkb doctor
uv run ytkb list status
uv run pytest -q
```

Retrieval smoke tests:

```bash
uv run ytkb find "Crohn probiotics" --mode hybrid --limit 5 --json
uv run ytkb find "donepezil AChEI" --mode hybrid --limit 5 --json
uv run ytkb find "neuroautoimmune disease" --mode hybrid --limit 5 --json
uv run ytkb find "complex disease diagnosis" --mode hybrid --limit 5 --json
```

Compare retrieval modes:

```bash
uv run ytkb find "Crohn probiotics" --mode lexical --limit 5 --json
uv run ytkb find "Crohn probiotics" --mode semantic --limit 5 --json
uv run ytkb find "Crohn probiotics" --mode hybrid --limit 5 --json
```

Context expansion:

```bash
uv run ytkb show context CHUNK_ID --token-budget 3000
uv run ytkb show context "https://youtube.com/watch?v=VIDEO_ID&t=123s" --token-budget 1800
```

Export checks:

```bash
uv run ytkb export portable-md
uv run ytkb export obsidian
```

Index consistency:

```bash
sqlite3 data/indexes/catalog.sqlite "
SELECT COUNT(*) AS chunks FROM chunks;
SELECT COUNT(*) AS indexed_embeddings FROM embeddings WHERE index_status='indexed';
SELECT chunker_version, COUNT(*) AS chunks, MAX(token_count) AS max_tokens
FROM chunks
GROUP BY chunker_version;
SELECT ingest_status, COUNT(*)
FROM videos
GROUP BY ingest_status
ORDER BY COUNT(*) DESC;
"

uv run python - <<'PY'
import lancedb
t = lancedb.connect("data/indexes/lancedb").open_table("chunks")
print("rows", t.count_rows())
print("columns", t.schema.names)
print("indices", [getattr(i, "name", str(i)) for i in t.list_indices()])
PY
```

## Expected Current State

Expected `ytkb list status`:

```text
{
  "searchable_now": "... indexed videos ...",
  "still_indexing": "... discovered/pending/metadata videos ...",
  "needs_attention": "... failed/deferred videos ...",
  "channels": "...",
  "videos": "...",
  "chunks": "...",
  "embeddings": "...",
  "statuses": {"indexed": "..."}
}
```

Expected retrieval/index properties:

- `timestamp-aware-v2` chunks.
- Max chunk size at or below `1000` estimated tokens.
- SQLite chunk, indexed embedding, and LanceDB row counts agree for active chunks.
- LanceDB table includes the required metadata columns from `docs/plan.md`.
- LanceDB has an FTS index on `text`.
- SQLite has both `chunks_fts` and `videos_fts`.
- `uv run pytest -q` passes.

## Review Focus Areas

### Retrieval Quality

Check:

- Are hybrid results actually better than lexical or semantic alone for mixed queries?
- Do exact biomedical terms surface expected chunks?
- Do broad conceptual queries find relevant paraphrases?
- Are too many adjacent chunks from one video crowding out other videos?
- Are snippets bounded and useful?
- Are scores exposed clearly enough for debugging?

Likely next fixes:

- Add retrieval fixtures.
- Add per-video caps.
- Collapse adjacent chunks.
- Add `--diversify` and `--dense` modes.
- Add exact-term query expansion or aliases only after fixtures show the failure mode.

### Context Safety

Check:

- Thin retrieval results do not include full chunk text.
- `project=chunk` includes full chunk text only when requested.
- `project=metadata` includes descriptions only when requested.
- `show context` respects token budget.
- Neighbor expansion dedupes overlap.
- Citations include timestamp URLs and transcript source provenance.

### Transcript Quality

Check:

- Manual captions are preferred over generated captions where provider APIs make that available.
- Preferred language handling avoids translated captions by default.
- Mislabeled generated captions are not blindly accepted.
- `yt-dlp` subtitle fallback is available and records attempts.
- Gemini fallback is explicit and versioned as a transcript source.
- ASR is explicit and not the normal path for channel-scale imports.
- Transcript artifacts include `raw.json`, `normalized.jsonl`, `transcript.txt`, `transcript.md`, `transcript.vtt`, and optional `transcript.srt`.
- Active transcript versions do not make older versions/addressable citations disappear.

### Provider And Proxy Behavior

Check:

- Block/rate-limit markers are classified as retryable/deferred.
- Per-video commits make runs resumable.
- Proxy secrets are redacted in diagnostics.
- Generic proxy pool selection and Webshare behavior match `docs/proxy-strategy.md`.
- `yt-dlp` subprocess timeout prevents long hangs.
- ASR audio downloads do not use residential proxy bandwidth by default.
- Backfill fetches full metadata by default. `--defer-metadata` is an explicit faster historical-import mode; discovery still stores title/duration/thumbnail and an approximate upload date when YouTube's channel listing exposes one.
- `--staged-fallback` is available for single-command fast import: transcript API first, unresolved queue second with `yt-dlp` fallback, with explicit stage logs.
- `--no-yt-dlp-fallback` remains available only for a transcript-API-only diagnostic pass.
- `--yt-dlp-first` is available for transcript API block situations.
- Provider-level circuit breakers are designed, or clearly identified as missing work.

### Rebuild Reliability

Check:

- `rebuild-chunks` can regenerate chunks from active normalized transcripts.
- `rebuild-vectors --resume` only embeds pending chunks.
- Full `rebuild-vectors` can recreate LanceDB from SQLite chunks.
- Stale LanceDB schema produces a clear error.
- Failed embedding batches stay pending.

### Export Quality

Check:

- Portable Markdown has valid frontmatter and timestamp links.
- Obsidian Markdown uses standard Markdown/YAML plus safe Obsidian-compatible block ids.
- Exports do not require summaries.
- Descriptions are bounded.
- File names are deterministic enough for repeated exports.

### Scheduler And Job Model

Check:

- `jobs` table fields are sufficient for idempotent, locked, retryable jobs.
- Scheduler config has clear bounded-run defaults.
- Planned scheduler commands are not documented as if already implemented.
- A future cron/launchd wrapper would call the same sync commands as a human, not duplicate indexing logic.

### Artifact And Manifest Quality

Check:

- Artifacts are machine-stable and not just a human folder UI.
- Metadata/transcript snapshots are versioned enough for citation stability.
- Chunk ids include video id, transcript version id, timestamp range, text hash, and chunker version.
- Tool/provider version manifests are either implemented or explicitly tracked as planned work.

## Specific Code Questions

Answer these during review:

1. Should `find --mode hybrid` handle missing LanceDB FTS index by falling back to lexical, or should it fail loudly as it does now?
2. Should default retrieval collapse adjacent chunks before returning results?
3. Should default retrieval enforce a per-video result cap?
4. Should `show context` support multiple chunk ids in one call?
5. Should `show context` budget selection prefer more left/right symmetry or sentence boundaries?
6. Should chunk token counts use a real tokenizer instead of the current estimate?
7. Should `yt-dlp` subtitle fallback become the first provider for large runs when transcript API block rates are high?
8. Should Gemini fallback be duration-capped by default?
9. Should exports include `resource_uri` values in frontmatter?
10. Should `ytkb inspect` commands be added before any answer-synthesis command?
11. Should channel registration be introduced before scheduler implementation?
12. What block/error threshold should trigger a provider-level circuit breaker?
13. Should RSS be used as a new-upload hint after channel id resolution?
14. What manifest format should record `yt-dlp`, transcript API, Gemini, ASR, embedding, and chunker versions?
15. Should planned target commands be implemented as aliases over current commands or as separate higher-level workflows?

## Suggested Next Slice

Implement retrieval evaluation and result shaping:

- Add query fixture file for canonical test queries.
- Record expected video ids and timestamp neighborhoods.
- Add tests for lexical, semantic, and hybrid modes where practical.
- Add per-video caps.
- Add adjacent chunk collapse.
- Add `--diversify` and `--dense`.
- Add `ytkb inspect video VIDEO_ID`.
- Add `ytkb inspect chunk CHUNK_ID`.
- Add `ytkb inspect attempts VIDEO_ID`.

This comes before built-in LLM answers because answer quality is not diagnosable until retrieval quality is measurable.

Next after that:

- Add channel registration commands.
- Add scheduler install/run commands.
- Add provider-level circuit breaker settings.
- Add tool/provider version manifests.
- Add fake-provider integration tests for ingest, fallback, vector rebuild, and export.

## Review Output Format

Report findings first, ordered by severity.

For each finding include:

- File and line reference.
- Why it matters.
- How to reproduce or inspect.
- Suggested fix.

Then include:

- Open questions.
- Test gaps.
- Recommended next implementation slice.

## Fresh Agent Prompt

```text
Read docs/plan.md, docs/proxy-strategy.md, and docs/reviewer-handoff.md. Inspect the files listed in the handoff. Run the health, retrieval, context, and index consistency commands. Review correctness, retrieval quality, context-bloat controls, provider fallback behavior, rebuild reliability, and export quality. Report findings first with file/line references. Do not implement changes until the findings and next slice are clear.
```
