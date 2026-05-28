from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import bindparam, cast, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert

from yutome.hosted.ids import input_hash
from yutome.hosted.repositories import SqlStatement
from yutome.hosted.schema import youtube_grants
from yutome.hosted.source_import import (
    HostedSourceImportActor,
    HostedSourceImportDescriptor,
    HostedSourcesImportRequest,
    import_sources,
)
from yutome.hosted.sqlalchemy_core import compile_postgres_statement
from yutome.youtube_oauth import (
    OAuthClient,
    YOUTUBE_READONLY_SCOPE,
    authorization_url,
    exchange_code,
    fetch_subscription_channels,
    pkce_challenge,
    refresh_token,
)


YOUTUBE_OAUTH_STATE_TTL_SECONDS = 10 * 60
YOUTUBE_SUBSCRIPTION_IMPORT_SOURCE = "youtube_oauth_subscriptions"
YOUTUBE_TOKEN_ENCRYPTION_ENV_VAR = "YUTOME_YOUTUBE_OAUTH_TOKEN_ENCRYPTION_KEY"
YOUTUBE_TOKEN_ENCRYPTION_ALGORITHM = "aesgcm-sha256-v1"


class HostedYouTubeOAuthError(RuntimeError):
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
class YouTubeOAuthSettings:
    client: OAuthClient | None
    token_encryption_key: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.client and self.client.client_id)


def youtube_oauth_settings_from_env(environ: Mapping[str, str]) -> YouTubeOAuthSettings:
    client_id = _optional_text(environ.get("YUTOME_YOUTUBE_OAUTH_CLIENT_ID"))
    if not client_id:
        return YouTubeOAuthSettings(client=None)
    return YouTubeOAuthSettings(
        client=OAuthClient(
            client_id=client_id,
            client_secret=_optional_text(environ.get("YUTOME_YOUTUBE_OAUTH_CLIENT_SECRET")),
        ),
        token_encryption_key=_optional_text(environ.get(YOUTUBE_TOKEN_ENCRYPTION_ENV_VAR)),
    )


def youtube_connection_status(
    connection: Any,
    *,
    workspace_id: str,
    user_id: str,
    configured: bool,
) -> dict[str, Any]:
    grant = load_active_youtube_grant(connection, workspace_id=workspace_id, user_id=user_id)
    return {
        "ok": True,
        "configured": configured,
        "connected": grant is not None,
        "scope": YOUTUBE_READONLY_SCOPE,
        "grant": _public_grant_json(grant) if grant is not None else None,
    }


