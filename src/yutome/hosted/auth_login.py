"""Email magic-link sign-in tokens for the web dashboard.

A session is minted only after the user proves control of their email by
presenting a single-use token delivered out of band (see ``/account/login/start``
and ``/account/login/verify`` in ``http_api.py``). Only the token *hash* is
persisted; the raw token lives only in the emailed link. Consumption is atomic
(``UPDATE ... WHERE consumed_at IS NULL AND expires_at > now RETURNING ...``) so
a token can be redeemed at most once even under concurrent verifies.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime

from yutome.hosted.repositories import SqlStatement

LOGIN_TOKEN_BYTES = 32
DEFAULT_LOGIN_TOKEN_TTL_SECONDS = 15 * 60


def new_login_token() -> str:
    """A high-entropy, URL-safe raw login token. Never stored; only emailed."""
    return secrets.token_urlsafe(LOGIN_TOKEN_BYTES)


def login_token_hash(raw_token: str) -> str:
    if not raw_token:
        raise ValueError("login token must not be empty.")
    return "sha256:" + hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def new_login_token_id() -> str:
    return "login_" + secrets.token_hex(12)


def insert_login_token_sql(
    *,
    token_id: str,
    token_hash: str,
    normalized_email: str,
    name: str | None,
    workspace_name: str | None,
    redirect_path: str | None,
    expires_at: datetime,
    user_agent: str | None,
) -> SqlStatement:
    return SqlStatement(
        sql="""
INSERT INTO email_login_tokens (
    id, token_hash, normalized_email, name, workspace_name, redirect_path, user_agent, expires_at
)
VALUES (
    %(id)s, %(token_hash)s, %(normalized_email)s, %(name)s, %(workspace_name)s,
    %(redirect_path)s, %(user_agent)s, %(expires_at)s
);
""".strip(),
        params={
            "id": token_id,
            "token_hash": token_hash,
            "normalized_email": normalized_email,
            "name": name,
            "workspace_name": workspace_name,
            "redirect_path": redirect_path,
            "user_agent": user_agent,
            "expires_at": expires_at,
        },
    )


def consume_login_token_sql(*, token_hash: str, now: datetime) -> SqlStatement:
    """Atomically mark a token consumed iff it is unconsumed and unexpired.

    Returns the stored sign-up details for the matched row, or no rows when the
    token is unknown, already used, or expired.
    """
    return SqlStatement(
        sql="""
UPDATE email_login_tokens
SET consumed_at = %(now)s
WHERE token_hash = %(token_hash)s
  AND consumed_at IS NULL
  AND expires_at > %(now)s
RETURNING normalized_email, name, workspace_name, redirect_path;
""".strip(),
        params={"token_hash": token_hash, "now": now},
    )


__all__ = [
    "DEFAULT_LOGIN_TOKEN_TTL_SECONDS",
    "consume_login_token_sql",
    "insert_login_token_sql",
    "login_token_hash",
    "new_login_token",
    "new_login_token_id",
]
