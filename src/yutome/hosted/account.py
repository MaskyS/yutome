from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator
from psycopg.types.json import Jsonb
from sqlalchemy import bindparam, case, func, literal, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import insert

from yutome.hosted.migrations import HOSTED_DEFAULT_EMBEDDING_MODEL, HOSTED_VECTOR_BACKEND
from yutome.hosted.repositories import SqlStatement
from yutome.hosted.schema import (
    account_sessions,
    entitlement_policies,
    price_books,
    provider_allocations,
    service_allocations,
    users,
    workspace_balances,
    workspace_members,
    workspaces,
)
from yutome.hosted.sqlalchemy_core import compile_postgres_statement


STARTER_PRICE_BOOK_ID = "price_book_starter_v1"
STARTER_PRICE_BOOK_VERSION = "starter-v1"
STARTER_PLAN_KEY = "starter"
DEFAULT_ACCOUNT_SESSION_AUDIENCE = "yutome:hosted-oauth"

# The Personal plan ships a 14-day card-gated trial; there is no perpetual free tier. A new
# workspace bootstraps with subscription_status='trialing' and trial_ends_at 14 days out. The
# entitlement layer treats `trialing` exactly like `active`; once trial_ends_at passes with no
# active/trialing subscription the workspace becomes trial-expiry read-only.
TRIAL_PERIOD_DAYS = 14

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
# Personal-plan seat included allowance: ~25 indexed video-hours / month, the usage the $4
# flat seat covers before any metered overage. Easily tunable — change the video-hours target
# and re-derive the four billable units below; the non-billable quota units are generous
# operational ceilings (cost-visibility only) scaled to match.
#
# Derivation (grounded in the common index path: existing transcript -> Gemini cleanup ->
# Voyage embed; see indexing.py _token_estimate + chunking.py DEFAULT_TARGET_TOKENS=700):
#   25 video-hours ≈ 75 videos (~20 min each, ~3 videos/video-hour)
#   per video ≈ 5_700 total_tokens (cleanup read + embed) and ≈ 8 vectors (chunks)
#     total_tokens: 75 × 5_700 ≈ 427_500   -> 430_000
#     vectors:      75 × 8     = 600        -> 600
#   media_seconds covers the Gemini transcribe fallback (no caption track):
#     25 video-hours × 3_600 s/h = 90_000   -> 90_000
#   queries: a generous monthly read budget for the seat (not the dominant cost) -> 25_000
# Credit check via STRIPE_CREDIT_UNIT_WEIGHTS (1 credit ≈ 1 video-hour):
#   430_000×5e-5 + 600×7e-3 = 21.5 + 4.2 = 25.7 credits ≈ 25 indexed video-hours.
STARTER_INCLUDED_UNITS: dict[str, Any] = {
    # Billable units (collapse into the composite `credits` meter): the seat's real allowance.
    "total_tokens": 430_000,
    "vectors": 600,
    "media_seconds": 90_000,
    "queries": 25_000,
    # Non-billable quota / cost-visibility ceilings (never metered to Stripe).
    "candidate_limit": 100_000,
    "query_vector_dimensions": 10_240_000,
    "resource_reads": 25_000,
    "result_count": 100_000,
    "request_count": 2_000,
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
_NON_SECRET_CREDENTIAL_KEYS = frozenset({"credential_mode"})


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


def sign_account_session_token(
    *,
    user_id: str,
    workspace_id: str,
    secret: str,
    expires_at: datetime,
    issued_at: datetime | None = None,
    replay_id: str | None = None,
    audience: str = DEFAULT_ACCOUNT_SESSION_AUDIENCE,
    workspace_ids: Sequence[str] | None = None,
    session_id: str | None = None,
) -> str:
    if not secret.strip():
        raise ValueError("account session signing secret is required.")
    issued_at = issued_at or datetime.now(expires_at.tzinfo)
    replay_id = replay_id or secrets.token_urlsafe(24)
    workspace_ids = tuple(dict.fromkeys([workspace_id, *(workspace_ids or ())]))
    payload: dict[str, Any] = {
        "aud": audience,
        "exp": int(expires_at.timestamp()),
        "iat": int(issued_at.timestamp()),
        "jti": replay_id,
        "user_id": user_id,
        "workspace_id": workspace_id,
        "workspace_ids": list(workspace_ids),
    }
    if session_id:
        payload["session_id"] = session_id
    encoded_payload = _base64url(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signed = f"v1.{encoded_payload}"
    signature = hmac.new(secret.encode("utf-8"), signed.encode("utf-8"), hashlib.sha256).digest()
    return f"{signed}.{_base64url(signature)}"


class AccountSessionError(Exception):
    """Raised when an account session token fails verification.

    Carries a stable ``code`` and ``status_code`` so the HTTP layer can map it
    to a sanitized response without leaking why verification failed.
    """

    def __init__(self, code: str, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class AccountSessionClaims:
    user_id: str
    workspace_id: str
    workspace_ids: tuple[str, ...]
    audience: str
    issued_at: datetime
    expires_at: datetime
    replay_id: str | None = None
    session_id: str | None = None


def verify_account_session_token(
    token: str,
    *,
    secret: str,
    audience: str = DEFAULT_ACCOUNT_SESSION_AUDIENCE,
    now: datetime | None = None,
    clock_skew_seconds: int = 60,
    max_age_seconds: int | None = None,
) -> AccountSessionClaims:
    """Verify a ``v1`` account session token and return its claims.

    Counterpart to :func:`sign_account_session_token`. Mirrors the worker-side
    TypeScript verifier in ``cloudflare/yutome-capsule/src/account-grants.ts`` so
    the hosted API can derive tenant identity from a forwarded session token
    instead of trusting a client-supplied workspace header. Raises
    :class:`AccountSessionError` (never returns a partial result) on any failure.
    """

    if not secret or not secret.strip():
        raise AccountSessionError(
            "account_session_signing_unconfigured",
            "Account session signing secret is required.",
            status_code=503,
        )
    parts = token.split(".") if token else []
    if len(parts) != 3 or parts[0] != "v1" or not parts[1] or not parts[2]:
        raise AccountSessionError("account_session_malformed", "Account session was not a signed v1 token.")
    signed = f"{parts[0]}.{parts[1]}"
    expected = hmac.new(secret.encode("utf-8"), signed.encode("utf-8"), hashlib.sha256).digest()
    try:
        actual = _base64url_decode(parts[2])
    except (ValueError, TypeError) as exc:
        raise AccountSessionError("account_session_malformed", "Account session signature was not valid base64url.") from exc
    if not hmac.compare_digest(actual, expected):
        raise AccountSessionError("account_session_invalid", "Account session signature was invalid.")
    try:
        payload = json.loads(_base64url_decode(parts[1]).decode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise AccountSessionError("account_session_malformed", "Account session payload was not valid JSON.") from exc
    if not isinstance(payload, Mapping):
        raise AccountSessionError("account_session_malformed", "Account session payload was not an object.")

    if payload.get("aud") != audience:
        raise AccountSessionError("account_session_audience_mismatch", "Account session audience is not accepted.")

    now = now or datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    exp = _int_claim(payload, "exp")
    iat = _int_claim(payload, "iat")
    if exp is None or iat is None:
        raise AccountSessionError("account_session_malformed", "Account session is missing exp/iat.")
    if now_ts > exp + clock_skew_seconds:
        raise AccountSessionError("account_session_expired", "Account session has expired.")
    if iat > now_ts + clock_skew_seconds:
        raise AccountSessionError("account_session_not_yet_valid", "Account session was issued in the future.")
    if max_age_seconds is not None and now_ts - iat > max_age_seconds + clock_skew_seconds:
        raise AccountSessionError("account_session_expired", "Account session exceeded its maximum age.")

    user_id = _str_claim(payload, "user_id")
    workspace_id = _str_claim(payload, "workspace_id")
    if not user_id or not workspace_id:
        raise AccountSessionError("account_session_malformed", "Account session is missing user/workspace.")
    raw_ids = payload.get("workspace_ids")
    extra_ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else []
    workspace_ids = tuple(dict.fromkeys([workspace_id, *extra_ids]))
    return AccountSessionClaims(
        user_id=user_id,
        workspace_id=workspace_id,
        workspace_ids=workspace_ids,
        audience=audience,
        issued_at=datetime.fromtimestamp(iat, tz=timezone.utc),
        expires_at=datetime.fromtimestamp(exp, tz=timezone.utc),
        replay_id=_str_claim(payload, "jti"),
        session_id=_str_claim(payload, "session_id"),
    )


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _int_claim(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _str_claim(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value.strip() else None


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
        result_rows = _rows_from_result(result)
        if result_rows:
            rows[key] = result_rows[0]

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
    statement = insert(users).values(
        **{
            "id": account.user_id,
            "email": account.email.strip(),
            "normalized_email": account.normalized_email,
            "name": account.name,
            "status": "active",
            "created_at": func.now(),
        }
    )
    statement = statement.on_conflict_do_update(
        index_elements=[users.c.normalized_email],
        set_={
            "email": users.c.email,
            "name": func.coalesce(users.c.name, statement.excluded.name),
            "status": case((users.c.status == "disabled", users.c.status), else_="active"),
        },
    ).returning(users)
    return _sql_statement(statement)


def _sql_statement(statement: Any) -> SqlStatement:
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def upsert_personal_workspace_sql(account: AccountBootstrapInput) -> SqlStatement:
    statement = insert(workspaces).values(
        id=account.workspace_id,
        owner_user_id=account.user_id,
        name=account.workspace_name or _default_workspace_name(account.normalized_email),
        status="active",
        # A brand-new workspace starts a 14-day card-gated trial. The Stripe webhook mirror
        # later moves subscription_status to active/past_due/canceled.
        subscription_status="trialing",
        trial_ends_at=text(f"now() + interval '{int(TRIAL_PERIOD_DAYS)} days'"),
        created_at=func.now(),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[workspaces.c.id],
        set_={
            "owner_user_id": workspaces.c.owner_user_id,
            "name": workspaces.c.name,
            "status": case((workspaces.c.status == "disabled", workspaces.c.status), else_="active"),
            # Re-bootstrap (e.g. a returning sign-in) must NOT restart the trial or overwrite a
            # paid subscription_status — preserve the existing trial window and status.
            "subscription_status": workspaces.c.subscription_status,
            "trial_ends_at": workspaces.c.trial_ends_at,
        },
    ).returning(workspaces)
    return _sql_statement(statement)


def upsert_workspace_member_sql(account: AccountBootstrapInput) -> SqlStatement:
    statement = insert(workspace_members).values(
        workspace_id=account.workspace_id,
        user_id=account.user_id,
        role="owner",
        status="active",
        created_at=func.now(),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[workspace_members.c.workspace_id, workspace_members.c.user_id],
        set_={
            "role": literal_column("'owner'"),
            "status": case(
                (workspace_members.c.status == "disabled", workspace_members.c.status),
                else_="active",
            ),
        },
    ).returning(workspace_members)
    return _sql_statement(statement)


def upsert_starter_price_book_sql() -> SqlStatement:
    statement = insert(price_books).values(
        **{
            "id": STARTER_PRICE_BOOK_ID,
            "version": STARTER_PRICE_BOOK_VERSION,
            "currency": "usd",
            "products_jsonb": Jsonb([{"plan_key": STARTER_PLAN_KEY, "label": "Starter"}]),
            "unit_mapping_jsonb": Jsonb({"source": "account_bootstrap"}),
            "status": "active",
            "metadata_json": Jsonb({"managed_by": "yutome_account_core"}),
            "created_at": func.now(),
        }
    )
    statement = statement.on_conflict_do_update(
        index_elements=[price_books.c.version],
        set_={
            "status": literal_column("'active'"),
            "metadata_json": statement.excluded.metadata_json,
            "updated_at": func.now(),
        },
    ).returning(price_books)
    return _sql_statement(statement)


def upsert_starter_entitlement_policy_sql(workspace_id: str) -> SqlStatement:
    statement = insert(entitlement_policies).values(
        **{
            "id": starter_entitlement_policy_id(workspace_id),
            "workspace_id": workspace_id,
            "plan_key": STARTER_PLAN_KEY,
            "price_book_id": STARTER_PRICE_BOOK_ID,
            "allowed_operations": list(STARTER_ALLOWED_OPERATIONS),
            "included_units_jsonb": Jsonb(STARTER_INCLUDED_UNITS),
            "hard_limits_jsonb": Jsonb(STARTER_HARD_LIMITS),
            "soft_limits_jsonb": Jsonb({}),
            "grace_policy_jsonb": Jsonb({}),
            "status": "active",
            "metadata_json": Jsonb({"source": "account_bootstrap"}),
            "created_at": func.now(),
        }
    )
    statement = statement.on_conflict_do_update(
        index_elements=[
            entitlement_policies.c.workspace_id,
            entitlement_policies.c.plan_key,
            entitlement_policies.c.price_book_id,
        ],
        set_={
            "allowed_operations": statement.excluded.allowed_operations,
            "included_units_jsonb": statement.excluded.included_units_jsonb,
            "hard_limits_jsonb": statement.excluded.hard_limits_jsonb,
            "soft_limits_jsonb": statement.excluded.soft_limits_jsonb,
            "metadata_json": statement.excluded.metadata_json,
            "updated_at": func.now(),
        },
    ).returning(entitlement_policies)
    return _sql_statement(statement)


def upsert_starter_workspace_balance_sql(workspace_id: str) -> SqlStatement:
    statement = insert(workspace_balances).values(
        **{
            "workspace_id": workspace_id,
            "entitlement_policy_id": starter_entitlement_policy_id(workspace_id),
            "period_start_at": text("date_trunc('month', now())"),
            "period_end_at": text("date_trunc('month', now()) + interval '1 month'"),
            "used_units_jsonb": Jsonb({}),
            "reserved_units_jsonb": Jsonb({}),
            "remaining_units_jsonb": Jsonb(STARTER_INCLUDED_UNITS),
            "unlimited_units": [],
            "metadata_json": Jsonb({"source": "account_bootstrap"}),
            "updated_at": func.now(),
        }
    )
    statement = statement.on_conflict_do_update(
        index_elements=[workspace_balances.c.workspace_id],
        set_={
            "entitlement_policy_id": statement.excluded.entitlement_policy_id,
            "remaining_units_jsonb": workspace_balances.c.remaining_units_jsonb,
            "metadata_json": workspace_balances.c.metadata_json,
            "updated_at": func.now(),
        },
    ).returning(workspace_balances)
    return _sql_statement(statement)


def upsert_starter_provider_allocation_sql(workspace_id: str, *, provider: str, operation: str) -> SqlStatement:
    model_or_plan = HOSTED_DEFAULT_EMBEDDING_MODEL if provider == "voyage" else None
    external_allocation_id = f"webshare_subuser_pending:{workspace_id}" if provider == "webshare" else None
    statement = insert(provider_allocations).values(
        **{
            "id": starter_provider_allocation_id(workspace_id, provider, operation),
            "workspace_id": workspace_id,
            "provider": provider,
            "operation": operation,
            "credential_mode": "hosted",
            "status": "active",
            "model_or_plan": model_or_plan,
            "external_allocation_id": external_allocation_id,
            "metadata_json": Jsonb({"source": "account_bootstrap", "allocation_mode": "hosted"}),
            "created_at": func.now(),
        }
    )
    statement = statement.on_conflict_do_update(
        index_elements=[provider_allocations.c.id],
        set_={
            "status": case(
                (provider_allocations.c.status == "disabled", provider_allocations.c.status),
                else_="active",
            ),
            "model_or_plan": statement.excluded.model_or_plan,
            "metadata_json": statement.excluded.metadata_json,
        },
    ).returning(provider_allocations)
    return _sql_statement(statement)


def upsert_starter_service_allocation_sql(
    workspace_id: str,
    *,
    service: str = "search_store",
    operation: str = "index_write",
) -> SqlStatement:
    if service != "search_store":
        raise ValueError("starter account bootstrap only supports search_store service allocations")
    statement = insert(service_allocations).values(
        **{
            "id": starter_service_allocation_id(workspace_id, service, operation),
            "workspace_id": workspace_id,
            "service": service,
            "operation": operation,
            "credential_mode": "service_internal",
            "status": "active",
            "backend": HOSTED_VECTOR_BACKEND,
            "index_profile_ref": None,
            "metadata_json": Jsonb({"source": "account_bootstrap"}),
            "created_at": func.now(),
        }
    )
    statement = statement.on_conflict_do_update(
        index_elements=[service_allocations.c.id],
        set_={
            "status": case(
                (service_allocations.c.status == "disabled", service_allocations.c.status),
                else_="active",
            ),
            "backend": statement.excluded.backend,
            "index_profile_ref": statement.excluded.index_profile_ref,
            "metadata_json": statement.excluded.metadata_json,
        },
    ).returning(service_allocations)
    return _sql_statement(statement)


def upsert_account_session_sql(account: AccountBootstrapInput) -> SqlStatement:
    if account.session_token is None:
        raise ValueError("session_token is required to persist an account session.")
    token_hash = session_token_hash(account.session_token)
    statement = insert(account_sessions).values(
        **{
            "id": deterministic_account_session_id(token_hash),
            "user_id": account.user_id,
            "workspace_id": account.workspace_id,
            "session_hash": token_hash,
            "status": "active",
            "scopes": list(account.session_scopes),
            "audience": account.session_audience,
            "client_id": account.session_client_id,
            "expires_at": account.session_expires_at,
            "metadata_json": Jsonb({"source": "account_bootstrap"}),
            "created_at": func.now(),
        }
    )
    statement = statement.on_conflict_do_update(
        index_elements=[account_sessions.c.session_hash],
        set_={
            "last_used_at": func.now(),
            "scopes": statement.excluded.scopes,
            "audience": statement.excluded.audience,
            "client_id": statement.excluded.client_id,
            "expires_at": statement.excluded.expires_at,
        },
    ).returning(account_sessions)
    return _sql_statement(statement)


def load_account_session_sql(*, session_hash: str) -> SqlStatement:
    statement = (
        select(account_sessions)
        .where(account_sessions.c.session_hash == bindparam("session_hash", value=session_hash))
        .limit(1)
    )
    return _sql_statement(statement)


def revoke_account_session_sql(*, session_hash: str, now: datetime) -> SqlStatement:
    statement = (
        update(account_sessions)
        .where(account_sessions.c.session_hash == bindparam("session_hash", value=session_hash))
        .values(status=literal("revoked"), revoked_at=bindparam("revoked_at", value=now))
        .returning(account_sessions)
    )
    return _sql_statement(statement)


def starter_entitlement_policy_id(workspace_id: str) -> str:
    return _stable_id("ent", f"{workspace_id}:{STARTER_PLAN_KEY}:{STARTER_PRICE_BOOK_ID}")


def starter_provider_allocation_id(workspace_id: str, provider: str, operation: str) -> str:
    return _stable_id("alloc", f"{workspace_id}:{provider}:{operation}:hosted")


def starter_service_allocation_id(workspace_id: str, service: str, operation: str) -> str:
    return _stable_id("svc", f"{workspace_id}:{service}:{operation}:service_internal")


def sql_params_contain_provider_credentials(statement: SqlStatement) -> bool:
    return _contains_credential_shape(statement.params)


def _contains_credential_shape(value: Any) -> bool:
    if isinstance(value, Jsonb):
        return _contains_credential_shape(value.obj)
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered not in _NON_SECRET_CREDENTIAL_KEYS and any(
                fragment in lowered for fragment in _CREDENTIAL_KEY_FRAGMENTS
            ):
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


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _rows_from_result(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, list):
        return [dict(row) for row in result]
    if isinstance(result, tuple):
        return [dict(row) for row in result]
    if hasattr(result, "mappings"):
        return [dict(row) for row in result.mappings()]
    if hasattr(result, "fetchall"):
        return [dict(row) for row in result.fetchall()]
    try:
        return [dict(row) for row in result]
    except TypeError:
        return []


def _default_workspace_name(normalized_email: str) -> str:
    local_part = normalized_email.partition("@")[0]
    return f"{local_part}'s workspace"


__all__ = [
    "AccountBootstrapInput",
    "AccountBootstrapResult",
    "AccountPrincipal",
    "AccountSession",
    "AccountSessionClaims",
    "AccountSessionError",
    "DEFAULT_ACCOUNT_SESSION_AUDIENCE",
    "STARTER_ALLOWED_OPERATIONS",
    "STARTER_HARD_LIMITS",
    "STARTER_INCLUDED_UNITS",
    "STARTER_PLAN_KEY",
    "STARTER_PRICE_BOOK_ID",
    "STARTER_PRICE_BOOK_VERSION",
    "STARTER_PROVIDER_OPERATIONS",
    "STARTER_SERVICE_OPERATIONS",
    "TRIAL_PERIOD_DAYS",
    "account_bootstrap_sql",
    "bootstrap_hosted_account",
    "deterministic_account_session_id",
    "deterministic_personal_workspace_id",
    "deterministic_user_id",
    "load_account_session_sql",
    "normalize_email",
    "revoke_account_session_sql",
    "session_token_hash",
    "sign_account_session_token",
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
    "verify_account_session_token",
]