def start_youtube_authorization(
    connection: Any,
    *,
    settings: YouTubeOAuthSettings,
    workspace_id: str,
    user_id: str,
    redirect_uri: str,
    state_secret: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    client = _require_client(settings)
    token_encryption_key = _require_token_encryption_key(settings)
    redirect_uri = _validate_dashboard_redirect_uri(redirect_uri)
    issued_at = now or datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=YOUTUBE_OAUTH_STATE_TTL_SECONDS)
    grant_id = new_youtube_grant_id(workspace_id=workspace_id, user_id=user_id)
    verifier = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(18)
    state = sign_youtube_oauth_state(
        secret=state_secret,
        workspace_id=workspace_id,
        user_id=user_id,
        grant_id=grant_id,
        redirect_uri=redirect_uri,
        nonce=nonce,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    statement = create_pending_youtube_grant_sql(
        grant_id=grant_id,
        user_id=user_id,
        workspace_id=workspace_id,
        state_hash=oauth_state_hash(state),
        code_verifier=verifier,
        token_encryption_key=token_encryption_key,
        redirect_uri=redirect_uri,
        expires_at=expires_at,
        now=issued_at,
    )
    connection.execute(statement.sql, statement.params)
    return {
        "ok": True,
        "authorization_url": authorization_url(
            client=client,
            redirect_uri=redirect_uri,
            state=state,
            challenge=pkce_challenge(verifier),
        ),
        "grant_id": grant_id,
        "scope": YOUTUBE_READONLY_SCOPE,
        "expires_at": expires_at.isoformat(),
    }


def complete_youtube_authorization(
    connection: Any,
    *,
    settings: YouTubeOAuthSettings,
    workspace_id: str,
    user_id: str,
    code: str,
    state: str,
    redirect_uri: str,
    state_secret: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    client = _require_client(settings)
    token_encryption_key = _require_token_encryption_key(settings)
    redirect_uri = _validate_dashboard_redirect_uri(redirect_uri)
    clock = now or datetime.now(timezone.utc)
    state_claims = verify_youtube_oauth_state(
        state,
        secret=state_secret,
        expected_workspace_id=workspace_id,
        expected_user_id=user_id,
        expected_redirect_uri=redirect_uri,
        now=clock,
    )
    grant = load_youtube_grant_by_id(connection, grant_id=state_claims["grant_id"])
    if grant is None or str(grant.get("status") or "") != "pending":
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_state_invalid",
            message="This YouTube connection attempt is invalid or has already been used.",
            status_code=401,
        )
    metadata = _json_object(grant.get("metadata_json"))
    if metadata.get("state_hash") != oauth_state_hash(state) or metadata.get("redirect_uri") != redirect_uri:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_state_invalid",
            message="This YouTube connection attempt does not match the current session.",
            status_code=401,
        )
    pending_expires_at = _row_datetime(grant.get("expires_at")) or _metadata_datetime(metadata, "state_expires_at")
    if pending_expires_at is not None and pending_expires_at <= clock:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_state_expired",
            message="This YouTube connection attempt has expired. Start again.",
            status_code=401,
        )
    verifier = _metadata_secret(
        metadata,
        "code_verifier",
        token_encryption_key=token_encryption_key,
        aad=_secret_aad(str(grant["id"]), "code_verifier"),
    )
    if verifier is None:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_state_invalid",
            message="This YouTube connection attempt is missing its PKCE verifier.",
            status_code=401,
        )
    try:
        token = exchange_code(client=client, code=code.strip(), redirect_uri=redirect_uri, verifier=verifier)
    except Exception as exc:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_exchange_failed",
            message="Google did not accept the YouTube authorization code. Start again.",
            status_code=502,
        ) from exc
    active_metadata = _token_metadata(
        token,
        connected_at=clock,
        redirect_uri=redirect_uri,
        previous_metadata={},
        grant_id=str(grant["id"]),
        token_encryption_key=token_encryption_key,
    )
    activate_statement = activate_youtube_grant_sql(
        grant_id=str(grant["id"]),
        workspace_id=workspace_id,
        user_id=user_id,
        state_hash=oauth_state_hash(state),
        metadata=active_metadata,
        grant_expires_at=_grant_expires_at(active_metadata),
    )
    rows = _rows_from_result(connection.execute(activate_statement.sql, activate_statement.params))
    active = rows[0] if rows else None
    if active is None:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_state_replayed",
            message="This YouTube connection attempt was already used.",
            status_code=401,
        )
    return {
        "ok": True,
        "configured": True,
        "connected": True,
        "scope": YOUTUBE_READONLY_SCOPE,
        "grant": _public_grant_json(active),
    }


def list_youtube_subscription_channels(
    connection: Any,
    *,
    settings: YouTubeOAuthSettings,
    workspace_id: str,
    user_id: str,
    limit: int | None = None,
) -> dict[str, Any]:
    _require_client(settings)
    grant = _require_active_grant(connection, workspace_id=workspace_id, user_id=user_id)
    access_token = access_token_for_grant(connection, settings=settings, grant=grant)
    channels = _fetch_channels_or_mark_invalid(connection, grant=grant, access_token=access_token)
    if limit is not None:
        channels = channels[: max(1, min(limit, 250))]
    return {
        "ok": True,
        "workspace_id": workspace_id,
        "grant": _public_grant_json(load_youtube_grant_by_id(connection, grant_id=str(grant["id"])) or grant),
        "channels": [_channel_json(channel) for channel in channels],
    }


