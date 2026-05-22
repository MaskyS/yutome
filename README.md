# Yutome

Yutome ingests transcripts from the YouTube channels you point at, stores them on your machine, and connects to whichever AI app you already use — Claude, ChatGPT, Cursor, anything MCP-compatible.

The library is searchable from the command line, from any local MCP client, or remotely from claude.ai / ChatGPT through a small Cloudflare Worker you deploy yourself.

Yutome is a command-line tool. You set it up from the terminal; from there it handles sync, cleanup, and exports. A coding agent (Claude Code, Cursor) can drive any of it.

## Install

```bash
uv tool install 'yutome[all]'
```

This puts a single `yutome` command on your PATH. The `[all]` extra pulls in the full feature set (yt-dlp, LanceDB, Voyage embeddings, MCP server, HTTP API). Without it you'll hit ImportErrors on first run.

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
pipx install 'yutome[all]'

# Install the latest unreleased commit
uv tool install 'yutome[all] @ git+https://github.com/MaskyS/yutome.git'

# For hacking on the code
git clone https://github.com/MaskyS/yutome.git
cd yutome
uv sync --extra all
uv run yutome --help
```

## Quickstart

```bash
yutome setup                                  # guided first-run: creates ./yutome.toml, ./data, etc.
yutome add https://www.youtube.com/@LexClips  # add a YouTube channel or video as a source
yutome sync                                   # discover videos, fetch transcripts, build indexes
yutome find "first principles"                # ranked search across everything indexed
```

`yutome setup` is interactive by default; pass `-y` to skip prompts and just print what would happen. It prompts for any API keys it needs. To set keys ahead of time, copy `.env.example` to `.env`. The only commonly-needed key is `VOYAGE_API_KEY` (semantic search) — get one at [voyageai.com](https://www.voyageai.com/). Without it, `find` still works but falls back to keyword search only. Every other key in `.env.example` is optional and tied to a specific feature (Gemini transcript cleanup, Webshare residential proxy, OAuth subscription import, etc.).

The indexed corpus lives under `./data/` next to `yutome.toml` — SQLite catalog, LanceDB vector index, transcript artifacts. Back it up like any other project directory.

Run `yutome --help` for the full surface. The most-used commands:

| Goal | Command |
|---|---|
| First-run setup | `yutome setup` |
| Add a channel/video | `yutome add <url>` |
| Import a subscription list | `yutome import-youtube` or `yutome import <file>` |
| Index everything new | `yutome sync` |
| Search | `yutome find "<query>"` |
| List or inspect indexed objects | `yutome list videos`, `yutome show video <id>`, … |
| Local MCP server | `yutome mcp serve` (usually invoked via Claude config, not by hand) |
| Deploy/manage remote connector | `yutome connect --deploy`, `yutome remote bridge`, `yutome status` |

Commands like `list`, `show`, `remote`, `export`, `quality` are groups — append `--help` (e.g. `yutome list --help`) to see their subcommands.

## Connect to Claude / ChatGPT

### Local (recommended for daily use)

For **Claude Code** or **Codex** opened against this repo, the bundled `.mcp.json` is picked up automatically — no extra config.

For **Claude Desktop**, add this block to `~/Library/Application Support/Claude/claude_desktop_config.json`. Use an absolute path to `yutome.toml`, since Desktop won't launch from your project directory:

```json
{
  "mcpServers": {
    "yutome": {
      "command": "uv",
      "args": ["run", "yutome", "mcp", "serve", "--config", "/absolute/path/to/yutome.toml"]
    }
  }
}
```

Restart Claude Desktop. You'll see `yutome` in the connectors list with `find`, `list`, `show`, and `q` tools.

### Remote (Claude.ai web, ChatGPT, phone)

```bash
yutome connect --deploy   # one-time: deploy the Worker, generate secrets, save state
yutome remote bridge      # keep this running while you want queries to work
```

`connect --deploy` deploys a Cloudflare Worker to your own account (free plan is enough), generates an OAuth-protected `/mcp` endpoint, and prints a pairing code. Paste the `/mcp` URL into a Claude.ai or ChatGPT custom connector and complete OAuth in the browser tab using the pairing code.

The Worker is just a relay — your corpus stays on your laptop. `yutome remote bridge` is the WebSocket process that lets the Worker reach it; if it's not running, the connector reports "desktop offline". Full setup walkthrough: [`docs/remote-access.md`](https://github.com/MaskyS/yutome/blob/main/docs/remote-access.md) and [`cloudflare/yutome-capsule/README.md`](https://github.com/MaskyS/yutome/blob/main/cloudflare/yutome-capsule/README.md).

## Docs

See [`docs/README.md`](https://github.com/MaskyS/yutome/blob/main/docs/README.md) for an index. The most useful starting points:

- [`docs/remote-access.md`](https://github.com/MaskyS/yutome/blob/main/docs/remote-access.md) — connecting Claude / ChatGPT / agents
- [`docs/cloud-capsule-strategy.md`](https://github.com/MaskyS/yutome/blob/main/docs/cloud-capsule-strategy.md) — how the Cloudflare Worker is designed
- [`docs/query-api.md`](https://github.com/MaskyS/yutome/blob/main/docs/query-api.md) — the query language `find` / `q` speak
- [`docs/plan.md`](https://github.com/MaskyS/yutome/blob/main/docs/plan.md) — internal architecture history (not a usage guide)

## Status

**v0.1.0 — early release.** Released under the MIT license; the API and CLI surface may shift between point releases.

Found a bug or a confusing doc? Open an issue: <https://github.com/MaskyS/yutome/issues>.
