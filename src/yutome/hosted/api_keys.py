from __future__ import annotations

import hashlib
import secrets
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb
from sqlalchemy import bindparam, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert

from yutome import contract
from yutome.hosted.repositories import SqlStatement
from yutome.hosted.schema import api_keys
from yutome.hosted.sqlalchemy_core import compile_postgres_statement


API_KEY_PREFIX = "yk_"
API_KEY_BYTES = 32
DEFAULT_API_KEY_SCOPES: tuple[str, ...] = (contract.AUTH_SCOPE,)


def new_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(API_KEY_BYTES)


def api_key_hash(raw: str) -> str:
    if not raw or not raw.strip():
        raise ValueError("API key must not be empty.")
    return "sha256:" + hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()


def new_api_key_id() -> str:
    return "apikey_" + secrets.token_hex(12)


def insert_api_key_sql(
    *,
    key_id: str,
    key_hash: str,
    workspace_id: str,
    user_id: str,
    scopes: Sequence[str],
    name: str | None,
    expires_at: datetime | None,
) -> SqlStatement:
    metadata: dict[str, Any] = {"purpose": "personal_api_key"}
    statement = (
        insert(api_keys)
        .values(
            id=bindparam("id", value=key_id),
            key_hash=bindparam("key_hash", value=key_hash),
            workspace_id=bindparam("workspace_id", value=workspace_id),
            user_id=bindparam("user_id", value=user_id),
            name=bindparam("name", value=name),
            scopes=bindparam("scopes", value=list(dict.fromkeys(scopes))),
            status=literal("active"),
            metadata_json=bindparam("metadata_json", value=Jsonb(metadata)),
            created_at=func.now(),
            expires_at=bindparam("expires_at", value=expires_at),
        )
        .returning(api_keys)
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def list_api_keys_sql(*, workspace_id: str) -> SqlStatement:
    statement = (
        select(
            api_keys.c.id,
            api_keys.c.name,
            api_keys.c.scopes,
            api_keys.c.status,
            api_keys.c.created_at,
            api_keys.c.last_used_at,
            api_keys.c.expires_at,
            api_keys.c.revoked_at,
        )
        .where(api_keys.c.workspace_id == bindparam("workspace_id", value=workspace_id))
        .order_by(api_keys.c.created_at.desc())
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def load_active_api_key_by_hash_sql(*, key_hash: str) -> SqlStatement:
    statement = (
        select(api_keys)
        .where(
            api_keys.c.key_hash == bindparam("key_hash", value=key_hash),
            api_keys.c.status == literal("active"),
            api_keys.c.revoked_at.is_(None),
        )
        .limit(1)
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def revoke_api_key_sql(*, key_id: str, workspace_id: str, now: datetime) -> SqlStatement:
    now_param = bindparam("now", value=now)
    statement = (
        update(api_keys)
        .where(
            api_keys.c.id == bindparam("key_id", value=key_id),
            api_keys.c.workspace_id == bindparam("workspace_id", value=workspace_id),
            api_keys.c.revoked_at.is_(None),
        )
        .values(status=literal("revoked"), revoked_at=now_param)
        .returning(api_keys.c.id)
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def mark_api_key_used_sql(*, key_id: str, now: datetime) -> SqlStatement:
    statement = (
        update(api_keys)
        .where(api_keys.c.id == bindparam("key_id", value=key_id))
        .values(last_used_at=bindparam("now", value=now))
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


__all__ = [
    "API_KEY_BYTES",
    "API_KEY_PREFIX",
    "DEFAULT_API_KEY_SCOPES",
    "api_key_hash",
    "insert_api_key_sql",
    "list_api_keys_sql",
    "load_active_api_key_by_hash_sql",
    "mark_api_key_used_sql",
    "new_api_key",
    "new_api_key_id",
    "revoke_api_key_sql",
]
