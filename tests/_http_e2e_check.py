"""End-to-end HTTP smoke test against `uv run yutome serve http` + live corpus.

Not part of pytest (spawns a subprocess, needs the indexed data dir).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from yutome.config import load_config
from yutome.db import connect_catalog
from yutome.paths import ProjectPaths


_QUERY_STOPWORDS = {
    "about",
    "after",
    "again",
    "friends",
    "welcome",
    "hello",
    "music",
    "there",
    "these",
    "thing",
    "think",
    "those",
    "today",
    "would",
}


def _live_corpus_smoke_query(repo_root: Path) -> str:
    config = load_config(repo_root / "yutome.toml")
    paths = ProjectPaths.from_config(config, project_root=repo_root)
    with connect_catalog(paths.catalog_db) as connection:
        rows = connection.execute(
            "SELECT text FROM chunks WHERE text IS NOT NULL AND length(text) > 200 ORDER BY created_at LIMIT 50"
        ).fetchall()
    for row in rows:
        for word in re.findall(r"[A-Za-z][A-Za-z'-]{4,}", row["text"]):
            candidate = word.strip("'").lower()
            if len(candidate) >= 8 and candidate not in _QUERY_STOPWORDS:
                return candidate
    raise RuntimeError("could not derive a smoke-test query from indexed chunks")


def _wait_for(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.0) as r:
                if r.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - polling for liveness.
            last_err = exc
        time.sleep(0.3)
    raise RuntimeError(f"server did not come up on :{port}; last error: {last_err}")


def _get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as r:
        return json.loads(r.read())


def _post(port: int, path: str, body: dict) -> dict | list:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    smoke_query = _live_corpus_smoke_query(repo_root)
    print("SMOKE QUERY:", smoke_query)
    port = 8766  # use a non-default port to avoid colliding with a real server.
    env = os.environ.copy()
    env.pop("YUTOME_HTTP_TOKEN", None)
    proc = subprocess.Popen(
        ["uv", "run", "yutome", "--config", "yutome.toml", "serve", "http", "--port", str(port)],
        cwd=str(repo_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for(port)

        health = _get(port, "/healthz")
        print("HEALTHZ:", health)
        assert health["ok"] is True

        status_result = _post(port, "/list", {"entity": "status"})
        status = status_result["rows"][0]
        print(
            "STATUS:",
            {k: status[k] for k in ("searchable_now", "videos", "chunks")},
        )
        assert status["videos"] > 0

        hits_result = _post(port, "/find", {"text": smoke_query, "mode": "lexical", "limit": 3})
        hits = hits_result["rows"]
        assert isinstance(hits, list) and hits, "expected lexical hits"
        first = hits[0]
        print(
            "FIRST HIT:",
            {
                "title": first.get("title"),
                "youtube_url": first["youtube_url"],
                "snippet": first["snippet"][:80] + "...",
            },
        )

        ctx = _post(
            port,
            "/show",
            {"kind": "context", "id": first["chunk_id"], "token_budget": 1500},
        )
        print(
            "CONTEXT:",
            f"estimated_tokens={ctx['estimated_tokens']} chunks={len(ctx['chunks'])}",
        )

        chunk_payload = _get(port, f"/chunks/{first['chunk_id']}")
        print("CHUNK RESOURCE:", f"{len(chunk_payload['text'])} chars of text")

        print("\nALL HTTP E2E CHECKS PASSED")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
