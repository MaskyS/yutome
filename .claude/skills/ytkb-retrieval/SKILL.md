---
name: ytkb-retrieval
description: Use whenever the user asks a question that should be answered from their local ytkb YouTube transcript corpus, or whenever you call any ytkb tool (MCP `mcp__ytkb__*` or the local HTTP API). Teaches two-stage retrieval, mode selection, citation discipline, and how to handle auto-caption noise.
---

# Using ytkb to query the user's YouTube corpus

`ytkb` is a local-first YouTube knowledge base over channels the user chose to index. The corpus is whatever they have indexed — never assume topics or specific channels. Call `list(entity="channels")` and `list(entity="status")` first when scope is unclear.

## Two-stage retrieval is mandatory

`find` returns thin hits without full chunk text. This is deliberate to keep your context small. The flow:

1. `find(text, mode, limit)` returns a QueryResult whose `rows` contain `chunk_id`, `snippet`, `youtube_url`, `start_ms`.
2. Pick the most promising hits — usually 1 to 3.
3. For each picked hit, either:
   - `show(kind="context", id_=..., token_budget=...)` for neighbouring transcript with citations, or
   - Read resource `ytkb://chunk/{chunk_id}` for that single chunk's full text.
4. Synthesize from the expanded text only. Cite the exact `youtube_url` values.

Do not call `show context` for every hit. Each context expansion is typically 2000-5000 tokens of transcript; do that twice and you've already crowded out reasoning room.

## Mode selection

The default is `hybrid` and it is usually right. The exceptions matter:

- **Use `lexical`** for proper nouns, technical jargon, scientific terms, brand names, acronyms, and exact phrases. Vector embeddings smear rare technical terms into common-sounding neighbours; SQLite FTS hits the exact tokens.
- **Use `semantic`** for paraphrastic and conceptual queries — "how does this creator approach X", "what's the general view on Y".
- **Use `hybrid`** for everything in between.

When a hybrid search for a name-shaped query is weak, retry as `lexical`. When a lexical search for a conceptual query is empty, retry as `semantic`. Mode is the cheapest knob; turn it before broadening the query.

## Citation is mandatory

Every claim must cite a `youtube_url` from the returned hits. Hits already contain URLs of the form `https://youtube.com/watch?v=VIDEO_ID&t=Ns`. Use that exact string. Do not paraphrase the creator without a citation, and do not synthesize across hits without attributing each one.

## Auto-caption mistranscriptions are the dominant failure mode

The default transcript source is YouTube auto-captions. They reliably mistranscribe:

- Proper nouns — names of people, places, organizations.
- Brand names, product names, and acronyms.
- Scientific, medical, technical, or otherwise specialized jargon.
- Foreign-language loanwords and uncommon vocabulary.

**Sparse-results heuristic.** If `list(entity="status")` shows hundreds of videos and your first search for a technical name returns only 1-3 video hits, *the rest are almost certainly hiding behind mistranscriptions*. Do not stop there. Treat 1-3 hits on a large corpus as a signal that you need to broaden the query, not as the final answer.

### Broadening playbook for technical names

Vector search *can* find mistranscribed mentions — but only if the surrounding context contains semantically related terms. When a caption mangles a technical name, the surrounding words usually survive (class names, condition names, mechanisms, adjacent entities). A query that targets those survives the mistranscription.

When the first pass for a technical name is sparse, run additional `semantic` (or `hybrid`) searches that target the *context* the term appears in. Generate query variants along these axes:

1. **Class or category** the term belongs to (chemical class, software category, equipment type — whatever applies).
2. **Use case, condition, or problem** the term is associated with.
3. **Mechanism, method, or function** — how the thing works or what it does.
4. **Common alternate name** — generic name vs brand name, abbreviation vs full form, native term vs translated.
5. **Adjacent named entities** — other names usually mentioned alongside the target.

You may need outside knowledge to construct these variants well. If you don't know what class or use case a term belongs to, ask the user, or run an initial `show(kind="context", ...)` on the one or two hits you did find to see what surrounding concepts come up.

