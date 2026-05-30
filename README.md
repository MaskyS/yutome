<p align="center">
  <img src="https://raw.githubusercontent.com/MaskyS/yutome/main/docs/assets/yutome-wordmark.png" alt="Yutome" width="420">
</p>

Yutome ingests transcripts from the YouTube channels you point at, stores catalog/search state in Postgres with VectorChord Suite, and connects to whichever AI app you already use — Claude, ChatGPT, Cursor, anything MCP-compatible.

The library is searchable from the command line, from any local MCP client, or remotely from claude.ai / ChatGPT through a small Cloudflare Worker you deploy yourself.

Yutome is a command-line tool. You set it up from the terminal; from there it handles sync, cleanup, and exports. A coding agent (Claude Code, Cursor) can drive any of it.

## Install

```bash
uv tool install yutome
```

This puts a single `yutome` command on your PATH. For now the package installs the full feature set by default: yt-dlp, Postgres client support, Voyage embeddings, MCP server, HTTP API, and the bundled Cloudflare Worker used by remote connectors.

**Don't have uv or Python yet?** uv installs itself in one command and will fetch Python for you if it's missing.

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then run the `uv tool install` line above. If your shell can't find `uv` right after install, open a new terminal — the installer adds it to PATH but existing shells need to reload. If your `python3` is older than 3.11 (or missing entirely), add `--python 3.11` to the install command and uv will fetch a 3.11 for you.

**Why `uv tool install` and not `pip install`?** `uv tool install` (and its older cousin `pipx install`) puts yutome in its own isolated environment and symlinks the `yutome` command into your PATH, so it works from anywhere like `git` or `node`. Plain `pip install` would tie yutome to whichever Python venv you happened to be in at install time, and the `yutome` command would only work while that venv is active.

**Alternatives:**

```bash
# pipx — needs python 3.11+ already on your PATH
pipx install yutome

# Install the latest unreleased commit
uv tool install 'yutome @ git+https://github.com/MaskyS/yutome.git'

# For hacking on the code
git clone https://github.com/MaskyS/yutome.git
cd yutome
uv sync
uv run yutome --help
```

## Quickstart

```bash
# 1. Start a VectorChord-Suite Postgres (the four extensions can't be installed on managed PG)
docker run -d --name yutome-pg \
  -e POSTGRES_USER=yutome -e POSTGRES_PASSWORD=yutome -e POSTGRES_DB=yutome \
  -p 5432:5432 tensorchord/vchord-suite:pg17-latest
export YUTOME_POSTGRES_URL=postgresql://yutome:yutome@localhost:5432/yutome

# 2. Guided first-run: writes ./yutome.toml + ./data, generates a local workspace,
#    and runs the CREATE EXTENSION migrations against the database above
yutome setup

yutome corpus add https://www.youtube.com/@LexClips  # add a YouTube channel or video as a source
yutome corpus sync                                   # discover videos, fetch transcripts, build indexes
yutome search find "first principles"                # ranked search across everything indexed
```

`yutome setup` is interactive by default — the wizard offers to save your Postgres DSN and an optional `VOYAGE_API_KEY` into `./.env`; pass `-y` to run non-interactively. It needs `YUTOME_POSTGRES_URL` to point at a Postgres database with the **VectorChord Suite** extensions (`vchord`, `vchord_bm25`, `pg_tokenizer`, `vector`). Those are native extensions that must be installed at the server level, so managed Postgres (Supabase, RDS, Neon) can't run them — use the Docker image above (it preconfigures `shared_preload_libraries`) or your own Postgres, and `yutome setup` runs the `CREATE EXTENSION` migrations for you. The only commonly-needed provider key is `VOYAGE_API_KEY` (semantic/hybrid search) — get one at [voyageai.com](https://www.voyageai.com/). Every other key in `.env.example` is optional and tied to a specific feature (Gemini transcript cleanup, Webshare residential proxy, OAuth subscription import, etc.).

Postgres is the database for catalog rows, jobs, lexical indexes, vectors, and usage records. The `./data/` directory next to `yutome.toml` holds transcript artifacts, exports, remote connector state, and logs.

Run `yutome --help` for the full surface. The most-used commands:

| Goal | Command |
|---|---|
| First-run setup | `yutome setup` |
| Add a channel/video | `yutome corpus add <url>` |
| Import a subscription list | `yutome corpus import-youtube` or `yutome corpus import <file>` |
| Index everything new | `yutome corpus sync` |
| Search | `yutome search find "<query>"` |
| List or inspect indexed objects | `yutome search list videos`, `yutome search show video <id>`, … |
| Local MCP server | `yutome serve mcp` (usually invoked via Claude config, not by hand) |
| Deploy/manage remote connector | `yutome connect --deploy`, `yutome serve bridge start`, `yutome status` |

Commands like `search`, `corpus`, `serve`, `hosted`, `doctor`, and `export` are groups — append `--help` (e.g. `yutome search --help`) to see their subcommands.

## Connect to an AI assistant

