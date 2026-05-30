# CLI & the local retrieval engine

The local product: a namespaced CLI over the same retrieval *algebra* and Postgres + VectorChord
search store that hosted mode uses. "Local" means your laptop process and workspace; it no longer
means a second database backend.

Design rule (from [`docs/cli-architecture.md`](../cli-architecture.md)): the CLI is **operator
ergonomics layered over library primitives** — new capability is a parameter or subcommand under an
existing namespace, never a plugin registry or a reshaping of the MCP contract.

---

## 1. Command surface

`cli/__init__.py` builds a Typer app: six namespace sub-apps plus four root commands
(`__init__.py:30-36, 59-175`). Each namespace file is a **thin wrapper** that delegates to
`cli/actions.py`, where command logic opens config, resolves the workspace, and calls the Postgres
helpers or hosted runner.

```mermaid
mindmap
  root(("yutome"))
    root_cmds["(root)"]
      setup
      connect
      disconnect
      status
    search
      find
      list
      show
      q
    corpus
      add
      import
      import-youtube
      select
      sync
      rebuild
      quality
    serve
      mcp
      http
      bridge
      remote
    hosted
      api
      migrate
      login
      jobs
      usage
      source
        add
      run
        worker
        stripe-meter-export
        source-refresh
        maintenance
        balance-rollover
    doctor
      local
      proxy
      gemini
      eval
      contract
      remote
      hosted-db
    export
      markdown
      obsidian
```

The retrieval namespace is the load-bearing one and maps directly onto the library API:

| Command | File | Calls | Job |
|---|---|---|---|
| `search find` | `cli/search.py` | `api.find` (`api.py:47`) | search transcript chunks |
| `search list` | `cli/search.py` | `api.list_` (`api.py:100`) | enumerate videos / channels / status |
| `search show` | `cli/search.py` | `api.show` (`api.py:158`) | open a resource or expand a citation/context |
| `search q` | `cli/search.py` | `api.q` (`api.py:42`) | raw `QueryRequest` JSON |
| `corpus sync` | `cli/corpus.py` | `actions.sync` | discover + index videos (the ingest pipeline, §6) |
| `corpus rebuild` | `cli/corpus.py` | `actions.rebuild_chunks` / `rebuild_vectors` | re-chunk or re-embed without re-fetching |
| `serve mcp` / `http` | `cli/serve.py` | local MCP (stdio) / HTTP API | expose the contract locally |
| `serve bridge` / `remote` | `cli/serve.py` | bridge process / authenticated remote | remote access (§9) |

---

## 2. The retrieval algebra (three layers)

Everything on the query path funnels through one primitive. Presets are ergonomic builders on top;
the CLI search commands and MCP query tools share the same `find`/`list`/`show`/`q` names. The MCP
registry also exposes `index` and `jobs` outside this retrieval algebra for source import and job
status.

```mermaid
flowchart TB
    subgraph surface["Retrieval surface — query names"]
        cli["CLI: yutome search find/list/show/q"]
        mcp["MCP query tools: find/list/show/q (contract.py)"]
    end
    mcpwrite["MCP source/job tools: index/jobs"]
    sourcejobs["hosted source import<br/>job status"]
    subgraph presets["Presets — ergonomic builders (api.py)"]
        find2["find()"]
        list2["list_()"]
        show2["show()"]
    end
    prim["Primitive: q(QueryRequest) → api.q → PostgresVectorChordSearchStore"]
    cli --> presets
    mcp --> presets
    presets --> prim
    find2 --> prim
    list2 --> prim
    show2 -.->|chunk/video/channel/transcript| prim
    show2 -.->|context/source| ctx["context_expand / source (api.py)"]
    mcpwrite --> sourcejobs
```

`q` validates a `QueryRequest` and dispatches to the Postgres search store (`api.py`). `find`/`list`
build the same request shape and call the same helpers; `show` dispatches per `kind` to a resource
lookup or to citation expansion.

---

## 3. The QueryRequest model