def import_youtube_subscription_channels(
    connection: Any,
    *,
    settings: YouTubeOAuthSettings,
    workspace_id: str,
    user_id: str,
    channel_ids: Sequence[str],
    refresh_enabled: bool,
    max_new_videos: int,
    cadence_seconds: int,
) -> dict[str, Any]:
    normalized_ids = tuple(dict.fromkeys(_optional_text(channel_id) for channel_id in channel_ids))
    normalized_ids = tuple(channel_id for channel_id in normalized_ids if channel_id)
    if not normalized_ids:
        raise HostedYouTubeOAuthError(
            code="youtube_subscription_selection_required",
            message="Select at least one YouTube subscription to import.",
        )
    if len(normalized_ids) > 250:
        raise HostedYouTubeOAuthError(
            code="youtube_subscription_selection_too_large",
            message="Import at most 250 subscriptions at a time.",
        )
    _require_client(settings)
    grant = _require_active_grant(connection, workspace_id=workspace_id, user_id=user_id)
    access_token = access_token_for_grant(connection, settings=settings, grant=grant)
    channels = _fetch_channels_or_mark_invalid(connection, grant=grant, access_token=access_token)
    by_id = {channel.channel_id: channel for channel in channels if channel.channel_id}
    missing = [channel_id for channel_id in normalized_ids if channel_id not in by_id]
    if missing:
        raise HostedYouTubeOAuthError(
            code="youtube_subscription_selection_invalid",
            message="One or more selected channels are not in the connected YouTube subscriptions.",
            data={"channel_ids": missing},
        )
    descriptors = [
        HostedSourceImportDescriptor(
            source_url=by_id[channel_id].source_url,
            channel_id=channel_id,
            display_name=by_id[channel_id].title,
            selected=True,
            # Import the selected channels as public channel sources. The YouTube
            # grant is only used to discover the user's subscription list.
            import_source="public_api",
            metadata={
                "selected_from": YOUTUBE_SUBSCRIPTION_IMPORT_SOURCE,
                "youtube_grant_id": str(grant["id"]),
            },
        )
        for channel_id in normalized_ids
    ]
    return import_sources(
        connection,
        request=HostedSourcesImportRequest(
            sources=descriptors,
            refresh_enabled=refresh_enabled,
            max_new_videos=max(1, min(int(max_new_videos), 250)),
            cadence_seconds=max(60, min(int(cadence_seconds), 86400)),
        ),
        actor=HostedSourceImportActor(
            workspace_id=workspace_id,
            user_id=user_id,
            seeded_by=YOUTUBE_SUBSCRIPTION_IMPORT_SOURCE,
        ),
    )


def revoke_youtube_connection(
    connection: Any,
    *,
    workspace_id: str,
    user_id: str,
) -> dict[str, Any]:
    grant = load_active_youtube_grant(connection, workspace_id=workspace_id, user_id=user_id)
    if grant is None:
        return {"ok": True, "revoked": False}
    statement = revoke_youtube_grant_sql(
        grant_id=str(grant["id"]),
        workspace_id=workspace_id,
        user_id=user_id,
        now=datetime.now(timezone.utc),
    )
    connection.execute(statement.sql, statement.params)
    return {"ok": True, "revoked": True, "grant_id": str(grant["id"])}


