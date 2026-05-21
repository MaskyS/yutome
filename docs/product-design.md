# Product Design Notes

`ytkb` should be understood as a local-first YouTube antilibrary, not only as a channel scraper or transcript index.

The user has a set of channels they care about, and that corpus contains far more latent value than they can watch, remember, or manually organize. The product promise is that those unwatched or half-remembered videos become searchable, citable, exportable, and available to the user's normal agent or chat workflow without requiring a centralized server.

## User Model

The broad target is not a single persona. The same system needs to work for a nontechnical user who wants to search favorite channels, a PKM user who wants durable Markdown/Obsidian exports, a researcher who cares about timestamped source passages, and an expert or agent that wants raw retrieval/context APIs.

The interface should therefore behave like a musical instrument: a beginner should be able to get a sound quickly, while an expert should be able to play with precision. The beginner-facing vocabulary should be channels, questions, search results, timestamps, exports, and indexing status. The expert-facing vocabulary can include chunks, transcript versions, provider attempts, retrieval modes, FTS, vector indexes, rebuilds, and SQL.

## First-Class Corpus Object

The first-class user object is a channel library.

The default flow should be:

```bash
ytkb init
ytkb channels add https://www.youtube.com/@SomeChannel
ytkb sync
ytkb find "topic I remember"
```

The broader import flow should support:

- OAuth import of the user's current YouTube subscriptions.
- Google Takeout `subscriptions.csv` import as a fallback when OAuth is undesirable or unavailable.
- OPML import for users already managing YouTube via RSS tools.
- Plain URL or handle lists for the lowest-friction manual path.
- Direct paste of channel URLs, handles, or channel IDs for channels outside subscriptions.

Playlists and single videos are useful later, but they should not complicate the main library model. They can become additional source types under the same local corpus idea.

## OAuth Position

OAuth is the preferred subscription-import experience because Takeout is too heavy for the common case. However, a local-first project should not require a centralized server just to import private subscription data.

The local OAuth design is:

1. User provides a Google OAuth desktop/local client secrets file.
2. `ytkb` opens a system-browser consent page with the read-only YouTube scope.
3. The callback returns to a localhost redirect.
4. The refresh/access token is stored locally under `data/auth/`.
5. `ytkb` calls YouTube Data API subscription listing and stores channels in the local library.

This is less polished than a hosted broker, but it keeps the trust boundary local. A hosted broker can be considered later if onboarding friction is too high.

## Daily-Driver LLM Integration

The important product surface is not a standalone chat UI. A standalone `ytkb ask` command can exist, but the main product promise is that a user's regular agent or chat app can query their local YouTube corpus.

The local connector should expose:

- Search/retrieve thin hits.
- Expand bounded transcript context.
- Open timestamp URLs.
- List channels and corpus health.
- Explain unresolved indexing problems.

This makes `ytkb` closer to a local Scry-like capability for a personal media diet than a destination app. A web UI can still be useful as an inspector, but it should not be required for the core experience.

## Search Modes

The product should support several result postures rather than treating search as one ranked list forever.

Direct lookup should return the strongest timestamped passages. Exploratory search should group by video and collapse adjacent chunks so one long video does not crowd out the corpus. Antilibrary browsing should eventually show where a topic appears across channels and clusters, even when the user is not asking for a synthesized answer.

When there are no strong results, the product should not simply say "no results." It should distinguish among likely causes: not indexed yet, exact phrase absent, vocabulary mismatch, weak semantic neighbors, low transcript quality, or genuinely absent from the corpus. Then it should suggest useful next actions such as trying related terms, broadening to all channels, inspecting transcript quality, indexing more channels, or opening weak matches.

## Transcript Quality Upgrade

Caption quality is part of retrieval quality. Many YouTube captions are good enough for rough search but bad enough to miss important biomedical or technical terms. For example, "cerebrolysin" can appear as "cerebral lysine", "cerebro lysin", or similar caption noise.

The primary optional quality path is LLM transcript cleanup:

```bash
ytkb quality upgrade --limit 10
ytkb quality upgrade --video-id VIDEO_ID --rebuild-vectors
ytkb quality upgrade --limit 50 --video-workers 2 --concurrency 3
```

This path uses the existing timestamped caption segments plus bounded metadata context as input. The metadata context should include channel title/handle, video title, and truncated video/channel descriptions, because those fields often contain the spelling of names, supplements, drugs, chapters, sponsors, and technical terms that captions miss. The model is asked to return a sparse correction patch, not a rewritten transcript: only changed segment sequences and their corrected text. That keeps unchanged segments byte-for-byte stable, reduces output tokens, and makes review/diff UX natural. Patches are verified before being applied: unexpected sequence numbers, duplicate edits, empty edits, no-op edits, and oversized changes are rejected, and invalid patches are retried with the validation error before the video is marked failed. The upgrade writes a new active transcript version with provenance such as `+llm-cleanup:{model}` and preserves the original transcript version, because LLM cleanup can still be wrong. For cost control and smooth rollout, it supports limits, source filters, per-video upgrades, video-level workers, per-video request concurrency, request timeouts, and later background scheduling.

The default model is `gemini-3.1-flash-lite`, because it is available through the current Gemini API and is the right cost/latency posture for a background cleanup pass across a personal subscription library. Cleanup should use a separate low output cap such as `cleanup_max_output_tokens = 4096`; full-video transcription fallback can keep the much larger `max_output_tokens` cap. Users can override `gemini.model` when a corpus needs a different cost/quality point, for example `gemini-3.5-flash` for a stronger pass.

Later quality layers can be added in increasing cost order:

- Query-time alias expansion for known troublesome terms.
- Whole-transcript LLM cleanup in timestamped batches with parallel request execution.
- Targeted LLM cleanup only for selected technical spans or high-value videos.
- Targeted ASR fallback for videos or windows where captions are unusable.
- Full transcript regeneration only when the user explicitly chooses it.

The design principle is that transcript improvement should be incremental and inspectable. Users should not have to pay for or wait on expensive cleanup before experiencing search.

## Open Questions

- Should OAuth onboarding remain bring-your-own Google client credentials, or should a future hosted broker exist for less technical users?
- Should `ytkb sync` with no target always mean "sync selected channels," or should it require `--all` for safety once schedules exist?
- Should LLM cleanup run automatically after ingest for selected channels, or should it remain an explicit/background upgrade?
- ~~Should the first local agent connector be MCP, an OpenAI/ChatGPT app connector shape, a plain HTTP API, or all of these over the same service?~~ Decided: local MCP first, thin local HTTP underneath sharing the same core functions, remote MCP/ChatGPT connector deferred. See the Local Agent Connector section of `plan.md`.
- How much should the beginner surface expose unresolved videos and transcript quality warnings before it becomes anxiety-inducing instead of helpful?
