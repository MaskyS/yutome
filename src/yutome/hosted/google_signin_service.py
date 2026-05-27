from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from yutome.youtube_oauth import OAuthClient, authorization_url, exchange_code


GOOGLE_SIGNIN_STATE_TTL_SECONDS = 10 * 60
GOOGLE_SIGNIN_SCOPES: tuple[str, ...] = ("openid", "email", "profile")
GOOGLE_USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"


class HostedGoogleSignInError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int = 400,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.data = dict(data or {})
        super().__init__(message)


@dataclass(frozen=True)
class GoogleSignInSettings:
    client: OAuthClient | None

    @property
    def configured(self) -> bool:
        return bool(self.client and self.client.client_id)


@dataclass(frozen=True)
class GoogleSignInIdentity:
    email: str
    name: str | None
    picture: str | None
    subject: str
    redirect_path: str | None


def google_signin_settings_from_env(environ: Mapping[str, str]) -> GoogleSignInSettings:
    client_id = _optional_text(environ.get("YUTOME_GOOGLE_OAUTH_CLIENT_ID")) or _optional_text(
        environ.get("YUTOME_YOUTUBE_OAUTH_CLIENT_ID")
    )
    if not client_id:
        return GoogleSignInSettings(client=None)
    return GoogleSignInSettings(
        client=OAuthClient(
            client_id=client_id,
            client_secret=_optional_text(environ.get("YUTOME_GOOGLE_OAUTH_CLIENT_SECRET"))
            or _optional_text(environ.get("YUTOME_YOUTUBE_OAUTH_CLIENT_SECRET")),
        )
    )


def start_google_signin(
    *,
    settings: GoogleSignInSettings,
    redirect_uri: str,
    redirect_path: str | None,
    state_secret: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    client = _require_client(settings)
    redirect_uri = _validate_google_signin_redirect_uri(redirect_uri)
    issued_at = now or datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=GOOGLE_SIGNIN_STATE_TTL_SECONDS)
    state = sign_google_signin_state(
        secret=state_secret,
        redirect_uri=redirect_uri,
        redirect_path=redirect_path,
        nonce=secrets.token_urlsafe(18),
        issued_at=issued_at,
        expires_at=expires_at,
    )
    return {
        "ok": True,
        "authorization_url": authorization_url(
            client=client,
            redirect_uri=redirect_uri,
            state=state,
            scopes=GOOGLE_SIGNIN_SCOPES,
            access_type=None,
            prompt=None,
        ),
        "scopes": list(GOOGLE_SIGNIN_SCOPES),
        "expires_at": expires_at.isoformat(),
    }


def complete_google_signin(
    *,
    settings: GoogleSignInSettings,
    code: str,
    state: str,
    redirect_uri: str,
    state_secret: str,
    now: datetime | None = None,
) -> GoogleSignInIdentity:
    client = _require_client(settings)
    redirect_uri = _validate_google_signin_redirect_uri(redirect_uri)
    state_claims = verify_google_signin_state(
        state,
        secret=state_secret,
        expected_redirect_uri=redirect_uri,
        now=now or datetime.now(timezone.utc),
    )
    try:
        token = exchange_code(client=client, code=code.strip(), redirect_uri=redirect_uri)
    except Exception as exc:
        raise HostedGoogleSignInError(
            code="google_signin_exchange_failed",
            message="Google did not accept the sign-in authorization code. Start again.",
            status_code=502,
        ) from exc
    access_token = _optional_text(token.get("access_token"))
    if access_token is None:
        raise HostedGoogleSignInError(
            code="google_signin_exchange_failed",
            message="Google did not return an access token. Start again.",
            status_code=502,
        )
    try:
        userinfo = fetch_google_userinfo(access_token)
    except Exception as exc:
        raise HostedGoogleSignInError(
            code="google_signin_userinfo_failed",
            message="Could not read your Google account profile. Start again.",
            status_code=502,
        ) from exc
    return google_identity_from_userinfo(userinfo, redirect_path=_optional_text(state_claims.get("redirect_path")))