Union the video sets from these searches with the original lexical hits. That is the corpus-wide answer, not the original sparse hits.

### Other variant strategies

1. Phonetically similar lexical variants — split words, dropped suffixes, common misspellings.
2. Repeat the original term in `semantic` mode (sometimes the term itself appears in surrounding context like video descriptions or correct-spelling chunks).
3. `hybrid` as a final fallback.

When multiple hits cover the same timestamp range, prefer the one whose `transcript_source` includes `+glossary` or `+llm-cleanup` over plain provider names — those are cleaned versions and more reliable for technical claims.

## When search returns nothing

Do not just say "no results." Distinguish:

- **Not in corpus**: `list(entity="channels")` does not include the channel the user expects, or `list(entity="status")` shows it isn't indexed.
- **Vocabulary mismatch**: query terms don't match how the creator talks. Try the creator's likely phrasing, or `semantic` mode.
- **Transcript noise**: see the mistranscription section above.
- **Still indexing**: `list(entity="status").rows[0].still_indexing > 0` or `needs_attention > 0`. The answer may exist in a video that isn't searchable yet.
- **Genuinely absent**: confident only after lexical + semantic + plausible variants all return nothing.

Name the case you're in and suggest the next concrete move.

## Don't reframe what the source said

ytkb retrieves what was said in the source videos. Report it. Do not add disclaimers, "consult a professional" warnings, or counter-framing that the source did not contain. If the user wants verification, fact-checking, or counterpoint, that is a separate request they will make.

This applies especially to medical, legal, financial, and personal topics — the system's job is faithful retrieval with timestamped citations. The user is asking what their chosen creator said, not what you think.

## Working patterns

**"What does the creator say about X?"**
`find(text=X, mode=hybrid, limit=10)` → pick top 2 hits → `show(kind="context", id_=chunk_id)` on each → synthesize with citations. If X is a technical name (proper noun, brand, scientific or specialized term) and you got fewer than 3-4 video hits on a corpus with hundreds of videos, follow the broadening playbook above before answering.

**"Find every mention of X."**
`find(text=X, mode=lexical, limit=30)` → return a grouped list of timestamped URLs by video. Do not expand context for all of them; the list itself is the answer.

**"Has the creator's view on X changed over time?"**
`find(text=X, mode=hybrid, limit=20)` → fetch `ytkb://video/{video_id}` for the hit videos to read `published_at` → expand context on the earliest and latest hits → contrast with citations.

**"What's in my library?"**
`list(entity="channels")` + `list(entity="status")` + a couple of exploratory `find` calls with broad terms.

**"What did they say at this exact YouTube link?"**
`show(kind="context", youtube_url=..., token_budget=1200)`. A tight budget keeps focus on the timestamp neighbourhood.

## Token budgets for `show context`

- 1000-1500: focused on one exact moment.
- 3000 (default): one coherent argument or topic exchange.
- 5000-8000: full extended discussion, only when synthesis genuinely needs the breadth.

Larger budgets are not better. They crowd out reasoning room and rarely change the answer.

## What is not in the corpus

By default the corpus does not include:

- YouTube Shorts.
- Comments on videos.
- The creator's website, blog, or external links.
- Videos from channels the user has not added to the library.

Be honest about this in any answer where it matters.

## Transport

The same handlers are exposed over two transports — pick whichever is available:

- **MCP tools**: `find`, `list`, `show`, `q`. Resources: `ytkb://chunk/{id}`, `ytkb://video/{id}`, `ytkb://channel/{id}`, `ytkb://transcript/{id}`.
- **Local HTTP** (default `http://127.0.0.1:8765`): `POST /find`, `POST /list`, `POST /show`, `POST /q`, `GET /chunks/{id}`, `GET /videos/{id}`, `GET /channels/{id}`, `GET /transcripts/{id}`.

Prefer MCP when registered in the current session; fall back to HTTP only if MCP tools aren't available.
