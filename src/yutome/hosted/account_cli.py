from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from psycopg.types.json import Jsonb
from sqlalchemy import bindparam, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert

from yutome import contract
from yutome.hosted.account import AccountSessionError
from yutome.hosted.repositories import SqlStatement
from yutome.hosted.schema import account_grants
from yutome.hosted.sqlalchemy_core import compile_postgres_statement


DEFAULT_CLI_AUDIENCE = "yutome:hosted-cli"
DEFAULT_CLI_CLIENT_ID = "yutome-cli"
CLI_AUTH_CODE_TTL_SECONDS = 5 * 60
CLI_TOKEN_TTL_SECONDS = 90 * 24 * 60 * 60
CLI_ACCOUNT_READ_SCOPE = "yutome.account.read"
CLI_SOURCE_WRITE_SCOPE = contract.SOURCE_WRITE_SCOPE
CLI_JOB_WRITE_SCOPE = contract.JOB_WRITE_SCOPE
CLI_LIBRARY_READ_SCOPE = "yutome.library.read"
DEFAULT_CLI_SCOPES: tuple[str, ...] = (
    CLI_ACCOUNT_READ_SCOPE,
    CLI_SOURCE_WRITE_SCOPE,
    CLI_JOB_WRITE_SCOPE,
    CLI_LIBRARY_READ_SCOPE,
)


@dataclass(frozen=True)
class CliTokenClaims:
    user_id: str
    workspace_id: str
    grant_id: str
    scopes: tuple[str, ...]
    audience: str
    client_id: str | None
    install_id: str | None
    token_version: int
    issued_at: datetime
    expires_at: datetime
    replay_id: str | None = None


def new_authorization_code() -> str:
    return secrets.token_urlsafe(32)


def new_code_verifier() -> str:
    return secrets.token_urlsafe(48)


def new_install_id() -> str:
    return "cli_" + secrets.token_urlsafe(18)


def code_hash(code: str) -> str:
    if not code or not code.strip():
        raise ValueError("authorization code is required")
    return "sha256:" + hashlib.sha256(code.strip().encode("utf-8")).hexdigest()


def code_challenge_for_verifier(verifier: str) -> str:
    if not verifier or not verifier.strip():
        raise ValueError("code verifier is required")
    digest = hashlib.sha256(verifier.strip().encode("utf-8")).digest()
    return _base64url(digest)


def stable_cli_grant_id(code_hash_value: str) -> str:
    digest = hashlib.sha256(code_hash_value.encode("utf-8")).hexdigest()[:24]
    return f"grant_{digest}"


