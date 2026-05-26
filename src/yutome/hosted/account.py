from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from yutome.hosted.migrations import HOSTED_DEFAULT_EMBEDDING_MODEL, HOSTED_VECTOR_BACKEND
from yutome.hosted.repositories import SqlStatement


STARTER_PRICE_BOOK_ID = "price_book_starter_v1"
STARTER_PRICE_BOOK_VERSION = "starter-v1"
STARTER_PLAN_KEY = "starter"

STARTER_ALLOWED_OPERATIONS: tuple[str, ...] = (
    "youtube.metadata_fetch",
    "youtube.transcript_fetch",
    "gemini.cleanup_transcript",
    "gemini.transcribe_media",
    "voyage.embed_documents",
    "voyage.embed_query",
    "webshare.proxy_fetch",
    "search_store.index_write",
    "search_store.lexical_query",
    "search_store.semantic_query",
    "search_store.hybrid_query",
    "search_store.list_read",
    "search_store.resource_read",
)
STARTER_INCLUDED_UNITS: dict[str, Any] = {
    "total_tokens": 250_000,
    "media_seconds": 3_600,
    "vectors": 10_000,
    "queries": 10_000,
    "candidate_limit": 100_000,
    "query_vector_dimensions": 10_240_000,
    "resource_reads": 10_000,
    "result_count": 100_000,
    "request_count": 1_000,
    "bytes": 1_073_741_824,
    "transcript_versions": 10_000,
    "chunks": 100_000,
    "embeddings": 100_000,
}
STARTER_HARD_LIMITS: dict[str, Any] = {
    "youtube.metadata_fetch": {"request_count": 500},
    "youtube.transcript_fetch": {"request_count": 500},
    "gemini.cleanup_transcript": {"total_tokens": 50_000},
    "gemini.transcribe_media": {"media_seconds": 1_800},
    "voyage.embed_documents": {"total_tokens": 100_000, "vectors": 5_000},
    "voyage.embed_query": {"total_tokens": 25_000, "vectors": 5_000},
    "webshare.proxy_fetch": {"request_count": 500, "bytes": 536_870_912},
    "search_store.index_write": {"transcript_versions": 1_000, "chunks": 10_000, "embeddings": 10_000},
    "search_store.lexical_query": {"queries": 5_000, "candidate_limit": 50_000},
    "search_store.semantic_query": {"queries": 5_000, "candidate_limit": 50_000, "query_vector_dimensions": 5_120_000},
    "search_store.hybrid_query": {"queries": 5_000, "candidate_limit": 50_000, "query_vector_dimensions": 5_120_000},
    "search_store.list_read": {"queries": 5_000, "candidate_limit": 50_000},
    "search_store.resource_read": {"queries": 5_000, "resource_reads": 5_000},
}

_CREDENTIAL_KEY_FRAGMENTS = ("api_key", "access_token", "refresh_token", "secret", "password", "credential")


class AccountModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountPrincipal(AccountModel):
    user_id: str
    workspace_id: str
    email: str
    normalized_email: str
    role: str = "owner"
    scopes: set[str] = Field(default_factory=set)

    @field_validator("normalized_email")
    @classmethod
    def _normalized_email_is_normalized(cls, value: str) -> str:
        normalized = normalize_email(value)
        if value != normalized:
            raise ValueError("normalized_email must already be normalized.")
        return value

    @field_validator("email")
    @classmethod
    def _email_is_valid(cls, value: str) -> str:
        return normalize_email(value)


class AccountSession(AccountModel):
    id: str
    user_id: str
    workspace_id: str
    session_hash: str
    status: str = "active"
    scopes: set[str] = Field(default_factory=set)
    audience: str | None = None
    client_id: str | None = None
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AccountBootstrapResult(AccountModel):
    principal: AccountPrincipal
    session: AccountSession | None = None
    entitlement_policy_id: str
    workspace_balance_workspace_id: str
    provider_allocation_ids: tuple[str, ...]
    service_allocation_ids: tuple[str, ...]


class AccountSqlConnection(Protocol):
    def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> Sequence[Mapping[str, Any]]:
        ...


@dataclass(frozen=True)
class AccountBootstrapInput:
    email: str
    name: str | None = None
    workspace_name: str | None = None
    session_token: str | None = None
    session_scopes: tuple[str, ...] = ()
    session_audience: str | None = None
    session_client_id: str | None = None
    session_expires_at: datetime | None = None

    @property
    def normalized_email(self) -> str:
        return normalize_email(self.email)

    @property
    def user_id(self) -> str:
        return deterministic_user_id(self.normalized_email)

    @property
    def workspace_id(self) -> str:
        return deterministic_personal_workspace_id(self.user_id)