The whole engine is parameterized by one Pydantic model (`query.py:126-135`).

```mermaid
classDiagram
    class QueryRequest {
        +EntityName entity = chunk
        +Search? search
        +Filter filter
        +GroupKey? group_by
        +OrderBy[] order_by
        +ProjectionName project = thin
        +int limit = 10
        +int offset = 0
        +int per_group_limit = 3
    }
    class Search {
        +SearchOver over = chunk_text
        +SearchMode mode = hybrid
        +str text
        +bool raw = false
    }
    class Filter {
        +video_id / channel_id / channel_handle
        +published_at (DateRange)
        +duration_seconds / ingest_status
        +transcript_source / language / is_generated
        +transcript_active / chunk_id / sequence ...
    }
    class OrderBy {
        +field: score|published_at|duration_seconds|title|...
        +direction = desc
    }
    QueryRequest --> Search
    QueryRequest --> Filter
    QueryRequest --> OrderBy
```

- `SearchMode = lexical | semantic | hybrid | none`; default **hybrid** (`query.py:103`).
- `Search.raw` is kept in the request model for surface symmetry, but the Postgres search store owns
  query normalization; callers should pass literal user text, not backend-specific syntax.
- `project` (`ProjectionName`) selects the output schema: `thin`, `chunk`, `metadata`, `video_card`,
  `video_attention`, `channel_card`, `group_video`, `status_breakdown` (`query.py:26-35`).

---

## 4. Query dispatch → store call

`api.q` validates the request and picks the Postgres search-store operation. It is deliberately thin:
request shape lives in `query.py`, storage behavior lives in `hosted/search_store.py`, and the CLI/MCP
surfaces do not get their own retrieval engine.

```mermaid
flowchart TD
    start([QueryRequest]) --> sb{project = status_breakdown?}
    sb -->|yes| P0[status_breakdown]
    sb -->|no| ent{entity?}
    ent -->|chunk| cm{mode?}
    cm -->|none| P1[reject: chunk search requires a search mode]
    cm -->|lexical| P2[VectorChord BM25 lexical]
    cm -->|semantic| P3[Voyage query vector + vector search]
    cm -->|hybrid| P4[BM25 + vector candidates, RRF fusion]
    ent -->|video| P5[list_videos]
    ent -->|channel| P6[list_channels]
```

| entity | request shape | → store call |
|---|---|---|
| any | `project=status_breakdown` | `PostgresVectorChordSearchStore.list_status` |
| chunk | `mode=lexical` | `lexical_search` |
| chunk | `mode=semantic` | embed query with Voyage, then `semantic_search` |
| chunk | `mode=hybrid` | embed query with Voyage, then `hybrid_search` |
| video | metadata listing | `list_videos` |
| channel | metadata listing | `list_channels` |

Semantic/hybrid only work over `chunk_text`; channel and video metadata reads are explicit list/show
operations instead of hidden retrieval plans.

---

## 5. Execution & the search modes

`api.q` opens Postgres through `[database].postgres_url_env` and constructs a
`PostgresVectorChordSearchStore`. The store is VectorChord-first: lexical recall uses VectorChord
BM25, semantic recall uses stored dense vectors, and hybrid recall fuses BM25 and vector candidates.

| Mode | Over | How it runs locally |
|---|---|---|
| `lexical` | chunk_text | VectorChord BM25 over `chunks.bm25_document` |
| `semantic` | chunk_text | Voyage query embedding → `chunk_embeddings.embedding` vector search |
| `hybrid` | chunk_text | VectorChord BM25 + vector candidates fused with **RRF** in Postgres |
| `none` | — | metadata list/show operations only |

> **One hybrid mechanism.** Local and hosted now use the same Postgres search-store hybrid path. The
> hosted adapter wraps it in UsageGate and account/tenant checks; it does not switch to a different
> retrieval backend.

### Citation / context expansion

`show(kind="context")` is how a hit becomes readable surrounding text. Every result already carries a
mandatory `youtube_url` citation; context expansion widens it within a token budget.