def create_pending_cli_grant_sql(
    *,
    code_hash_value: str,
    code_challenge: str,
    redirect_uri: str,
    user_id: str,
    workspace_id: str,
    scopes: Sequence[str] = DEFAULT_CLI_SCOPES,
    client_id: str = DEFAULT_CLI_CLIENT_ID,
    audience: str = DEFAULT_CLI_AUDIENCE,
    expires_at: datetime,
    state: str | None = None,
) -> SqlStatement:
    grant_id = stable_cli_grant_id(code_hash_value)
    metadata = {
        "purpose": "cli_authorization_code",
        "code_hash": code_hash_value,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    statement = (
        insert(account_grants)
        .values(
            id=bindparam("id", value=grant_id),
            user_id=bindparam("user_id", value=user_id),
            workspace_id=bindparam("workspace_id", value=workspace_id),
            kind=literal("cli_install"),
            scopes=bindparam("scopes", value=list(dict.fromkeys(scopes))),
            status=literal("pending"),
            audience=bindparam("audience", value=audience),
            client_id=bindparam("client_id", value=client_id),
            install_id=bindparam("install_id", value=code_hash_value),
            token_version=literal(1),
            metadata_json=bindparam(
                "metadata_json",
                value=Jsonb({key: value for key, value in metadata.items() if value is not None}),
            ),
            expires_at=bindparam("expires_at", value=expires_at),
            created_at=func.now(),
        )
        .returning(account_grants)
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def load_cli_grant_by_code_hash_sql(*, code_hash_value: str) -> SqlStatement:
    statement = (
        select(account_grants)
        .where(
            account_grants.c.kind == literal("cli_install"),
            account_grants.c.install_id == bindparam("code_hash", value=code_hash_value),
        )
        .limit(1)
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def activate_pending_cli_grant_sql(
    *,
    grant_id: str,
    code_hash_value: str,
    install_id: str,
    token_expires_at: datetime,
    metadata: Mapping[str, Any] | None = None,
) -> SqlStatement:
    merged_metadata = {"purpose": "cli_install", "authorized_at": datetime.now(timezone.utc).isoformat()}
    merged_metadata.update(dict(metadata or {}))
    statement = (
        update(account_grants)
        .where(
            account_grants.c.id == bindparam("grant_id", value=grant_id),
            account_grants.c.kind == literal("cli_install"),
            account_grants.c.status == literal("pending"),
            account_grants.c.install_id == bindparam("code_hash", value=code_hash_value),
        )
        .values(
            status=literal("active"),
            install_id=bindparam("install_id", value=install_id),
            token_version=literal(1),
            expires_at=bindparam("token_expires_at", value=token_expires_at),
            last_used_at=func.now(),
            metadata_json=account_grants.c.metadata_json.op("||")(
                bindparam("metadata_json", value=Jsonb(merged_metadata))
            ),
        )
        .returning(account_grants)
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def load_cli_grant_by_id_sql(*, grant_id: str) -> SqlStatement:
    statement = (
        select(account_grants)
        .where(
            account_grants.c.id == bindparam("grant_id", value=grant_id),
            account_grants.c.kind == literal("cli_install"),
        )
        .limit(1)
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def mark_cli_grant_used_sql(*, grant_id: str) -> SqlStatement:
    statement = (
        update(account_grants)
        .where(
            account_grants.c.id == bindparam("grant_id", value=grant_id),
            account_grants.c.kind == literal("cli_install"),
        )
        .values(last_used_at=func.now())
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def sign_cli_token(
    *,
    user_id: str,
    workspace_id: str,
    grant_id: str,
    scopes: Sequence[str],
    secret: str,
    expires_at: datetime,
    issued_at: datetime | None = None,
    audience: str = DEFAULT_CLI_AUDIENCE,
    client_id: str | None = DEFAULT_CLI_CLIENT_ID,
    install_id: str | None = None,
    token_version: int = 1,
    replay_id: str | None = None,
) -> str:
    if not secret.strip():
        raise ValueError("CLI token signing secret is required.")
    issued_at = issued_at or datetime.now(timezone.utc)
    replay_id = replay_id or secrets.token_urlsafe(24)
    payload: dict[str, Any] = {
        "aud": audience,
        "exp": int(expires_at.timestamp()),
        "iat": int(issued_at.timestamp()),
        "jti": replay_id,
        "user_id": user_id,
        "workspace_id": workspace_id,
        "grant_id": grant_id,
        "scopes": list(dict.fromkeys(scopes)),
        "token_version": int(token_version),
    }
    if client_id:
        payload["client_id"] = client_id
    if install_id:
        payload["install_id"] = install_id
    encoded_payload = _base64url(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signed = f"v1.{encoded_payload}"
    signature = hmac.new(secret.encode("utf-8"), signed.encode("utf-8"), hashlib.sha256).digest()
    return f"{signed}.{_base64url(signature)}"


def verify_cli_token(
    token: str,
    *,
    secret: str,
    audience: str = DEFAULT_CLI_AUDIENCE,
    now: datetime | None = None,
    clock_skew_seconds: int = 60,
) -> CliTokenClaims:
    if not secret or not secret.strip():
        raise AccountSessionError(
            "cli_token_signing_unconfigured", "CLI token signing secret is required.", status_code=503
        )
    parts = token.split(".") if token else []
    if len(parts) != 3 or parts[0] != "v1" or not parts[1] or not parts[2]:
        raise AccountSessionError("cli_token_malformed", "CLI token was not a signed v1 token.")
    signed = f"{parts[0]}.{parts[1]}"
    expected = hmac.new(secret.encode("utf-8"), signed.encode("utf-8"), hashlib.sha256).digest()
    try:
        actual = _base64url_decode(parts[2])
    except (ValueError, TypeError) as exc:
        raise AccountSessionError("cli_token_malformed", "CLI token signature was not valid base64url.") from exc
    if not hmac.compare_digest(actual, expected):
        raise AccountSessionError("cli_token_invalid", "CLI token signature was invalid.")
    try:
        payload = json.loads(_base64url_decode(parts[1]).decode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise AccountSessionError("cli_token_malformed", "CLI token payload was not valid JSON.") from exc
    if not isinstance(payload, Mapping):
        raise AccountSessionError("cli_token_malformed", "CLI token payload was not an object.")
    if payload.get("aud") != audience:
        raise AccountSessionError("cli_token_audience_mismatch", "CLI token audience is not accepted.")

    now = now or datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    exp = _int_claim(payload, "exp")
    iat = _int_claim(payload, "iat")
    if exp is None or iat is None:
        raise AccountSessionError("cli_token_malformed", "CLI token is missing exp/iat.")
    if now_ts > exp + clock_skew_seconds:
        raise AccountSessionError("cli_token_expired", "CLI token has expired.")
    if iat > now_ts + clock_skew_seconds:
        raise AccountSessionError("cli_token_not_yet_valid", "CLI token was issued in the future.")

    user_id = _str_claim(payload, "user_id")
    workspace_id = _str_claim(payload, "workspace_id")
    grant_id = _str_claim(payload, "grant_id")
    token_version = _int_claim(payload, "token_version")
    raw_scopes = payload.get("scopes")
    scopes = tuple(str(scope) for scope in raw_scopes) if isinstance(raw_scopes, list) else ()
    if not user_id or not workspace_id or not grant_id or token_version is None:
        raise AccountSessionError("cli_token_malformed", "CLI token is missing grant identity.")
    return CliTokenClaims(
        user_id=user_id,
        workspace_id=workspace_id,
        grant_id=grant_id,
        scopes=scopes,
        audience=audience,
        client_id=_str_claim(payload, "client_id"),
        install_id=_str_claim(payload, "install_id"),
        token_version=token_version,
        issued_at=datetime.fromtimestamp(iat, tz=timezone.utc),
        expires_at=datetime.fromtimestamp(exp, tz=timezone.utc),
        replay_id=_str_claim(payload, "jti"),
    )


def _int_claim(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _str_claim(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
