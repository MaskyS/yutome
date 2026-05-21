from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ytkb.channels import LibraryChannel, channel_from_input


YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SUBSCRIPTIONS_URI = "https://www.googleapis.com/youtube/v3/subscriptions"


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str | None = None
    auth_uri: str = AUTH_URI
    token_uri: str = TOKEN_URI


def load_oauth_client(client_secrets_path: Path) -> OAuthClient:
    payload = json.loads(client_secrets_path.read_text(encoding="utf-8"))
    config = payload.get("installed") or payload.get("web") or payload
    client_id = config.get("client_id")
    if not client_id:
        raise RuntimeError("OAuth client secrets file is missing client_id")
    return OAuthClient(
        client_id=client_id,
        client_secret=config.get("client_secret"),
        auth_uri=config.get("auth_uri", AUTH_URI),
        token_uri=config.get("token_uri", TOKEN_URI),
    )


def load_or_authorize_token(
    *,
    client: OAuthClient,
    token_path: Path,
    port: int = 0,
    open_browser: bool = True,
) -> dict[str, Any]:
    token = _load_token(token_path)
    if token and _token_is_valid(token):
        return token
    if token and token.get("refresh_token"):
        refreshed = _refresh_token(client=client, refresh_token=str(token["refresh_token"]))
        if "refresh_token" not in refreshed:
            refreshed["refresh_token"] = token["refresh_token"]
        _write_token(token_path, refreshed)
        return refreshed
    token = _authorize_token(client=client, port=port, open_browser=open_browser)
    _write_token(token_path, token)
    return token


def fetch_subscription_channels(access_token: str) -> list[LibraryChannel]:
    channels: list[LibraryChannel] = []
    page_token: str | None = None
    while True:
        params = {
            "part": "snippet",
            "mine": "true",
            "maxResults": "50",
        }
        if page_token:
            params["pageToken"] = page_token
        url = f"{SUBSCRIPTIONS_URI}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        for item in payload.get("items", []):
            snippet = item.get("snippet") or {}
            resource = snippet.get("resourceId") or {}
            channel_id = resource.get("channelId")
            if not channel_id:
                continue
            channel = channel_from_input(
                channel_id,
                title=snippet.get("title"),
                import_source="youtube-oauth",
            )
            if channel is not None:
                channels.append(channel)
        page_token = payload.get("nextPageToken")
        if not page_token:
            return channels


def _authorize_token(*, client: OAuthClient, port: int, open_browser: bool) -> dict[str, Any]:
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = _pkce_challenge(verifier)
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name.
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            if params.get("state", [None])[0] != state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"OAuth state mismatch. You can close this window.")
                return
            if error := params.get("error", [None])[0]:
                result["error"] = error
            if code := params.get("code", [None])[0]:
                result["code"] = code
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"YouTube authorization complete. You can close this window.")

        def log_message(self, _format: str, *args) -> None:
            return

    with http.server.HTTPServer(("127.0.0.1", port), Handler) as server:
        redirect_uri = f"http://127.0.0.1:{server.server_port}/"
        auth_url = _authorization_url(
            client=client,
            redirect_uri=redirect_uri,
            state=state,
            challenge=challenge,
        )
        if open_browser:
            webbrowser.open(auth_url)
        else:
            print(auth_url)
        server.handle_request()

    if error := result.get("error"):
        raise RuntimeError(f"YouTube OAuth authorization failed: {error}")
    code = result.get("code")
    if not code:
        raise RuntimeError("YouTube OAuth authorization returned no code")
    return _exchange_code(
        client=client,
        code=code,
        redirect_uri=redirect_uri,
        verifier=verifier,
    )


def _authorization_url(*, client: OAuthClient, redirect_uri: str, state: str, challenge: str) -> str:
    params = {
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": YOUTUBE_READONLY_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{client.auth_uri}?{urllib.parse.urlencode(params)}"


def _exchange_code(*, client: OAuthClient, code: str, redirect_uri: str, verifier: str) -> dict[str, Any]:
    fields = {
        "client_id": client.client_id,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }
    if client.client_secret:
        fields["client_secret"] = client.client_secret
    return _token_request(client.token_uri, fields)


def _refresh_token(*, client: OAuthClient, refresh_token: str) -> dict[str, Any]:
    fields = {
        "client_id": client.client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if client.client_secret:
        fields["client_secret"] = client.client_secret
    return _token_request(client.token_uri, fields)


def _token_request(token_uri: str, fields: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        token_uri,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        token = json.loads(response.read().decode("utf-8"))
    if expires_in := token.get("expires_in"):
        token["expires_at"] = time.time() + float(expires_in) - 60
    return token


def _load_token(token_path: Path) -> dict[str, Any] | None:
    if not token_path.exists():
        return None
    return json.loads(token_path.read_text(encoding="utf-8"))


def _write_token(token_path: Path, token: dict[str, Any]) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps(token, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass


def _token_is_valid(token: dict[str, Any]) -> bool:
    return bool(token.get("access_token")) and float(token.get("expires_at") or 0) > time.time()


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