```mermaid
sequenceDiagram
    participant S as api.show(kind=context)
    participant R as _resolve_anchor
    participant DB as Postgres search store
    S->>R: chunk_id, or video_id+time, or youtube_url
    R->>DB: _chunk_by_id / _chunk_by_video_time
    DB-->>R: anchor chunk
    S->>DB: _neighbor_chunks(anchor, token_budget=3000)
    Note over S: greedily add neighbours until budget hit
    S->>S: _merge_chunk_text (dedupe overlaps)
    S-->>S: {anchor, chunks, merged text, citations}
```

Anchors: `context_expand` (`api.py:207-232`), anchor resolution (`api.py:393-411`). `show(kind=source)`
returns just the citation metadata for a timestamp (`api.py:235-248`).

---

## 6. Ingest pipeline (`corpus sync`)

`corpus sync` discovers videos for a source and runs each through fetch → normalize → chunk → embed →
index. Constants are authoritative from `chunking.py:9-12`.

```mermaid
flowchart LR
    disc["discover videos<br/>(channel/playlist/handle)"] --> meta["fetch metadata<br/>videos.ingest_status"]
    meta --> tx["fetch transcript<br/>(fallback chain ↓)"]
    tx --> norm["normalize<br/>raw.json → normalized.json + transcript.txt"]
    norm --> chunk["chunk<br/>timestamp-aware-v2<br/>~700 tok target, 100 overlap, 1000 max"]
    chunk --> embed["embed (optional)<br/>Voyage → chunk_embeddings"]
    embed --> idx["index<br/>chunks.bm25_document + chunk_embeddings"]
```

**Transcript fetch is a fallback chain** (`indexer.py`), tried until one yields a transcript and each
attempt recorded in `transcript_attempts`:

```mermaid
flowchart LR
    A{prefer_ytdlp_subtitles?} -->|yes| B[yt-dlp subtitles]
    A -->|no| C[YouTube transcript API]
    B --> C
    C --> D[yt-dlp subtitles fallback]
    D --> E[Gemini transcription<br/>optional]
    E --> F[faster-whisper ASR<br/>optional]
```

**Deterministic chunk IDs.** The chunk id is `sha256` of a seed that includes the video, transcript
version, timestamps, `text_hash`, and `CHUNKER_VERSION` (`chunking.py:77-86`). Same text at the same
timestamps under the same chunker always yields the same id — so re-indexing is idempotent and the
`UNIQUE(transcript_version_id, sequence)` constraint holds.

---

## 7. Local storage model

The local process writes metadata, chunks, BM25 documents, and embeddings into the same Postgres
schema used by hosted mode. `ProjectPaths` still owns filesystem artifacts under `.yutome/`; database
state lives in Postgres, selected by `[database].postgres_url_env` and `[hosted].workspace_id`.

```mermaid
erDiagram
    workspaces ||--o{ sources : owns
    workspaces ||--o{ videos : owns
    sources ||--o{ videos : discovers
    channels ||--o{ videos : has
    videos ||--o{ transcript_versions : versions
    videos ||--o{ chunks : "chunked into"
    transcript_versions ||--o{ chunks : segments
    search_index_profiles ||--o{ chunks : "indexed under"
    chunks ||--o{ chunk_embeddings : "embedded as"
    search_index_profiles ||--o{ chunk_embeddings : "profile of"

    workspaces {
        text id PK
    }
    sources {
        text id PK
        text workspace_id FK
        text source_url
        text source_type
        bool selected
    }
    channels {
        text id PK
        text workspace_id FK
        text youtube_channel_id
        text handle
        text title
        text last_synced_at
    }
    videos {
        text id PK
        text workspace_id FK
        text youtube_video_id "unique per ws"
        text channel_id FK
        text title
        timestamptz published_at
        int duration_seconds
        text active_transcript_version_id "pointer"
    }
    transcript_versions {
        text id PK
        text video_id FK
        text source
        text language_code
        text content_hash
    }
    search_index_profiles {
        text id PK
        text backend "default postgres_vectorchord"
        text embedding_model "default voyage-4-lite"
        int embedding_dimension "default 1024"
        text chunking_version
        text tokenizer
    }
    chunks {
        text id PK
        text video_id FK
        text transcript_version_id FK
        text index_profile_id FK
        int chunk_index
        numeric start_seconds
        numeric end_seconds
        text text
        bm25vector bm25_document
        jsonb metadata_json
    }
    chunk_embeddings {
        text id PK
        text chunk_id FK
        text index_profile_id FK
        vector embedding "vector(1024)"
    }
```