def normalize_email(email: str) -> str:
    normalized = " ".join(email.strip().split()).lower()
    if not normalized or "@" not in normalized:
        raise ValueError("A valid email address is required.")
    return normalized


def deterministic_user_id(normalized_email: str) -> str:
    return _stable_id("usr", normalized_email)


def deterministic_personal_workspace_id(user_id: str) -> str:
    return _stable_id("ws", f"personal:{user_id}")


def deterministic_account_session_id(session_hash: str) -> str:
    return _stable_id("sess", session_hash)


def session_token_hash(session_token: str) -> str:
    if not session_token:
        raise ValueError("session_token must not be empty.")
    return "sha256:" + hashlib.sha256(session_token.encode("utf-8")).hexdigest()


def bootstrap_hosted_account(
    connection: AccountSqlConnection,
    account: AccountBootstrapInput,
) -> AccountBootstrapResult:
    """Create or return the hosted account bootstrap rows for one user.

    The account core owns identity, membership, sessions, entitlements, and
    allocation references. Provider credentials are intentionally absent from
    these contracts and SQL params.
    """

    statements = account_bootstrap_sql(account)
    rows: dict[str, Mapping[str, Any]] = {}
    for key, statement in statements:
        result = connection.execute(statement.sql, statement.params)
        if result:
            rows[key] = result[0]

    normalized_email = account.normalized_email
    user_id = str(rows.get("user", {}).get("id") or account.user_id)
    workspace_id = str(rows.get("workspace", {}).get("id") or account.workspace_id)
    policy_id = str(rows.get("entitlement_policy", {}).get("id") or starter_entitlement_policy_id(workspace_id))
    provider_ids = tuple(
        starter_provider_allocation_id(workspace_id, provider, operation)
        for provider, operation in STARTER_PROVIDER_OPERATIONS
    )
    service_ids = tuple(
        starter_service_allocation_id(workspace_id, service, operation)
        for service, operation in STARTER_SERVICE_OPERATIONS
    )

    session = None
    if account.session_token is not None:
        session_hash = session_token_hash(account.session_token)
        session = AccountSession(
            id=str(rows.get("account_session", {}).get("id") or deterministic_account_session_id(session_hash)),
            user_id=user_id,
            workspace_id=workspace_id,
            session_hash=session_hash,
            scopes=set(account.session_scopes),
            audience=account.session_audience,
            client_id=account.session_client_id,
            expires_at=account.session_expires_at,
        )

    return AccountBootstrapResult(
        principal=AccountPrincipal(
            user_id=user_id,
            workspace_id=workspace_id,
            email=normalized_email,
            normalized_email=normalized_email,
            role="owner",
            scopes=set(account.session_scopes),
        ),
        session=session,
        entitlement_policy_id=policy_id,
        workspace_balance_workspace_id=workspace_id,
        provider_allocation_ids=provider_ids,
        service_allocation_ids=service_ids,
    )


STARTER_PROVIDER_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("youtube", "metadata_fetch"),
    ("youtube", "transcript_fetch"),
    ("gemini", "cleanup_transcript"),
    ("gemini", "transcribe_media"),
    ("voyage", "embed_documents"),
    ("voyage", "embed_query"),
    ("webshare", "proxy_fetch"),
)

STARTER_SERVICE_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("search_store", "index_write"),
    ("search_store", "lexical_query"),
    ("search_store", "semantic_query"),
    ("search_store", "hybrid_query"),
    ("search_store", "list_read"),
    ("search_store", "resource_read"),
)


def account_bootstrap_sql(account: AccountBootstrapInput) -> list[tuple[str, SqlStatement]]:
    statements: list[tuple[str, SqlStatement]] = [
        ("user", upsert_user_sql(account)),
        ("workspace", upsert_personal_workspace_sql(account)),
        ("workspace_member", upsert_workspace_member_sql(account)),
        ("starter_price_book", upsert_starter_price_book_sql()),
        ("entitlement_policy", upsert_starter_entitlement_policy_sql(account.workspace_id)),
        ("workspace_balance", upsert_starter_workspace_balance_sql(account.workspace_id)),
    ]
    statements.extend(
        (
            f"provider_allocation_{provider}_{operation}",
            upsert_starter_provider_allocation_sql(account.workspace_id, provider=provider, operation=operation),
        )
        for provider, operation in STARTER_PROVIDER_OPERATIONS
    )
    statements.extend(
        (
            f"service_allocation_{service}_{operation}",
            upsert_starter_service_allocation_sql(account.workspace_id, service=service, operation=operation),
        )
        for service, operation in STARTER_SERVICE_OPERATIONS
    )
    if account.session_token is not None:
        statements.append(("account_session", upsert_account_session_sql(account)))
    return statements