Yutome speaks [MCP](https://modelcontextprotocol.io/), so any MCP-aware assistant can call its `find`, `list`, `show`, and `q` tools. The fastest way is `yutome setup` — it walks you through both paths below.

### Local (recommended for daily use)

For assistants running on the same Mac as yutome — **Claude Desktop, Cursor, Claude Code, Cherry Studio, LibreChat, Goose**, etc. Same config snippet, different config file per app:

```json
{
  "mcpServers": {
    "yutome": {
      "command": "yutome",
      "args": ["--config", "/absolute/path/to/yutome.toml", "serve", "mcp"]
    }
  }
}
```

Where to paste it:

- **Claude Desktop** — [`~/Library/Application Support/Claude/claude_desktop_config.json`](https://modelcontextprotocol.io/quickstart/user). Inside Claude Desktop: *Settings → Developer → Edit Config*. Restart Claude Desktop after saving.
- **Cursor** — `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` in your project.
- **Claude Code** — one-liner, no JSON editing: `claude mcp add yutome -- yutome --config /absolute/path/to/yutome.toml serve mcp`
- **Cherry Studio / LibreChat / Goose / others** — each app has its own MCP server settings; paste the same snippet there.

`yutome setup` does this interactively: shows the snippet, copies it to your clipboard, and opens the Claude Desktop config folder for you. If yutome's installed via `uv tool install`, the `yutome` binary on your PATH is what gets invoked — no `uv run` wrapper needed.

### Remote bridge (unstable advanced path)

For hosted Yutome, use the production MCP endpoint shown in the web dashboard. The laptop-backed remote bridge below is still kept for self-hosting and local-first experiments, but its command shape and setup flow are not stable yet.

```bash
yutome connect --deploy        # one-time: deploy the Worker, generate secrets, auto-start the bridge
yutome serve bridge install    # optional: keep the bridge running across reboots (launchd / systemd)
```

`connect --deploy` deploys a Cloudflare Worker to your own account (free plan is enough), generates an OAuth-protected `/mcp` endpoint, prints a pairing code, and auto-spawns the laptop bridge in the background. Paste the `/mcp` URL into a Claude.ai or ChatGPT custom connector and complete OAuth in the browser tab using the pairing code.

The Worker is just a relay — your corpus stays on your laptop. The bridge is a WebSocket process that lets the Worker reach it; `yutome serve bridge status/start/stop` control it manually, and `yutome serve bridge install` registers it as a launchd (macOS) or systemd-user (Linux) service so it survives reboots. If the bridge isn't running, the connector reports "Yutome Desktop offline" and the assistant chat keeps working otherwise. Full setup walkthrough: [`docs/remote-access.md`](https://github.com/MaskyS/yutome/blob/main/docs/remote-access.md) and [`cloudflare/yutome-capsule/README.md`](https://github.com/MaskyS/yutome/blob/main/cloudflare/yutome-capsule/README.md).

### From a script or agent (HTTP API)

The MCP connector is one front door; the bearer-token HTTP API is the other. Yutome exposes **one retrieval model through three surfaces** — CLI, MCP, and HTTP — so a script, cron job, or agent can hit the same library directly:

```bash
yutome serve remote prepare --show-token              # writes/prints the YUTOME_HTTP_TOKEN
yutome serve remote http --host 127.0.0.1 --port 8765 # serve the authenticated HTTP API

curl -s http://127.0.0.1:8765/find \
  -H "Authorization: Bearer $YUTOME_HTTP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text":"first principles","mode":"hybrid","limit":5}'
```

The HTTP API speaks the same `find` / `list` / `show` / `q` verbs as MCP. It's available today when you self-host or run the CLI server; for multi-device or behind-a-proxy deployments see [`docs/remote-access.md`](https://github.com/MaskyS/yutome/blob/main/docs/remote-access.md). The full endpoint reference and query language live in [`docs/query-api.md`](https://github.com/MaskyS/yutome/blob/main/docs/query-api.md).

## Docs

See [`docs/README.md`](https://github.com/MaskyS/yutome/blob/main/docs/README.md) for an index. The most useful starting points:

- [`docs/remote-access.md`](https://github.com/MaskyS/yutome/blob/main/docs/remote-access.md) — connecting Claude / ChatGPT / agents
- [`docs/architecture/README.md`](https://github.com/MaskyS/yutome/blob/main/docs/architecture/README.md) — current architecture map
- [`docs/cli-architecture.md`](https://github.com/MaskyS/yutome/blob/main/docs/cli-architecture.md) — CLI namespace and composition rules
- [`docs/cloud-capsule-strategy.md`](https://github.com/MaskyS/yutome/blob/main/docs/cloud-capsule-strategy.md) — how the Cloudflare Worker is designed
- [`docs/query-api.md`](https://github.com/MaskyS/yutome/blob/main/docs/query-api.md) — **Developer API**: the HTTP/MCP/CLI surfaces and the query language `find` / `list` / `show` / `q` speak
- [`docs/plan.md`](https://github.com/MaskyS/yutome/blob/main/docs/plan.md) — pointer to current architecture docs and archived planning history

## Status

**v0.1.0 — early release.** Released under the MIT license; the API and CLI surface may shift between point releases.

Found a bug or a confusing doc? Open an issue: <https://github.com/MaskyS/yutome/issues>.
