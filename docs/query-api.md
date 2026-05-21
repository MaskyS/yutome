# Query API

`ytkb` exposes one retrieval model through three surfaces:

- CLI: `ytkb find`, `ytkb list`, `ytkb show`, `ytkb q`
- MCP: tools named `find`, `list`, `show`, `q`
- HTTP: `POST /find`, `POST /list`, `POST /show`, `POST /q`

The raw primitive is `QueryRequest` in `src/ytkb/query.py`. The transport-neutral convenience verbs live in `src/ytkb/api.py`.

## Verbs

`find` ranks results by relevance. It searches transcript chunks by default and can search video titles or descriptions lexically.

```bash
uv run ytkb find "Crohn probiotics" --mode hybrid --limit 5 --json
uv run ytkb find "cerebrolysin" --in titles --mode lexical --json
```

`list` enumerates corpus objects by filter.

```bash
uv run ytkb list videos --status 'indexed' --limit 20
uv run ytkb list channels --selected
uv run ytkb list attention
uv run ytkb list status
```

`show` fetches resources or resolves citations.

```bash
uv run ytkb show chunk CHUNK_ID
uv run ytkb show video VIDEO_ID
uv run ytkb show transcript TRANSCRIPT_VERSION_ID
uv run ytkb show source CHUNK_ID
uv run ytkb show context CHUNK_ID --token-budget 3000
uv run ytkb show context "https://youtube.com/watch?v=VIDEO_ID&t=123s"
```

`q` accepts a raw `QueryRequest` JSON object.

```bash
uv run ytkb q '{"entity":"video","filter":{"ingest_status":{"eq":"indexed"}},"project":"video_card","limit":5}'
```

## Projections

Supported projection names are:

- `thin`
- `chunk`
- `metadata`
- `video_card`
- `video_attention`
- `channel_card`
- `group_video`
- `status_breakdown`

Chunk `thin` results are citation-first and do not include full transcript text. Use `project=chunk` for full chunk text and `show context` for bounded neighboring transcript text.