def access_token_for_grant(connection: Any, *, settings: YouTubeOAuthSettings, grant: Mapping[str, Any]) -> str:
    client = _require_client(settings)
    token_encryption_key = _require_token_encryption_key(settings)
    grant_id = str(grant["id"])
    metadata = _json_object(grant.get("metadata_json"))
    token = _metadata_secret(
        metadata,
        "access_token",
        token_encryption_key=token_encryption_key,
        aad=_secret_aad(grant_id, "access_token"),
    )
    access_expires_at = _metadata_datetime(metadata, "access_token_expires_at")
    if token and (access_expires_at is None or access_expires_at > datetime.now(timezone.utc) + timedelta(seconds=60)):
        statement = mark_youtube_grant_used_sql(grant_id=str(grant["id"]))
        connection.execute(statement.sql, statement.params)
        return token
    refresh = _metadata_secret(
        metadata,
        "refresh_token",
        token_encryption_key=token_encryption_key,
        aad=_secret_aad(grant_id, "refresh_token"),
    )
    if refresh is None:
        _expire_grant(connection, grant=grant)
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_reconnect_required",
            message="Reconnect YouTube before importing subscriptions.",
            status_code=401,
        )
    try:
        refreshed = refresh_token(client=client, refresh_token=refresh)
    except Exception as exc:
        _expire_grant(connection, grant=grant)
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_reconnect_required",
            message="Reconnect YouTube before importing subscriptions.",
            status_code=401,
        ) from exc
    refreshed_metadata = _token_metadata(
        refreshed,
        connected_at=_metadata_datetime(metadata, "connected_at") or datetime.now(timezone.utc),
        redirect_uri=str(metadata.get("redirect_uri") or ""),
        previous_metadata=metadata,
        grant_id=grant_id,
        token_encryption_key=token_encryption_key,
    )
    statement = update_youtube_grant_token_sql(
        grant_id=str(grant["id"]),
        metadata=refreshed_metadata,
        grant_expires_at=_grant_expires_at(refreshed_metadata),
    )
    rows = _rows_from_result(connection.execute(statement.sql, statement.params))
    row = rows[0] if rows else {"metadata_json": refreshed_metadata}
    refreshed_row_metadata = _json_object(row.get("metadata_json"))
    access_token = _metadata_secret(
        refreshed_row_metadata,
        "access_token",
        token_encryption_key=token_encryption_key,
        aad=_secret_aad(grant_id, "access_token"),
    )
    if access_token is None:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_reconnect_required",
            message="Reconnect YouTube before importing subscriptions.",
            status_code=401,
        )
    return access_token


def _fetch_channels_or_mark_invalid(connection: Any, *, grant: Mapping[str, Any], access_token: str) -> list[Any]:
    try:
        channels = fetch_subscription_channels(access_token)
    except urllib.error.HTTPError as exc:
        if exc.code in {400, 401, 403}:
            _expire_grant(connection, grant=grant, status="invalid")
            raise HostedYouTubeOAuthError(
                code="youtube_oauth_reconnect_required",
                message="Reconnect YouTube before importing subscriptions.",
                status_code=401,
            ) from exc
        raise HostedYouTubeOAuthError(
            code="youtube_subscriptions_fetch_failed",
            message="Could not load YouTube subscriptions right now.",
            status_code=502,
        ) from exc
    except Exception as exc:
        raise HostedYouTubeOAuthError(
            code="youtube_subscriptions_fetch_failed",
            message="Could not load YouTube subscriptions right now.",
            status_code=502,
        ) from exc
    statement = mark_youtube_grant_used_sql(grant_id=str(grant["id"]))
    connection.execute(statement.sql, statement.params)
    return channels


def _require_client(settings: YouTubeOAuthSettings) -> OAuthClient:
    if settings.client is None or not settings.client.client_id:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_unconfigured",
            message="YouTube connection is not configured for this hosted app.",
            status_code=503,
        )
    return settings.client


def _require_token_encryption_key(settings: YouTubeOAuthSettings) -> str:
    key = _optional_text(settings.token_encryption_key)
    if key is None:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_token_encryption_unconfigured",
            message=f"Set {YOUTUBE_TOKEN_ENCRYPTION_ENV_VAR} before using YouTube OAuth.",
            status_code=503,
        )
    return key


def _require_active_grant(connection: Any, *, workspace_id: str, user_id: str) -> Mapping[str, Any]:
    grant = load_active_youtube_grant(connection, workspace_id=workspace_id, user_id=user_id)
    if grant is None:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_reconnect_required",
            message="Connect YouTube before importing subscriptions.",
            status_code=401,
        )
    return grant


def new_youtube_grant_id(*, workspace_id: str, user_id: str) -> str:
    return "ytg_" + input_hash(
        {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "scope": YOUTUBE_READONLY_SCOPE,
            "nonce": secrets.token_urlsafe(16),
        },
        prefix="",
    ).lstrip("_")[:28]