def upsert_user_sql(account: AccountBootstrapInput) -> SqlStatement:
    return SqlStatement(
        sql="""
INSERT INTO users (
    id,
    email,
    normalized_email,
    name,
    status,
    created_at
)
VALUES (
    %(id)s,
    %(email)s,
    %(normalized_email)s,
    %(name)s,
    'active',
    now()
)
ON CONFLICT (normalized_email) DO UPDATE
SET email = users.email,
    name = COALESCE(users.name, EXCLUDED.name),
    status = CASE WHEN users.status = 'disabled' THEN users.status ELSE 'active' END
RETURNING *;
""".strip(),
        params={
            "id": account.user_id,
            "email": account.email.strip(),
            "normalized_email": account.normalized_email,
            "name": account.name,
        },
    )


def upsert_personal_workspace_sql(account: AccountBootstrapInput) -> SqlStatement:
    return SqlStatement(
        sql="""
INSERT INTO workspaces (
    id,
    owner_user_id,
    name,
    status,
    created_at
)
VALUES (
    %(id)s,
    %(owner_user_id)s,
    %(name)s,
    'active',
    now()
)
ON CONFLICT (id) DO UPDATE
SET owner_user_id = workspaces.owner_user_id,
    name = workspaces.name,
    status = CASE WHEN workspaces.status = 'disabled' THEN workspaces.status ELSE 'active' END
RETURNING *;
""".strip(),
        params={
            "id": account.workspace_id,
            "owner_user_id": account.user_id,
            "name": account.workspace_name or _default_workspace_name(account.normalized_email),
        },
    )


def upsert_workspace_member_sql(account: AccountBootstrapInput) -> SqlStatement:
    return SqlStatement(
        sql="""
INSERT INTO workspace_members (
    workspace_id,
    user_id,
    role,
    status,
    created_at
)
VALUES (
    %(workspace_id)s,
    %(user_id)s,
    'owner',
    'active',
    now()
)
ON CONFLICT (workspace_id, user_id) DO UPDATE
SET role = 'owner',
    status = CASE WHEN workspace_members.status = 'disabled' THEN workspace_members.status ELSE 'active' END
RETURNING *;
""".strip(),
        params={"workspace_id": account.workspace_id, "user_id": account.user_id},
    )


def upsert_starter_price_book_sql() -> SqlStatement:
    return SqlStatement(
        sql="""
INSERT INTO price_books (
    id,
    version,
    currency,
    products_jsonb,
    unit_mapping_jsonb,
    status,
    metadata_json,
    created_at
)
VALUES (
    %(id)s,
    %(version)s,
    'usd',
    %(products_jsonb)s::jsonb,
    %(unit_mapping_jsonb)s::jsonb,
    'active',
    %(metadata_json)s::jsonb,
    now()
)
ON CONFLICT (version) DO UPDATE
SET status = 'active',
    metadata_json = EXCLUDED.metadata_json,
    updated_at = now()
RETURNING *;
""".strip(),
        params={
            "id": STARTER_PRICE_BOOK_ID,
            "version": STARTER_PRICE_BOOK_VERSION,
            "products_jsonb": _json_param([{"plan_key": STARTER_PLAN_KEY, "label": "Starter"}]),
            "unit_mapping_jsonb": _json_param({"source": "account_bootstrap"}),
            "metadata_json": _json_param({"managed_by": "yutome_account_core"}),
        },
    )


def upsert_starter_entitlement_policy_sql(workspace_id: str) -> SqlStatement:
    return SqlStatement(
        sql="""
INSERT INTO entitlement_policies (
    id,
    workspace_id,
    plan_key,
    price_book_id,
    allowed_operations,
    included_units_jsonb,
    hard_limits_jsonb,
    soft_limits_jsonb,
    grace_policy_jsonb,
    status,
    metadata_json,
    created_at
)
VALUES (
    %(id)s,
    %(workspace_id)s,
    %(plan_key)s,
    %(price_book_id)s,
    %(allowed_operations)s,
    %(included_units_jsonb)s::jsonb,
    %(hard_limits_jsonb)s::jsonb,
    %(soft_limits_jsonb)s::jsonb,
    '{}'::jsonb,
    'active',
    %(metadata_json)s::jsonb,
    now()
)
ON CONFLICT (workspace_id, plan_key, price_book_id) DO UPDATE
SET allowed_operations = EXCLUDED.allowed_operations,
    included_units_jsonb = EXCLUDED.included_units_jsonb,
    hard_limits_jsonb = EXCLUDED.hard_limits_jsonb,
    metadata_json = EXCLUDED.metadata_json,
    updated_at = now()
RETURNING *;
""".strip(),
        params={
            "id": starter_entitlement_policy_id(workspace_id),
            "workspace_id": workspace_id,
            "plan_key": STARTER_PLAN_KEY,
            "price_book_id": STARTER_PRICE_BOOK_ID,
            "allowed_operations": list(STARTER_ALLOWED_OPERATIONS),
            "included_units_jsonb": _json_param(STARTER_INCLUDED_UNITS),
            "hard_limits_jsonb": _json_param(STARTER_HARD_LIMITS),
            "soft_limits_jsonb": _json_param({}),
            "metadata_json": _json_param({"source": "account_bootstrap"}),
        },
    )