def sign_google_signin_state(
    *,
    secret: str,
    redirect_uri: str,
    redirect_path: str | None,
    nonce: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    if not secret or not secret.strip():
        raise HostedGoogleSignInError(
            code="google_signin_state_secret_unconfigured",
            message="Set YUTOME_ACCOUNT_SESSION_HMAC_SECRET before using Google sign-in.",
            status_code=503,
        )
    payload = {
        "aud": "yutome:google-signin",
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "redirect_uri": redirect_uri,
        "redirect_path": redirect_path,
        "nonce": nonce,
    }
    encoded = _base64url(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = _base64url(hmac.new(secret.strip().encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def verify_google_signin_state(
    state: str,
    *,
    secret: str,
    expected_redirect_uri: str,
    now: datetime,
) -> dict[str, Any]:
    try:
        encoded, signature = state.split(".", 1)
    except ValueError as exc:
        raise HostedGoogleSignInError(
            code="google_signin_state_invalid",
            message="This Google sign-in attempt is invalid.",
            status_code=401,
        ) from exc
    expected_signature = _base64url(
        hmac.new(secret.strip().encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(signature, expected_signature):
        raise HostedGoogleSignInError(
            code="google_signin_state_invalid",
            message="This Google sign-in attempt is invalid.",
            status_code=401,
        )
    try:
        payload = json.loads(_base64url_decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HostedGoogleSignInError(
            code="google_signin_state_invalid",
            message="This Google sign-in attempt is invalid.",
            status_code=401,
        ) from exc
    if payload.get("aud") != "yutome:google-signin" or payload.get("redirect_uri") != expected_redirect_uri:
        raise HostedGoogleSignInError(
            code="google_signin_state_invalid",
            message="This Google sign-in attempt does not match the current session.",
            status_code=401,
        )
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp <= int(now.timestamp()):
        raise HostedGoogleSignInError(
            code="google_signin_state_expired",
            message="This Google sign-in attempt has expired. Start again.",
            status_code=401,
        )
    return payload


def fetch_google_userinfo(access_token: str) -> dict[str, Any]:
    request = urllib.request.Request(GOOGLE_USERINFO_URI, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Google userinfo response was not an object.")
    return payload


def google_identity_from_userinfo(userinfo: Mapping[str, Any], *, redirect_path: str | None) -> GoogleSignInIdentity:
    email = _optional_text(userinfo.get("email"))
    subject = _optional_text(userinfo.get("sub"))
    if email is None or subject is None:
        raise HostedGoogleSignInError(
            code="google_signin_identity_invalid",
            message="Google did not return a usable account identity.",
            status_code=401,
        )
    verified = userinfo.get("email_verified")
    if verified is False or (isinstance(verified, str) and verified.lower() == "false"):
        raise HostedGoogleSignInError(
            code="google_signin_email_unverified",
            message="Your Google account email is not verified.",
            status_code=401,
        )
    return GoogleSignInIdentity(
        email=email,
        name=_optional_text(userinfo.get("name")),
        picture=_optional_text(userinfo.get("picture")),
        subject=subject,
        redirect_path=redirect_path,
    )


def _require_client(settings: GoogleSignInSettings) -> OAuthClient:
    if not settings.configured or settings.client is None:
        raise HostedGoogleSignInError(
            code="google_signin_unconfigured",
            message="Google sign-in is not configured.",
            status_code=503,
        )
    return settings.client


def _validate_google_signin_redirect_uri(value: str) -> str:
    uri = value.strip()
    try:
        parsed = urlsplit(uri)
    except ValueError as exc:
        raise HostedGoogleSignInError(
            code="google_signin_redirect_uri_invalid",
            message="Google sign-in redirect URI is invalid.",
            status_code=400,
        ) from exc
    host = parsed.hostname or ""
    local = parsed.scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}
    if not (parsed.scheme == "https" or local) or parsed.fragment:
        raise HostedGoogleSignInError(
            code="google_signin_redirect_uri_invalid",
            message="Google sign-in redirect URI must be HTTPS, except localhost development callbacks.",
            status_code=400,
        )
    if parsed.path != "/auth/google/callback":
        raise HostedGoogleSignInError(
            code="google_signin_redirect_uri_invalid",
            message="Google sign-in redirect URI must point to the Google auth callback.",
            status_code=400,
        )
    return uri


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


__all__ = [
    "GOOGLE_SIGNIN_SCOPES",
    "GoogleSignInIdentity",
    "GoogleSignInSettings",
    "HostedGoogleSignInError",
    "complete_google_signin",
    "google_signin_settings_from_env",
    "start_google_signin",
]