def sign_youtube_oauth_state(
    *,
    secret: str,
    workspace_id: str,
    user_id: str,
    grant_id: str,
    redirect_uri: str,
    nonce: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    if not secret or not secret.strip():
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_state_secret_unconfigured",
            message="Set YUTOME_ACCOUNT_SESSION_HMAC_SECRET before using YouTube OAuth.",
            status_code=503,
        )
    payload = {
        "aud": "yutome:youtube-oauth",
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "workspace_id": workspace_id,
        "user_id": user_id,
        "grant_id": grant_id,
        "redirect_uri": redirect_uri,
        "nonce": nonce,
    }
    encoded = _base64url(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = _base64url(hmac.new(secret.strip().encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def verify_youtube_oauth_state(
    state: str,
    *,
    secret: str,
    expected_workspace_id: str,
    expected_user_id: str,
    expected_redirect_uri: str,
    now: datetime,
) -> dict[str, Any]:
    try:
        encoded, signature = state.split(".", 1)
    except ValueError as exc:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_state_invalid",
            message="This YouTube connection attempt is invalid.",
            status_code=401,
        ) from exc
    expected_signature = _base64url(
        hmac.new(secret.strip().encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(signature, expected_signature):
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_state_invalid",
            message="This YouTube connection attempt is invalid.",
            status_code=401,
        )
    try:
        payload = json.loads(_base64url_decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_state_invalid",
            message="This YouTube connection attempt is invalid.",
            status_code=401,
        ) from exc
    if (
        payload.get("aud") != "yutome:youtube-oauth"
        or payload.get("workspace_id") != expected_workspace_id
        or payload.get("user_id") != expected_user_id
        or payload.get("redirect_uri") != expected_redirect_uri
    ):
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_state_invalid",
            message="This YouTube connection attempt does not match the current session.",
            status_code=401,
        )
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp <= int(now.timestamp()):
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_state_expired",
            message="This YouTube connection attempt has expired. Start again.",
            status_code=401,
        )
    grant_id = _optional_text(payload.get("grant_id"))
    if grant_id is None:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_state_invalid",
            message="This YouTube connection attempt is invalid.",
            status_code=401,
        )
    return payload


def oauth_state_hash(state: str) -> str:
    return "sha256:" + hashlib.sha256(state.encode("utf-8")).hexdigest()


def create_pending_youtube_grant_sql(
    *,
    grant_id: str,
    user_id: str,
    workspace_id: str,
    state_hash: str,
    code_verifier: str,
    token_encryption_key: str,
    redirect_uri: str,
    expires_at: datetime,
    now: datetime,
) -> SqlStatement:
    metadata = {
        "purpose": "youtube_oauth_authorization_code",
        "state_hash": state_hash,
        "token_encryption": YOUTUBE_TOKEN_ENCRYPTION_ALGORITHM,
        "code_verifier_ciphertext": _encrypt_metadata_secret(
            code_verifier,
            token_encryption_key=token_encryption_key,
            aad=_secret_aad(grant_id, "code_verifier"),
        ),
        "code_challenge_method": "S256",
        "redirect_uri": redirect_uri,
        "state_expires_at": expires_at.isoformat(),
    }
    statement = (
        insert(youtube_grants)
        .values(
            id=bindparam("id", value=grant_id),
            user_id=bindparam("user_id", value=user_id),
            workspace_id=bindparam("workspace_id", value=workspace_id),
            scopes=bindparam("scopes", value=[YOUTUBE_READONLY_SCOPE]),
            status=literal("pending"),
            metadata_json=cast(bindparam("metadata_json", value=_json_param(metadata)), JSONB),
            created_at=bindparam("created_at", value=now),
            updated_at=bindparam("updated_at", value=now),
            expires_at=bindparam("expires_at", value=expires_at),
        )
        .returning(youtube_grants)
    )
    return _sql_statement(statement)