def upsert_starter_workspace_balance_sql(workspace_id: str) -> SqlStatement:
    return SqlStatement(
        sql="""
INSERT INTO workspace_balances (
    workspace_id,
    entitlement_policy_id,
    period_start_at,
    period_end_at,
    used_units_jsonb,
    reserved_units_jsonb,
    remaining_units_jsonb,
    unlimited_units,
    metadata_json,
    updated_at
)
VALUES (
    %(workspace_id)s,
    %(entitlement_policy_id)s,
    date_trunc('month', now()),
    date_trunc('month', now()) + interval '1 month',
    '{}'::jsonb,
    '{}'::jsonb,
    %(remaining_units_jsonb)s::jsonb,
    ARRAY[]::text[],
    %(metadata_json)s::jsonb,
    now()
)
ON CONFLICT (workspace_id) DO UPDATE
SET entitlement_policy_id = EXCLUDED.entitlement_policy_id,
    remaining_units_jsonb = workspace_balances.remaining_units_jsonb,
    metadata_json = workspace_balances.metadata_json,
    updated_at = now()
RETURNING *;
""".strip(),
        params={
            "workspace_id": workspace_id,
            "entitlement_policy_id": starter_entitlement_policy_id(workspace_id),
            "remaining_units_jsonb": _json_param(STARTER_INCLUDED_UNITS),
            "metadata_json": _json_param({"source": "account_bootstrap"}),
        },
    )


def upsert_starter_provider_allocation_sql(workspace_id: str, *, provider: str, operation: str) -> SqlStatement:
    model_or_plan = HOSTED_DEFAULT_EMBEDDING_MODEL if provider == "voyage" else None
    external_allocation_id = f"webshare_subuser_pending:{workspace_id}" if provider == "webshare" else None
    return SqlStatement(
        sql="""
INSERT INTO provider_allocations (
    id,
    workspace_id,
    provider,
    operation,
    mode,
    status,
    model_or_plan,
    external_allocation_id,
    metadata_json,
    created_at
)
VALUES (
    %(id)s,
    %(workspace_id)s,
    %(provider)s,
    %(operation)s,
    'hosted',
    'active',
    %(model_or_plan)s,
    %(external_allocation_id)s,
    %(metadata_json)s::jsonb,
    now()
)
ON CONFLICT (id) DO UPDATE
SET status = CASE WHEN provider_allocations.status = 'disabled' THEN provider_allocations.status ELSE 'active' END,
    model_or_plan = EXCLUDED.model_or_plan,
    metadata_json = EXCLUDED.metadata_json
RETURNING *;
""".strip(),
        params={
            "id": starter_provider_allocation_id(workspace_id, provider, operation),
            "workspace_id": workspace_id,
            "provider": provider,
            "operation": operation,
            "model_or_plan": model_or_plan,
            "external_allocation_id": external_allocation_id,
            "metadata_json": _json_param({"source": "account_bootstrap", "allocation_mode": "hosted"}),
        },
    )


def upsert_starter_service_allocation_sql(
    workspace_id: str,
    *,
    service: str = "search_store",
    operation: str = "index_write",
) -> SqlStatement:
    if service != "search_store":
        raise ValueError("starter account bootstrap only supports search_store service allocations")
    return SqlStatement(
        sql="""
INSERT INTO service_allocations (
    id,
    workspace_id,
    service,
    operation,
    mode,
    status,
    backend,
    index_profile_ref,
    metadata_json,
    created_at
)
VALUES (
    %(id)s,
    %(workspace_id)s,
    %(service)s,
    %(operation)s,
    'service_internal',
    'active',
    %(backend)s,
    %(index_profile_ref)s,
    %(metadata_json)s::jsonb,
    now()
)
ON CONFLICT (id) DO UPDATE
SET status = CASE WHEN service_allocations.status = 'disabled' THEN service_allocations.status ELSE 'active' END,
    backend = EXCLUDED.backend,
    index_profile_ref = EXCLUDED.index_profile_ref,
    metadata_json = EXCLUDED.metadata_json
RETURNING *;
""".strip(),
        params={
            "id": starter_service_allocation_id(workspace_id, service, operation),
            "workspace_id": workspace_id,
            "service": service,
            "operation": operation,
            "backend": HOSTED_VECTOR_BACKEND,
            "index_profile_ref": None,
            "metadata_json": _json_param({"source": "account_bootstrap"}),
        },
    )


