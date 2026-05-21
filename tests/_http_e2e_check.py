"""End-to-end HTTP smoke test against `uv run ytkb http serve` + live corpus.

Not part of pytest (spawns a subprocess, needs the indexed data dir).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


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
    port = 8766  # use a non-default port to avoid colliding with a real server.
    env = os.environ.copy()
    env.pop("YTKB_HTTP_TOKEN", None)
    proc = subprocess.Popen(
        ["uv", "run", "ytkb", "http", "serve", "--config", "ytkb.toml", "--port", str(port)],
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

        hits_result = _post(port, "/find", {"text": "Crohn probiotics", "mode": "lexical", "limit": 3})
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