def activate_youtube_grant_sql(
    *,
    grant_id: str,
    workspace_id: str,
    user_id: str,
    state_hash: str,
    metadata: Mapping[str, Any],
    grant_expires_at: datetime | None,
) -> SqlStatement:
    statement = (
        update(youtube_grants)
        .where(
            youtube_grants.c.id == bindparam("grant_id", value=grant_id),
            youtube_grants.c.workspace_id == bindparam("workspace_id", value=workspace_id),
            youtube_grants.c.user_id == bindparam("user_id", value=user_id),
            youtube_grants.c.status == literal("pending"),
            youtube_grants.c.metadata_json["state_hash"].astext == bindparam("state_hash", value=state_hash),
        )
        .values(
            status=literal("active"),
            scopes=bindparam("scopes", value=[YOUTUBE_READONLY_SCOPE]),
            metadata_json=cast(bindparam("metadata_json", value=_json_param(metadata)), JSONB),
            expires_at=bindparam("grant_expires_at", value=grant_expires_at),
            updated_at=func.now(),
            last_used_at=func.now(),
        )
        .returning(youtube_grants)
    )
    return _sql_statement(statement)


def update_youtube_grant_token_sql(
    *,
    grant_id: str,
    metadata: Mapping[str, Any],
    grant_expires_at: datetime | None,
) -> SqlStatement:
    statement = (
        update(youtube_grants)
        .where(
            youtube_grants.c.id == bindparam("grant_id", value=grant_id),
            youtube_grants.c.status == literal("active"),
        )
        .values(
            metadata_json=cast(bindparam("metadata_json", value=_json_param(metadata)), JSONB),
            expires_at=bindparam("grant_expires_at", value=grant_expires_at),
            updated_at=func.now(),
            last_used_at=func.now(),
        )
        .returning(youtube_grants)
    )
    return _sql_statement(statement)


def load_youtube_grant_by_id_sql(*, grant_id: str) -> SqlStatement:
    statement = select(youtube_grants).where(youtube_grants.c.id == bindparam("grant_id", value=grant_id)).limit(1)
    return _sql_statement(statement)


def load_active_youtube_grant_sql(*, workspace_id: str, user_id: str) -> SqlStatement:
    statement = (
        select(youtube_grants)
        .where(
            youtube_grants.c.workspace_id == bindparam("workspace_id", value=workspace_id),
            youtube_grants.c.user_id == bindparam("user_id", value=user_id),
            youtube_grants.c.status == literal("active"),
            youtube_grants.c.revoked_at.is_(None),
            or_(youtube_grants.c.expires_at.is_(None), youtube_grants.c.expires_at > func.now()),
        )
        .order_by(youtube_grants.c.updated_at.desc())
        .limit(1)
    )
    return _sql_statement(statement)


def mark_youtube_grant_used_sql(*, grant_id: str) -> SqlStatement:
    statement = (
        update(youtube_grants)
        .where(youtube_grants.c.id == bindparam("grant_id", value=grant_id))
        .values(last_used_at=func.now(), updated_at=func.now())
    )
    return _sql_statement(statement)


def revoke_youtube_grant_sql(*, grant_id: str, workspace_id: str, user_id: str, now: datetime) -> SqlStatement:
    statement = (
        update(youtube_grants)
        .where(
            youtube_grants.c.id == bindparam("grant_id", value=grant_id),
            youtube_grants.c.workspace_id == bindparam("workspace_id", value=workspace_id),
            youtube_grants.c.user_id == bindparam("user_id", value=user_id),
        )
        .values(status=literal("revoked"), revoked_at=bindparam("revoked_at", value=now), updated_at=func.now())
    )
    return _sql_statement(statement)


def expire_youtube_grant_sql(*, grant_id: str, status: str = "expired") -> SqlStatement:
    statement = (
        update(youtube_grants)
        .where(youtube_grants.c.id == bindparam("grant_id", value=grant_id))
        .values(status=bindparam("status", value=status), expires_at=func.now(), updated_at=func.now())
    )
    return _sql_statement(statement)