def upsert_account_session_sql(account: AccountBootstrapInput) -> SqlStatement:
    if account.session_token is None:
        raise ValueError("session_token is required to persist an account session.")
    token_hash = session_token_hash(account.session_token)
    return SqlStatement(
        sql="""
INSERT INTO account_sessions (
    id,
    user_id,
    workspace_id,
    session_hash,
    status,
    scopes,
    audience,
    client_id,
    metadata_json,
    created_at,
    expires_at
)
VALUES (
    %(id)s,
    %(user_id)s,
    %(workspace_id)s,
    %(session_hash)s,
    'active',
    %(scopes)s,
    %(audience)s,
    %(client_id)s,
    %(metadata_json)s::jsonb,
    now(),
    %(expires_at)s
)
ON CONFLICT (session_hash) DO UPDATE
SET last_used_at = now(),
    scopes = EXCLUDED.scopes,
    audience = EXCLUDED.audience,
    client_id = EXCLUDED.client_id,
    expires_at = EXCLUDED.expires_at
RETURNING *;
""".strip(),
        params={
            "id": deterministic_account_session_id(token_hash),
            "user_id": account.user_id,
            "workspace_id": account.workspace_id,
            "session_hash": token_hash,
            "scopes": list(account.session_scopes),
            "audience": account.session_audience,
            "client_id": account.session_client_id,
            "expires_at": account.session_expires_at,
            "metadata_json": _json_param({"source": "account_bootstrap"}),
        },
    )


def starter_entitlement_policy_id(workspace_id: str) -> str:
    return _stable_id("ent", f"{workspace_id}:{STARTER_PLAN_KEY}:{STARTER_PRICE_BOOK_ID}")


def starter_provider_allocation_id(workspace_id: str, provider: str, operation: str) -> str:
    return _stable_id("alloc", f"{workspace_id}:{provider}:{operation}:hosted")


def starter_service_allocation_id(workspace_id: str, service: str, operation: str) -> str:
    return _stable_id("svc", f"{workspace_id}:{service}:{operation}:service_internal")


def sql_params_contain_provider_credentials(statement: SqlStatement) -> bool:
    return _contains_credential_shape(statement.params)


def _contains_credential_shape(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in _CREDENTIAL_KEY_FRAGMENTS):
                return True
            if _contains_credential_shape(item):
                return True
        return False
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return False
        return _contains_credential_shape(parsed)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return any(_contains_credential_shape(item) for item in value)
    return False


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _json_param(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _default_workspace_name(normalized_email: str) -> str:
    local_part = normalized_email.partition("@")[0]
    return f"{local_part}'s workspace"


__all__ = [
    "AccountBootstrapInput",
    "AccountBootstrapResult",
    "AccountPrincipal",
    "AccountSession",
    "STARTER_ALLOWED_OPERATIONS",
    "STARTER_HARD_LIMITS",
    "STARTER_INCLUDED_UNITS",
    "STARTER_PLAN_KEY",
    "STARTER_PRICE_BOOK_ID",
    "STARTER_PRICE_BOOK_VERSION",
    "STARTER_PROVIDER_OPERATIONS",
    "STARTER_SERVICE_OPERATIONS",
    "account_bootstrap_sql",
    "bootstrap_hosted_account",
    "deterministic_account_session_id",
    "deterministic_personal_workspace_id",
    "deterministic_user_id",
    "normalize_email",
    "session_token_hash",
    "sql_params_contain_provider_credentials",
    "starter_entitlement_policy_id",
    "starter_provider_allocation_id",
    "starter_service_allocation_id",
    "upsert_account_session_sql",
    "upsert_personal_workspace_sql",
    "upsert_starter_entitlement_policy_sql",
    "upsert_starter_price_book_sql",
    "upsert_starter_provider_allocation_sql",
    "upsert_starter_service_allocation_sql",
    "upsert_starter_workspace_balance_sql",
    "upsert_user_sql",
    "upsert_workspace_member_sql",
]
