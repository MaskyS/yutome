"""End-to-end OAuth + MCP smoke test against a deployed yutome Worker.

Not part of the pytest suite (filename starts with underscore). Run directly:

    YUTOME_REMOTE_URL=https://yutome-remote-mcp.<acct>.workers.dev \
    YUTOME_PAIRING_CODE=ABC123 \
    uv run python tests/_oauth_e2e_check.py

The script simulates a Claude-style custom connector:

1. Dynamic Client Registration (DCR) at /register.
2. PKCE S256 challenge.
3. GET /authorize → parse the hidden auth-request id + CSRF token from the pairing form.
4. POST /pair with the pairing code + hidden auth state. Capture the
   redirect to the registered redirect_uri with the auth code.
5. POST /token with the code + verifier → access_token.
6. POST /mcp with the access_token: initialize, tools/list,
   resources/templates/list, resources/read.

Pass-through requires the laptop bridge (``yutome bridge start``) to be
running so resources/read can reach a chunk.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import urllib.parse
import urllib.request


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _add_cookie(jar: dict[str, str], set_cookie: str) -> dict[str, str]:
    updated = dict(jar)
    cookie_pair = set_cookie.split(";", 1)[0]
    if "=" not in cookie_pair:
        return updated
    name, value = cookie_pair.split("=", 1)
    updated[name] = value
    return updated


_DEFAULT_UA = "Mozilla/5.0 (Macintosh; yutome-smoke) urllib/3.12"


def _http(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    allow_redirect: bool = False,
) -> tuple[int, dict[str, str], bytes]:
    merged = {"user-agent": _DEFAULT_UA, **(headers or {})}
    request = urllib.request.Request(url, method=method, headers=merged, data=body)

    class _Handler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # noqa: ANN001, ANN002
            return None if not allow_redirect else super().redirect_request(*args, **kwargs)

    opener = urllib.request.build_opener(_Handler())
    try:
        with opener.open(request) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def main() -> int:
    base = os.environ.get("YUTOME_REMOTE_URL", "").rstrip("/")
    pairing_code = os.environ.get("YUTOME_PAIRING_CODE", "")
    if not base or not pairing_code:
        print("Set YUTOME_REMOTE_URL and YUTOME_PAIRING_CODE.", file=sys.stderr)
        return 2

    # ---- 1. DCR ----
    redirect_uri = "https://example.com/callback"
    status, _, body = _http(
        "POST",
        f"{base}/register",
        headers={"content-type": "application/json"},
        body=json.dumps(
            {
                "client_name": "yutome-smoke",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }
        ).encode(),
    )
    if status not in (200, 201):
        print(f"register failed: HTTP {status}\n{body.decode(errors='replace')}", file=sys.stderr)
        return 1
    client = json.loads(body)
    client_id = client["client_id"]
    print(f"[OK] DCR client_id={client_id}")

    # ---- 2. PKCE ----
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(16))

    auth_url = (
        f"{base}/authorize?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": "yutome.search.read",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )

    # ---- 3. GET /authorize → pairing form ----
    status, auth_headers, body = _http("GET", auth_url)
    if status != 200:
        print(f"authorize GET failed: HTTP {status}\n{body.decode(errors='replace')}", file=sys.stderr)
        return 1
    html = body.decode("utf-8", errors="replace")
    auth_id_match = re.search(r'name="auth_request_id"\s+value="([^"]+)"', html)
    csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    cookie = auth_headers.get("Set-Cookie") or auth_headers.get("set-cookie") or ""
    if not auth_id_match or not csrf_match:
        print("could not find auth_request_id/csrf pairing state on /authorize page", file=sys.stderr)
        return 1
    auth_request_id = auth_id_match.group(1)
    csrf_token = csrf_match.group(1)
    expected_cookie_name = f"__Host-yutome_pairing_{auth_request_id}"
    cookie_jar = _add_cookie({}, cookie)
    if expected_cookie_name not in cookie_jar:
        print("could not find auth-request-specific CSRF cookie on /authorize page", file=sys.stderr)
        return 1
    print("[OK] GET /authorize rendered pairing form")

    # Simulate a noob clicking Connect more than once. Browsers keep all
    # auth-request-specific cookies, but a single global cookie would be
    # overwritten here and make the first visible tab fail.
    status, second_headers, second_body = _http("GET", auth_url)
    if status != 200:
        print(
            f"second authorize GET failed: HTTP {status}\n{second_body.decode(errors='replace')}",
            file=sys.stderr,
        )
        return 1
    second_html = second_body.decode("utf-8", errors="replace")
    second_auth_id_match = re.search(r'name="auth_request_id"\s+value="([^"]+)"', second_html)
    if not second_auth_id_match:
        print("could not find second auth_request_id on /authorize page", file=sys.stderr)
        return 1
    second_cookie = second_headers.get("Set-Cookie") or second_headers.get("set-cookie") or ""
    cookie_jar = _add_cookie(cookie_jar, second_cookie)
    if f"__Host-yutome_pairing_{second_auth_id_match.group(1)}" not in cookie_jar:
        print("could not find second auth-request-specific CSRF cookie on /authorize page", file=sys.stderr)
        return 1
    cookie_header = "; ".join(f"{key}={value}" for key, value in cookie_jar.items())
    print("[OK] overlapping /authorize tabs preserved independent pairing state")

    # ---- 4. POST /pair ----
    form_body = urllib.parse.urlencode(
        {"pairing_code": pairing_code, "auth_request_id": auth_request_id, "csrf_token": csrf_token}
    ).encode()
    status, headers, body = _http(
        "POST",
        f"{base}/pair",
        headers={"content-type": "application/x-www-form-urlencoded", "cookie": cookie_header},
        body=form_body,
    )
    if status != 302:
        print(
            f"pair POST did not redirect: HTTP {status}\n{body.decode(errors='replace')}",
            file=sys.stderr,
        )
        return 1
    location = headers.get("Location") or headers.get("location") or ""
    status, retry_headers, retry_body = _http(
        "POST",
        f"{base}/pair",
        headers={"content-type": "application/x-www-form-urlencoded", "cookie": cookie_header},
        body=form_body,
    )
    if status != 302:
        print(
            f"pair POST retry did not redirect: HTTP {status}\n{retry_body.decode(errors='replace')}",
            file=sys.stderr,
        )
        return 1
    retry_location = retry_headers.get("Location") or retry_headers.get("location") or ""
    if retry_location != location:
        print("pair POST retry redirected to a different callback URL", file=sys.stderr)
        return 1
    print("[OK] /pair retry reused completed authorization redirect")

    parsed = urllib.parse.urlsplit(location)
    code = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]
    if not code:
        print(f"pair redirect missing code: {location}", file=sys.stderr)
        return 1
    print(f"[OK] /pair issued auth code")

    # ---- 5. POST /token ----
    status, _, body = _http(
        "POST",
        f"{base}/token",
        headers={"content-type": "application/x-www-form-urlencoded"},
        body=urllib.parse.urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            }
        ).encode(),
    )
    if status != 200:
        print(f"token failed: HTTP {status}\n{body.decode(errors='replace')}", file=sys.stderr)
        return 1
    token_payload = json.loads(body)
    access_token = token_payload["access_token"]
    print(f"[OK] /token issued access_token (scope={token_payload.get('scope')})")

    # ---- 6. /mcp calls ----
    session_id: str | None = None

    def mcp(method: str, params: dict | None = None, rid: int = 1) -> dict:
        nonlocal session_id
        request_headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "authorization": f"Bearer {access_token}",
        }
        if session_id:
            request_headers["mcp-session-id"] = session_id
        status, headers, body = _http(
            "POST",
            f"{base}/mcp",
            headers=request_headers,
            body=json.dumps(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
            ).encode(),
        )
        # Capture Mcp-Session-Id from the initialize response.
        for key in ("Mcp-Session-Id", "mcp-session-id"):
            if key in headers:
                session_id = headers[key]
                break
        if status != 200:
            print(f"{method} failed: HTTP {status}\n{body.decode(errors='replace')}", file=sys.stderr)
            sys.exit(1)
        # Streamable HTTP can return JSON or SSE depending on negotiation.
        text = body.decode("utf-8", errors="replace")
        if text.startswith("event:") or text.startswith("data:"):
            data_lines = [
                line[len("data: ") :] for line in text.splitlines() if line.startswith("data: ")
            ]
            return json.loads(data_lines[-1]) if data_lines else {}
        return json.loads(text)

    print("---")
    init = mcp(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "yutome-smoke", "version": "0"},
        },
    )
    caps = init.get("result", {}).get("capabilities", {})
    print(f"[OK] initialize capabilities={list(caps.keys())}")

    tools = mcp("tools/list").get("result", {}).get("tools", [])
    print(f"[OK] tools/list -> {[t['name'] for t in tools]}")

    templates = mcp("resources/templates/list").get("result", {}).get("resourceTemplates", [])
    print(f"[OK] resources/templates/list -> {[t['uriTemplate'] for t in templates]}")

    # tools/call list status — returns one row describing corpus health.
    # (Note: tools/call list entity=videos order_by=newest hits a preexisting
    # bug where _order doesn't translate "newest" to "published_at"; tracking
    # separately.)
    call = mcp(
        "tools/call",
        {"name": "list", "arguments": {"entity": "status"}},
    )
    result = call.get("result", {})
    rows = (result.get("structuredContent") or {}).get("rows", [])
    if rows:
        first = rows[0]
        print(f"[OK] tools/call list status -> {first}")
    else:
        print(f"[WARN] tools/call list returned no rows. Raw: {result}")

    # Try a resource read for the first indexed video (if any).
    videos_call = mcp(
        "tools/call",
        {"name": "list", "arguments": {"entity": "videos", "limit": 1}},
    )
    video_rows = (videos_call.get("result", {}).get("structuredContent") or {}).get("rows", [])
    if video_rows and "video_id" in video_rows[0]:
        vid = video_rows[0]["video_id"]
        read = mcp("resources/read", {"uri": f"yutome://video/{vid}"})
        contents = read.get("result", {}).get("contents", [])
        if contents:
            print(
                f"[OK] resources/read yutome://video/{vid} -> "
                f"mimeType={contents[0].get('mimeType')} len={len(contents[0].get('text', ''))}"
            )
        else:
            print(f"[WARN] resources/read returned no contents: {read}")
    else:
        print(f"[WARN] no videos in corpus to resource-read against")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