def load_youtube_grant_by_id(connection: Any, *, grant_id: str) -> Mapping[str, Any] | None:
    statement = load_youtube_grant_by_id_sql(grant_id=grant_id)
    rows = _rows_from_result(connection.execute(statement.sql, statement.params))
    return rows[0] if rows else None


def load_active_youtube_grant(connection: Any, *, workspace_id: str, user_id: str) -> Mapping[str, Any] | None:
    statement = load_active_youtube_grant_sql(workspace_id=workspace_id, user_id=user_id)
    rows = _rows_from_result(connection.execute(statement.sql, statement.params))
    return rows[0] if rows else None


def _token_metadata(
    token: Mapping[str, Any],
    *,
    connected_at: datetime,
    redirect_uri: str,
    previous_metadata: Mapping[str, Any],
    grant_id: str,
    token_encryption_key: str,
) -> dict[str, Any]:
    previous_access_token = _metadata_secret(
        previous_metadata,
        "access_token",
        token_encryption_key=token_encryption_key,
        aad=_secret_aad(grant_id, "access_token"),
    )
    access_token = _optional_text(token.get("access_token")) or previous_access_token
    if access_token is None:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_exchange_failed",
            message="Google did not return a YouTube access token.",
            status_code=502,
        )
    expires_at = _token_expires_at(token) or _metadata_datetime(previous_metadata, "access_token_expires_at")
    previous_refresh_token = _metadata_secret(
        previous_metadata,
        "refresh_token",
        token_encryption_key=token_encryption_key,
        aad=_secret_aad(grant_id, "refresh_token"),
    )
    refresh = _optional_text(token.get("refresh_token")) or previous_refresh_token
    metadata = {
        "purpose": "youtube_subscription_discovery",
        "oauth_provider": "google",
        "token_encryption": YOUTUBE_TOKEN_ENCRYPTION_ALGORITHM,
        "scope": _optional_text(token.get("scope")) or YOUTUBE_READONLY_SCOPE,
        "token_type": _optional_text(token.get("token_type")) or "Bearer",
        "access_token_ciphertext": _encrypt_metadata_secret(
            access_token,
            token_encryption_key=token_encryption_key,
            aad=_secret_aad(grant_id, "access_token"),
        ),
        "refresh_token_ciphertext": (
            _encrypt_metadata_secret(
                refresh,
                token_encryption_key=token_encryption_key,
                aad=_secret_aad(grant_id, "refresh_token"),
            )
            if refresh
            else None
        ),
        "access_token_expires_at": expires_at.isoformat() if expires_at is not None else None,
        "connected_at": connected_at.isoformat(),
        "redirect_uri": redirect_uri or _optional_text(previous_metadata.get("redirect_uri")),
    }
    return {key: value for key, value in metadata.items() if value is not None}


def _grant_expires_at(metadata: Mapping[str, Any]) -> datetime | None:
    if _optional_text(metadata.get("refresh_token_ciphertext")) or _optional_text(metadata.get("refresh_token")):
        return None
    return _metadata_datetime(metadata, "access_token_expires_at")


def _token_expires_at(token: Mapping[str, Any]) -> datetime | None:
    raw = token.get("expires_at")
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    if isinstance(raw, str):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except ValueError:
            return _row_datetime(raw)
    expires_in = token.get("expires_in")
    if isinstance(expires_in, (int, float)):
        return datetime.fromtimestamp(time.time() + float(expires_in) - 60, tz=timezone.utc)
    return None


def _expire_grant(connection: Any, *, grant: Mapping[str, Any], status: str = "expired") -> None:
    statement = expire_youtube_grant_sql(grant_id=str(grant["id"]), status=status)
    connection.execute(statement.sql, statement.params)