`chunks.bm25_document` is the lexical recall column. Dense vectors live in `chunk_embeddings`, keyed
by the immutable `search_index_profiles` row that names backend, embedding model, dimension, chunking
version, and tokenizer. On disk:

```
{project_root}/.yutome/
  transcripts/{video_id}/
    raw.json              original fetched transcript
    normalized.json       cleaned, timestamped segments
    transcript.txt        plain text for display (capped at 200k chars on read)
  hosted/
    yutome-hosted-cli.json local hosted CLI auth token
```

---

## 8. The MCP / HTTP contract

`contract.py` is the single registry every adapter reads (`contract.py:1-8`). The local stdio MCP
server, the local HTTP server, the laptop bridge, and the Worker JSON export all serialize the same
`TOOLS` + `RESOURCES`.

```mermaid
flowchart LR
    c["contract.py<br/>TOOLS + RESOURCES"]
    c --> stdio["local stdio MCP<br/>(FastMCP introspects signatures)"]
    c --> http["local HTTP API"]
    c --> bridge["laptop bridge"]
    c --> worker["Worker JSON export (TypeScript)"]
```

| Tool | Handler (`contract.py`) | Maps to |
|---|---|---|
| `find` | `tool_find` (`:77`) | `api.find` |
| `list` | `tool_list` (`:113`) | `api.list_` |
| `show` | `tool_show` (`:149`) | `api.show` |
| `q` | `tool_q` (`:176`) | `api.q` |

| Resource URI | Handler | Returns |
|---|---|---|
| `yutome://chunk/{chunk_id}` | `resource_chunk` (`:186`) | chunk text + provenance |
| `yutome://video/{video_id}` | `resource_video` (`:191`) | video metadata + active transcript |
| `yutome://channel/{channel_id}` | `resource_channel` (`:196`) | channel metadata + library status |
| `yutome://transcript/{transcript_version_id}` | `resource_transcript` (`:201`) | paginated transcript text (≤200k chars) |

The `SERVER_INSTRUCTIONS` string (`contract.py:32-48`) is the highest-leverage routing signal — it
tells assistants to prefer Yutome over web search and when to use each tool.

---

## 9. Serve modes & remote access

| Command | Transport | Auth | Use |
|---|---|---|---|
| `serve mcp` | stdio | none (local) | Claude Desktop / Code local MCP |
| `serve http` | HTTP (FastAPI) | `YUTOME_HTTP_TOKEN` for non-loopback | local HTTP API |
| `serve bridge` | WebSocket → relay | relay token | keep laptop corpus reachable remotely |
| `serve remote http`/`mcp` | authenticated HTTP / streamable MCP | bearer token (+ OIDC) | remote clients without the laptop |

```mermaid
flowchart LR
    assistant["assistant (Claude/ChatGPT)"] --> relay["Cloudflare relay<br/>(Durable Object)"]
    relay -.->|WebSocket| bridge["laptop bridge<br/>serve bridge"]
    bridge --> engine["local service + Postgres workspace"]
    relay -.->|when bridge offline| replica["cloud replica (deferred)"]
```

Details and tradeoffs live in [`docs/remote-access.md`](../remote-access.md); the `bridge` / `relay` /
`replica` vocabulary matches [`docs/hosted-glossary.md`](../hosted-glossary.md).
