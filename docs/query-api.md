# Query API

`yutome` exposes one retrieval model through three surfaces:

- CLI: `yutome find`, `yutome list`, `yutome show`, `yutome q`
- MCP: tools named `find`, `list`, `show`, `q`
- HTTP: `POST /find`, `POST /list`, `POST /show`, `POST /q`

The raw primitive is `QueryRequest` in `src/yutome/query.py`. The transport-neutral convenience verbs live in `src/yutome/api.py`.

For multi-device use, run the authenticated HTTP API with `yutome remote prepare` and `yutome remote serve`. See `docs/remote-access.md`.

## HTTP Examples

Start a local authenticated server:

```bash
uv run yutome remote prepare --show-token
uv run yutome remote serve --host 127.0.0.1 --port 8765
```

Check readiness:

```bash
curl -H "Authorization: Bearer $YUTOME_HTTP_TOKEN" \
  http://127.0.0.1:8765/readyz
```

Run a search:

```bash
curl -s http://127.0.0.1:8765/find \
  -H "Authorization: Bearer $YUTOME_HTTP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"Crohn probiotics","mode":"hybrid","limit":5}'
```

Expand one hit:

```bash
curl -s http://127.0.0.1:8765/show \
  -H "Authorization: Bearer $YUTOME_HTTP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"kind":"context","id":"CHUNK_ID","token_budget":3000}'
```

Fetch the full active transcript path by first reading the video metadata, then the returned `transcript_version_id`:

```bash
curl -s http://127.0.0.1:8765/videos/VIDEO_ID \
  -H "Authorization: Bearer $YUTOME_HTTP_TOKEN"

curl -s http://127.0.0.1:8765/transcripts/TRANSCRIPT_VERSION_ID \
  -H "Authorization: Bearer $YUTOME_HTTP_TOKEN"
```

FastAPI also exposes interactive OpenAPI docs at `/docs` when you are serving the HTTP API.

## Verbs

`find` ranks results by relevance. It searches transcript chunks by default and can search video titles or descriptions lexically.

```bash
uv run yutome find "Crohn probiotics" --mode hybrid --limit 5 --json
uv run yutome find "cerebrolysin" --in titles --mode lexical --json
```

`list` enumerates corpus objects by filter.

```bash
uv run yutome list videos --status 'indexed' --limit 20
uv run yutome list channels --selected
uv run yutome list attention
uv run yutome list status
```

`show` fetches resources or resolves citations.

```bash
uv run yutome show chunk CHUNK_ID
uv run yutome show video VIDEO_ID
uv run yutome show transcript TRANSCRIPT_VERSION_ID
uv run yutome show transcript VIDEO_ID --offset 0 --limit 300
uv run yutome show source CHUNK_ID
uv run yutome show context CHUNK_ID --token-budget 3000
uv run yutome show context "https://youtube.com/watch?v=VIDEO_ID&t=123s"
```

`q` accepts a raw `QueryRequest` JSON object.

```bash
uv run yutome q '{"entity":"video","filter":{"ingest_status":{"eq":"indexed"}},"project":"video_card","limit":5}'
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