def _public_grant_json(row: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _json_object(row.get("metadata_json"))
    return {
        "grant_id": row.get("id"),
        "status": row.get("status"),
        "scopes": _row_scopes(row.get("scopes")),
        "created_at": _datetime_json(row.get("created_at")),
        "updated_at": _datetime_json(row.get("updated_at")),
        "last_used_at": _datetime_json(row.get("last_used_at")),
        "expires_at": _datetime_json(row.get("expires_at")),
        "connected_at": _datetime_json(metadata.get("connected_at")),
        "access_token_expires_at": _datetime_json(metadata.get("access_token_expires_at")),
    }


def _channel_json(channel: Any) -> dict[str, Any]:
    return {
        "channel_id": channel.channel_id,
        "title": channel.title,
        "source_url": channel.source_url,
        "selected": channel.selected,
    }


def _validate_dashboard_redirect_uri(value: str) -> str:
    uri = value.strip()
    try:
        parsed = urlsplit(uri)
    except ValueError as exc:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_redirect_uri_invalid",
            message="YouTube redirect URI is invalid.",
            status_code=400,
        ) from exc
    host = parsed.hostname or ""
    local = parsed.scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}
    if not (parsed.scheme == "https" or local) or parsed.fragment:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_redirect_uri_invalid",
            message="YouTube redirect URI must be HTTPS, except localhost development callbacks.",
            status_code=400,
        )
    if parsed.path != "/dashboard/youtube/callback":
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_redirect_uri_invalid",
            message="YouTube redirect URI must point to the dashboard callback.",
            status_code=400,
        )
    return uri


def _sql_statement(statement: Any) -> SqlStatement:
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def _json_param(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _encrypt_metadata_secret(value: str, *, token_encryption_key: str, aad: bytes) -> str:
    secret = _optional_text(value)
    if secret is None:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_token_secret_invalid",
            message="OAuth token material was empty.",
            status_code=500,
        )
    nonce = os.urandom(12)
    ciphertext = AESGCM(_aesgcm_key(token_encryption_key)).encrypt(nonce, secret.encode("utf-8"), aad)
    return "v1:" + _base64url(nonce + ciphertext)


def _metadata_secret(
    metadata: Mapping[str, Any],
    key: str,
    *,
    token_encryption_key: str,
    aad: bytes,
) -> str | None:
    ciphertext = _optional_text(metadata.get(f"{key}_ciphertext"))
    if ciphertext:
        return _decrypt_metadata_secret(ciphertext, token_encryption_key=token_encryption_key, aad=aad)
    return _optional_text(metadata.get(key))


def _decrypt_metadata_secret(ciphertext: str, *, token_encryption_key: str, aad: bytes) -> str:
    payload = ciphertext.removeprefix("v1:")
    try:
        raw = _base64url_decode(payload)
        nonce, encrypted = raw[:12], raw[12:]
        decrypted = AESGCM(_aesgcm_key(token_encryption_key)).decrypt(nonce, encrypted, aad)
    except Exception as exc:
        raise HostedYouTubeOAuthError(
            code="youtube_oauth_token_secret_invalid",
            message="OAuth token material could not be decrypted.",
            status_code=500,
        ) from exc
    return decrypted.decode("utf-8")


def _aesgcm_key(value: str) -> bytes:
    return hashlib.sha256(value.strip().encode("utf-8")).digest()


def _secret_aad(grant_id: str, key: str) -> bytes:
    return f"youtube_grants:{grant_id}:{key}".encode("utf-8")


def _rows_from_result(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, list):
        return [dict(row) for row in result]
    if hasattr(result, "mappings"):
        return [dict(row) for row in result.mappings().all()]
    if hasattr(result, "fetchall"):
        return [dict(row) for row in result.fetchall()]
    try:
        return [dict(row) for row in result]
    except TypeError:
        return []


def _row_scopes(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _row_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _metadata_datetime(metadata: Mapping[str, Any], key: str) -> datetime | None:
    return _row_datetime(metadata.get(key))


def _datetime_json(value: Any) -> str | None:
    parsed = _row_datetime(value)
    return parsed.isoformat() if parsed is not None else None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _base64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))


__all__ = [
    "HostedYouTubeOAuthError",
    "YouTubeOAuthSettings",
    "complete_youtube_authorization",
    "import_youtube_subscription_channels",
    "list_youtube_subscription_channels",
    "revoke_youtube_connection",
    "start_youtube_authorization",
    "youtube_connection_status",
    "youtube_oauth_settings_from_env",
]
