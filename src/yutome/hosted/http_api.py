from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request

from yutome import contract
from yutome.hosted.account import (
    DEFAULT_ACCOUNT_SESSION_AUDIENCE,
    TRIAL_PERIOD_DAYS,
    AccountBootstrapInput,
    AccountSessionError,
    bootstrap_hosted_account,
    load_account_session_sql,
    normalize_email,
    revoke_account_session_sql,
    session_token_hash,
    sign_account_session_token,
    verify_account_session_token,
)
from yutome.hosted.auth_login import (
    DEFAULT_LOGIN_TOKEN_TTL_SECONDS,
    consume_login_token_sql,
    insert_login_token_sql,
    login_token_hash,
    new_login_token,
    new_login_token_id,
)
from yutome.hosted.email import EmailMessage, EmailSender, EmailSendError, build_email_sender_from_env
from yutome.hosted.google_signin_service import (
    GoogleSignInSettings,
    HostedGoogleSignInError,
    complete_google_signin,
    google_signin_settings_from_env,
    start_google_signin,
)
from yutome.hosted.account_cli import (
    CLI_ACCOUNT_READ_SCOPE,
    CLI_AUTH_CODE_TTL_SECONDS,
    CLI_JOB_WRITE_SCOPE,
    CLI_LIBRARY_READ_SCOPE,
    CLI_SOURCE_WRITE_SCOPE,
    CLI_TOKEN_TTL_SECONDS,
    DEFAULT_CLI_AUDIENCE,
    DEFAULT_CLI_CLIENT_ID,
    DEFAULT_CLI_SCOPES,
    activate_pending_cli_grant_sql,
    code_challenge_for_verifier,
    code_hash,
    create_pending_cli_grant_sql,
    load_cli_grant_by_code_hash_sql,
    load_cli_grant_by_id_sql,
    mark_cli_grant_used_sql,
    new_authorization_code,
    new_install_id,
    sign_cli_token,
    verify_cli_token,
)
from yutome.hosted.account_read import (
    load_active_workspace,
    read_active_account_grants,
    read_library_overview,
    read_workspace_summary,
)
from yutome.hosted.mcp_query import HostedMcpAuthContext, HostedMcpError, HostedMcpQueryAdapter
from yutome.hosted.rate_gate import DEFAULT_REQUESTS_PER_MINUTE, RateGate
from yutome.hosted.source_import import (
    HostedSourceImportActor,
    HostedSourceImportDescriptor,
    HostedSourceImportError,
    HostedSourcesImportRequest,
    account_jobs_sql,
    import_sources,
    job_row_json,
    list_source_jobs,
)
from yutome.hosted.youtube_oauth_service import (
    HostedYouTubeOAuthError,
    YouTubeOAuthSettings,
    complete_youtube_authorization,
    import_youtube_subscription_channels,
    list_youtube_subscription_channels,
    revoke_youtube_connection,
    start_youtube_authorization,
    youtube_connection_status,
    youtube_oauth_settings_from_env,
)


WORKSPACE_HEADER = "X-Yutome-Workspace-Id"
SCOPES_HEADER = "X-Yutome-Scopes"
USER_HEADER = "X-Yutome-User-Id"
GRANT_HEADER = "X-Yutome-Grant-Id"
CLIENT_HEADER = "X-Yutome-Client-Id"
SESSION_HEADER = "X-Yutome-Session-Id"
ACCOUNT_SESSION_TOKEN_HEADER = "X-Yutome-Account-Session"
TOKEN_ENV_VAR = "YUTOME_HOSTED_API_TOKEN"
DASHBOARD_TOKEN_ENV_VAR = "YUTOME_DASHBOARD_API_TOKEN"
STRIPE_WEBHOOK_SECRET_ENV_VAR = "STRIPE_WEBHOOK_SECRET"
STRIPE_SECRET_KEY_ENV_VAR = "STRIPE_SECRET_KEY"
# The metered overage credits Price (no quantity); the flat $4 recurring seat Price (quantity 1).
STRIPE_PRICE_ID_ENV_VAR = "STRIPE_PRICE_ID"
STRIPE_SEAT_PRICE_ID_ENV_VAR = "STRIPE_SEAT_PRICE_ID"
STRIPE_CHECKOUT_SUCCESS_URL_ENV_VAR = "STRIPE_CHECKOUT_SUCCESS_URL"
STRIPE_CHECKOUT_CANCEL_URL_ENV_VAR = "STRIPE_CHECKOUT_CANCEL_URL"
STRIPE_PORTAL_RETURN_URL_ENV_VAR = "STRIPE_PORTAL_RETURN_URL"
STRIPE_API_BASE_ENV_VAR = "STRIPE_API_BASE"
ACCOUNT_SESSION_HMAC_SECRET_ENV_VAR = "YUTOME_ACCOUNT_SESSION_HMAC_SECRET"
ACCOUNT_SESSION_AUDIENCE_ENV_VAR = "YUTOME_ACCOUNT_SESSION_AUDIENCE"
ACCOUNT_SESSION_MAX_AGE_SECONDS_ENV_VAR = "YUTOME_ACCOUNT_SESSION_MAX_AGE_SECONDS"
ACCOUNT_SESSION_COOKIE_NAME = "yutome_account_session"
ACCOUNT_SESSION_TTL_SECONDS = 60 * 60
APP_BASE_URL_ENV_VAR = "YUTOME_APP_URL"
LOGIN_TOKEN_TTL_SECONDS_ENV_VAR = "YUTOME_AUTH_LOGIN_TTL_SECONDS"
RATE_LIMIT_RPM_ENV_VAR = "YUTOME_HOSTED_RATE_LIMIT_RPM"
AUTH_DEV_RETURN_LINK_ENV_VAR = "YUTOME_AUTH_DEV_RETURN_LINK"
_SAFE_READINESS_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_READINESS_ERROR_FIELDS = frozenset({"error", "message", "detail"})
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_ENCODED_SLASH_OR_BACKSLASH = re.compile(r"%(?:2f|5c)", re.IGNORECASE)
_AUDIT_LOGGER = logging.getLogger("yutome.hosted.audit")


class ToolCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ResourceReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str


class AccountBootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    name: str | None = None
    workspace_name: str | None = None


class AccountLoginStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    name: str | None = None
    workspace_name: str | None = None
    redirect_path: str | None = None


class AccountLoginVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str


class AccountGoogleAuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    redirect_uri: str
    redirect_path: str | None = None


class AccountGoogleCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    state: str
    redirect_uri: str


class AccountCliAuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code_challenge: str
    code_challenge_method: str = "S256"
    redirect_uri: str
    state: str | None = None
    scopes: list[str] = Field(default_factory=list)
    client_id: str = DEFAULT_CLI_CLIENT_ID


class AccountCliTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    code_verifier: str
    redirect_uri: str


class AccountYouTubeAuthorizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    redirect_uri: str


class AccountYouTubeCallbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    state: str
    redirect_uri: str


class AccountYouTubeSubscriptionsImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_ids: list[str]
    refresh_enabled: bool = True
    max_new_videos: int = Field(default=25, ge=1, le=250)
    cadence_seconds: int = Field(default=900, ge=60, le=86400)


AccountSourceImportDescriptor = HostedSourceImportDescriptor
AccountSourcesImportRequest = HostedSourcesImportRequest


class AccountSearchRequest(BaseModel):
    """Dashboard retrieval request. Mirrors the `find` tool arguments, minus any
    tenant identity: the workspace comes only from the verified session token."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    text: str
    mode: str | None = None
    in_: str | None = Field(default=None, alias="in")
    channel: str | None = None
    since: str | None = None
    until: str | None = None
    source: str | None = None
    language: str | None = None
    group_by: str | None = None
    project: str | None = None
    limit: int | None = Field(default=None, ge=1, le=200)
    offset: int | None = Field(default=None, ge=0)


class AccountShowRequest(BaseModel):
    """Dashboard citation/transcript expansion. Mirrors the `show` tool arguments."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    id: str | None = None
    token_budget: int | None = Field(default=None, ge=200, le=8000)
    transcript_offset: int | None = Field(default=None, ge=0)
    transcript_limit: int | None = Field(default=None, ge=1, le=5000)


class AccountListRequest(BaseModel):
    """Dashboard browse request. Mirrors the `list` tool arguments. The adapter
    enforces which (entity, filter) combinations are valid — e.g. `selected` is
    channels-only and `order_by` is videos-only — and surfaces 501 for the rest."""

    model_config = ConfigDict(extra="forbid")

    entity: str
    channel: str | None = None
    selected: bool | None = None
    order_by: str | None = None
    limit: int | None = Field(default=None, ge=1, le=200)
    offset: int | None = Field(default=None, ge=0)


class AccountApiContext(BaseModel):
    """Authenticated dashboard caller. workspace_id is derived from the verified
    session token, never from a client-supplied header."""

    workspace_id: str
    user_id: str
    workspace_name: str | None = None


class AccountCliApiContext(BaseModel):
    """Authenticated hosted CLI caller. workspace_id is loaded from the
    active CLI grant, never from a request body."""

    workspace_id: str
    user_id: str
    grant_id: str
    scopes: set[str] = Field(default_factory=set)
    client_id: str | None = None
    install_id: str | None = None


AccountSourceImportActor = HostedSourceImportActor


AuthDependency = Callable[..., HostedMcpAuthContext]
ReadinessCheck = Callable[[], Any]


def build_postgres_app(
    *,
    connection: Any,
    readiness_check: ReadinessCheck | None = None,
    gate: Any | None = None,
    ledger: Any | None = None,
    usage_context_provider: Any | None = None,
    voyage_usage_context_provider: Any | None = None,
    index_profile_ref: str | None = None,
    expected_api_token: str | None = None,
    expected_account_api_token: str | None = None,
    stripe_webhook_secret: str | None = None,
    account_session_secret: str | None = None,
    account_session_audience: str | None = None,
    account_session_ttl_seconds: int | None = None,
    email_sender: EmailSender | None = None,
    app_base_url: str | None = None,
    login_token_ttl_seconds: int | None = None,
    dev_return_login_link: bool | None = None,
    youtube_oauth_settings: YouTubeOAuthSettings | None = None,
    google_signin_settings: GoogleSignInSettings | None = None,
    requests_per_minute: int | None = None,
) -> Any:
    from yutome.hosted.entitlements import PostgresUsageContextProvider
    from yutome.hosted.search_store import PostgresVectorChordSearchStore

    search_store = PostgresVectorChordSearchStore(connection, index_profile_ref=index_profile_ref)
    entitlement_provider = PostgresUsageContextProvider(connection)
    adapter = HostedMcpQueryAdapter(
        search_store=search_store,
        gate=gate,
        ledger=ledger,
        usage_context_provider=usage_context_provider or entitlement_provider,
        voyage_usage_context_provider=voyage_usage_context_provider or entitlement_provider.voyage,
        source_connection=connection,
    )
    app = build_app(
        adapter=adapter,
        readiness_check=readiness_check,
        expected_api_token=_normalize_api_token(expected_api_token) or _api_token_from_env(),
        expected_account_api_token=_normalize_api_token(expected_account_api_token) or _dashboard_api_token_from_env(),
        billing_connection=connection,
        stripe_webhook_secret=_normalize_api_token(stripe_webhook_secret) or _stripe_webhook_secret_from_env(),
        account_session_secret=_normalize_api_token(account_session_secret) or _account_session_secret_from_env(),
        account_session_audience=_normalize_api_token(account_session_audience) or _account_session_audience_from_env(),
        account_session_ttl_seconds=account_session_ttl_seconds or _account_session_ttl_seconds_from_env(),
        email_sender=email_sender or build_email_sender_from_env(),
        app_base_url=app_base_url or _app_base_url_from_env(),
        login_token_ttl_seconds=login_token_ttl_seconds or _login_token_ttl_seconds_from_env(),
        dev_return_login_link=dev_return_login_link
        if dev_return_login_link is not None
        else _auth_dev_return_link_from_env(),
        youtube_oauth_settings=youtube_oauth_settings or youtube_oauth_settings_from_env(os.environ),
        google_signin_settings=google_signin_settings or google_signin_settings_from_env(os.environ),
        requests_per_minute=requests_per_minute,
    )
    app.state.hosted_connection = connection
    app.state.hosted_search_store = search_store
    app.state.hosted_adapter = adapter

    def close_postgres_pool() -> None:
        close_hosted_connection(connection)

    app.router.on_shutdown.append(close_postgres_pool)
    return app


def build_app(
    *,
    adapter: HostedMcpQueryAdapter,
    auth_dependency: AuthDependency | None = None,
    readiness_check: ReadinessCheck | None = None,
    expected_api_token: str | None = None,
    expected_account_api_token: str | None = None,
    billing_connection: Any | None = None,
    stripe_webhook_secret: str | None = None,
    account_session_secret: str | None = None,
    account_session_audience: str | None = None,
    account_session_ttl_seconds: int | None = None,
    email_sender: EmailSender | None = None,
    app_base_url: str | None = None,
    login_token_ttl_seconds: int | None = None,
    dev_return_login_link: bool | None = None,
    youtube_oauth_settings: YouTubeOAuthSettings | None = None,
    google_signin_settings: GoogleSignInSettings | None = None,
    requests_per_minute: int | None = None,
) -> Any:
    from fastapi import Depends, FastAPI, Header
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import JSONResponse
    from yutome.hosted.billing import (
        StripeWebhookVerificationError,
        load_stripe_customer_sql,
        process_stripe_webhook_event,
        stripe_customer_id as _build_stripe_customer_id,
        stripe_webhook_processing_statements,
        upsert_stripe_customer_sql,
        verify_stripe_webhook_signature,
    )
    from yutome.hosted.billing import StripeCustomer as _StripeCustomer

    async def request_connection_lease() -> Any:
        lease_factory = getattr(billing_connection, "request_lease", None)
        if not callable(lease_factory):
            yield
            return
        with lease_factory():
            yield

    app = FastAPI(
        title="yutome-hosted-mcp",
        description="Hosted Yutome MCP query API for the Cloudflare MCP edge.",
        version="0.1.0",
        dependencies=[Depends(request_connection_lease)],
    )
    app.state.hosted_adapter = adapter
    app.state.hosted_billing_connection = billing_connection
    normalized_api_token = _normalize_api_token(expected_api_token)
    normalized_account_api_token = _normalize_api_token(expected_account_api_token)
    normalized_stripe_webhook_secret = _normalize_api_token(stripe_webhook_secret)
    normalized_account_session_secret = _normalize_api_token(account_session_secret)
    normalized_account_session_audience = (
        _normalize_api_token(account_session_audience) or DEFAULT_ACCOUNT_SESSION_AUDIENCE
    )
    account_session_ttl = _positive_int(account_session_ttl_seconds, ACCOUNT_SESSION_TTL_SECONDS)
    resolved_email_sender = email_sender or build_email_sender_from_env()
    resolved_app_base_url = (app_base_url or "").strip().rstrip("/")
    login_token_ttl = _positive_int(login_token_ttl_seconds, DEFAULT_LOGIN_TOKEN_TTL_SECONDS)
    resolved_dev_return_login_link = bool(dev_return_login_link)
    resolved_youtube_oauth_settings = youtube_oauth_settings or youtube_oauth_settings_from_env(os.environ)
    resolved_google_signin_settings = google_signin_settings or google_signin_settings_from_env(os.environ)
    configured_requests_per_minute = (
        requests_per_minute if requests_per_minute is not None else _rate_limit_rpm_from_env()
    )
    resolved_rate_limit_rpm = (
        configured_requests_per_minute
        if configured_requests_per_minute is not None
        else DEFAULT_REQUESTS_PER_MINUTE
    )
    app.state.hosted_rate_gate = RateGate(requests_per_minute=resolved_rate_limit_rpm)
    app.state.hosted_rate_limit_requests_per_minute = resolved_rate_limit_rpm
    app.state.hosted_api_auth_required = True
    app.state.hosted_api_auth_configured = auth_dependency is not None or bool(normalized_api_token)
    app.state.hosted_rate_limit_configured = True
    app.state.stripe_webhook_configured = bool(normalized_stripe_webhook_secret)
    app.state.account_session_signing_configured = bool(normalized_account_session_secret)
    app.state.hosted_account_api_configured = bool(normalized_account_api_token)
    app.state.youtube_oauth_configured = resolved_youtube_oauth_settings.configured
    app.state.google_signin_configured = resolved_google_signin_settings.configured

    async def default_auth_dependency(
        authorization: str | None = Header(default=None),
        workspace_id: str | None = Header(default=None, alias=WORKSPACE_HEADER),
        scopes_header: str | None = Header(default=None, alias=SCOPES_HEADER),
        user_id: str | None = Header(default=None, alias=USER_HEADER),
        grant_id: str | None = Header(default=None, alias=GRANT_HEADER),
        client_id: str | None = Header(default=None, alias=CLIENT_HEADER),
        session_id: str | None = Header(default=None, alias=SESSION_HEADER),
    ) -> HostedMcpAuthContext:
        _verify_bearer_token(authorization=authorization, expected_api_token=normalized_api_token)
        if workspace_id is None or not workspace_id.strip():
            raise _http_error(
                HostedMcpError(
                    code="workspace_required",
                    message=f"Missing required {WORKSPACE_HEADER} header.",
                    status_code=401,
                )
            )
        scopes = _parse_scopes(scopes_header)
        try:
            return HostedMcpAuthContext(
                workspace_id=workspace_id,
                scopes=frozenset(scopes),
                user_id=user_id,
                grant_id=grant_id,
                client_id=client_id,
                session_id=session_id,
            ).validated()
        except HostedMcpError as exc:
            raise _http_error(exc) from exc

    auth = auth_dependency or default_auth_dependency

    @app.middleware("http")
    async def rate_limit(request: Any, call_next: Any) -> Any:
        path = request.url.path
        authorization = request.headers.get("authorization")
        workspace_id = _optional_text(request.headers.get(WORKSPACE_HEADER))
        bearer_token = _bearer_token_or_none(authorization)
        client_ip = _rate_limit_client_ip(request)

        if path in {"/healthz", "/readyz"}:
            response = await call_next(request)
            _audit_hosted_request(
                request=request,
                response=response,
                workspace_id=workspace_id,
                auth_bearer_present=bearer_token is not None,
                client_ip=client_ip,
                rate_limit_outcome="allowed",
            )
            return response

        if path == "/webhooks/stripe":
            # The billing mirror (Stripe) webhook is exempt from the frequency gate; the
            # handler performs Stripe signature verification before it mutates state.
            response = await call_next(request)
            _audit_hosted_request(
                request=request,
                response=response,
                workspace_id=workspace_id,
                auth_bearer_present=bearer_token is not None,
                client_ip=client_ip,
                rate_limit_outcome="allowed",
            )
            return response

        key = _rate_limit_key(bearer_token=bearer_token, client_ip=client_ip)
        rpm = int(getattr(app.state, "hosted_rate_limit_requests_per_minute", DEFAULT_REQUESTS_PER_MINUTE))
        # TODO: EntitlementPolicy.requests_per_minute is hydrated for a future shared
        # store/UsageGate path. This single-process middleware uses the app default to
        # avoid a DB read before auth dependencies run.
        decision = app.state.hosted_rate_gate.check(key, requests_per_minute=rpm)
        if not decision.allowed:
            error = HostedMcpError(
                code="rate_limited",
                message=decision.message or "Too many requests.",
                status_code=429,
            )
            response = JSONResponse(
                status_code=429,
                content={"detail": error.to_dict()["error"]},
                headers=_rate_limit_headers(decision, include_retry_after=True),
            )
            _audit_hosted_request(
                request=request,
                response=response,
                workspace_id=workspace_id,
                auth_bearer_present=bearer_token is not None,
                client_ip=client_ip,
                rate_limit_outcome="limited",
            )
            return response

        response = await call_next(request)
        for name, value in _rate_limit_headers(decision).items():
            response.headers.setdefault(name, value)
        _audit_hosted_request(
            request=request,
            response=response,
            workspace_id=workspace_id,
            auth_bearer_present=bearer_token is not None,
            client_ip=client_ip,
            rate_limit_outcome="allowed",
        )
        return response

    @app.middleware("http")
    async def security_headers(request: Any, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "yutome-hosted-mcp",
            "contract": {
                "auth_scope": contract.AUTH_SCOPE,
                "tools": [tool.name for tool in contract.TOOLS],
                "resource_hosts": [resource.host for resource in contract.RESOURCES],
            },
        }

    @app.get("/readyz")
    def readyz() -> Any:
        payload: dict[str, Any] = {
            "ok": True,
            "service": "yutome-hosted-mcp",
            "adapter": "ready",
        }
        if readiness_check is None:
            return payload
        try:
            checks = _jsonable(readiness_check())
        except Exception as exc:  # pragma: no cover - defensive live readiness path
            payload["ok"] = False
            payload["checks"] = _readiness_exception_payload(exc)
            return JSONResponse(status_code=503, content=payload)
        checks = _sanitize_readiness_payload(checks)
        payload["checks"] = checks
        if isinstance(checks, Mapping) and checks.get("ok") is False:
            payload["ok"] = False
        status_code = 200 if payload["ok"] else 503
        return JSONResponse(status_code=status_code, content=payload)

    @app.post("/tools/call")
    @app.post("/mcp/tools/call")
    def call_tool(
        request: ToolCallRequest,
        auth_context: HostedMcpAuthContext = Depends(auth),
    ) -> dict[str, Any]:
        try:
            result = adapter.call_tool(
                auth=auth_context,
                name=request.name,
                arguments=request.arguments,
            )
        except HostedMcpError as exc:
            raise _http_error(exc) from exc
        return {"ok": True, "result": result}

    @app.post("/resources/read")
    @app.post("/mcp/resources/read")
    def read_resource(
        request: ResourceReadRequest,
        auth_context: HostedMcpAuthContext = Depends(auth),
    ) -> dict[str, Any]:
        try:
            result = adapter.read_resource(auth=auth_context, uri=request.uri)
        except HostedMcpError as exc:
            raise _http_error(exc) from exc
        return {"ok": True, "result": result}

    @app.post("/account/bootstrap")
    def account_bootstrap(
        request: AccountBootstrapRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        # Kept for the MCP edge worker's OAuth/pairing flow. The dashboard uses
        # /account/login/start + /account/login/verify so its bearer cannot mint
        # an unverified account session through this legacy bootstrap endpoint.
        _verify_bearer_token(
            authorization=authorization,
            expected_api_token=normalized_api_token,
        )
        if billing_connection is None:
            raise _http_error(
                HostedMcpError(
                    code="account_bootstrap_connection_unconfigured",
                    message="Hosted account bootstrap requires a database connection.",
                    status_code=503,
                )
            )
        if not normalized_account_session_secret:
            raise _http_error(
                HostedMcpError(
                    code="account_session_signing_unconfigured",
                    message=f"Set {ACCOUNT_SESSION_HMAC_SECRET_ENV_VAR} before creating hosted account sessions.",
                    status_code=503,
                )
            )

        try:
            issued_at = datetime.now(timezone.utc)
            expires_at = issued_at + timedelta(seconds=account_session_ttl)
            bootstrap_input = AccountBootstrapInput(
                email=request.email,
                name=_optional_text(request.name),
                workspace_name=_optional_text(request.workspace_name),
            )
            session_token = sign_account_session_token(
                user_id=bootstrap_input.user_id,
                workspace_id=bootstrap_input.workspace_id,
                secret=normalized_account_session_secret,
                expires_at=expires_at,
                issued_at=issued_at,
                audience=normalized_account_session_audience,
            )
            session_input = AccountBootstrapInput(
                email=bootstrap_input.email,
                name=bootstrap_input.name,
                workspace_name=bootstrap_input.workspace_name,
                session_token=session_token,
                session_scopes=(contract.AUTH_SCOPE,),
                session_audience=normalized_account_session_audience,
                session_expires_at=expires_at,
            )
            result = _bootstrap_account_in_transaction(billing_connection, session_input)
        except ValueError as exc:
            raise _http_error(
                HostedMcpError(
                    code="account_bootstrap_invalid",
                    message=str(exc),
                    status_code=400,
                )
            ) from exc
        return {
            "ok": True,
            "principal": result.principal.model_dump(mode="json"),
            "session": {
                "token": session_token,
                "expires_at": expires_at.isoformat(),
                "audience": normalized_account_session_audience,
                "cookie_name": ACCOUNT_SESSION_COOKIE_NAME,
                "max_age_seconds": account_session_ttl,
            },
        }

    @app.post("/account/google/authorize")
    def account_google_authorize(
        request: AccountGoogleAuthorizeRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _verify_bearer_token_any(
            authorization=authorization,
            expected_tokens=(normalized_api_token, normalized_account_api_token),
        )
        if not normalized_account_session_secret:
            raise _http_error(
                HostedMcpError(
                    code="account_session_signing_unconfigured",
                    message=f"Set {ACCOUNT_SESSION_HMAC_SECRET_ENV_VAR} before creating hosted account sessions.",
                    status_code=503,
                )
            )
        try:
            return start_google_signin(
                settings=resolved_google_signin_settings,
                redirect_uri=request.redirect_uri,
                redirect_path=_safe_redirect_path(request.redirect_path),
                state_secret=normalized_account_session_secret,
            )
        except HostedGoogleSignInError as exc:
            raise _http_error(
                HostedMcpError(
                    code=exc.code,
                    message=exc.message,
                    status_code=exc.status_code,
                    data=exc.data,
                )
            ) from exc

    @app.post("/account/google/callback")
    def account_google_callback(
        request: AccountGoogleCallbackRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _verify_bearer_token_any(
            authorization=authorization,
            expected_tokens=(normalized_api_token, normalized_account_api_token),
        )
        if billing_connection is None:
            raise _http_error(
                HostedMcpError(
                    code="account_login_connection_unconfigured",
                    message="Hosted login requires a database connection.",
                    status_code=503,
                )
            )
        if not normalized_account_session_secret:
            raise _http_error(
                HostedMcpError(
                    code="account_session_signing_unconfigured",
                    message=f"Set {ACCOUNT_SESSION_HMAC_SECRET_ENV_VAR} before creating hosted account sessions.",
                    status_code=503,
                )
            )
        now = datetime.now(timezone.utc)
        try:
            identity = complete_google_signin(
                settings=resolved_google_signin_settings,
                code=request.code,
                state=request.state,
                redirect_uri=request.redirect_uri,
                state_secret=normalized_account_session_secret,
                now=now,
            )
            expires_at = now + timedelta(seconds=account_session_ttl)
            bootstrap_input = AccountBootstrapInput(
                email=identity.email,
                name=_optional_text(identity.name),
            )
            session_token = sign_account_session_token(
                user_id=bootstrap_input.user_id,
                workspace_id=bootstrap_input.workspace_id,
                secret=normalized_account_session_secret,
                expires_at=expires_at,
                issued_at=now,
                audience=normalized_account_session_audience,
            )
            session_input = AccountBootstrapInput(
                email=bootstrap_input.email,
                name=bootstrap_input.name,
                workspace_name=bootstrap_input.workspace_name,
                session_token=session_token,
                session_scopes=(contract.AUTH_SCOPE,),
                session_audience=normalized_account_session_audience,
                session_expires_at=expires_at,
            )
            result = _bootstrap_account_in_transaction(billing_connection, session_input)
        except HostedGoogleSignInError as exc:
            raise _http_error(
                HostedMcpError(
                    code=exc.code,
                    message=exc.message,
                    status_code=exc.status_code,
                    data=exc.data,
                )
            ) from exc
        except ValueError as exc:
            raise _http_error(HostedMcpError(code="account_login_invalid", message=str(exc), status_code=400)) from exc
        return {
            "ok": True,
            "principal": result.principal.model_dump(mode="json"),
            "session": {
                "token": session_token,
                "expires_at": expires_at.isoformat(),
                "audience": normalized_account_session_audience,
                "cookie_name": ACCOUNT_SESSION_COOKIE_NAME,
                "max_age_seconds": account_session_ttl,
            },
            "redirect_path": _safe_redirect_path(identity.redirect_path),
        }

    @app.post("/account/login/start")
    def account_login_start(
        request: AccountLoginStartRequest,
        user_agent: str | None = Header(default=None, alias="User-Agent"),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        # The web BFF and MCP edge may request a sign-in link, but this only
        # records a single-use token and emails a link - it never mints a session.
        _verify_bearer_token_any(
            authorization=authorization,
            expected_tokens=(normalized_api_token, normalized_account_api_token),
        )
        if billing_connection is None:
            raise _http_error(
                HostedMcpError(
                    code="account_login_connection_unconfigured",
                    message="Hosted login requires a database connection.",
                    status_code=503,
                )
            )
        if not resolved_app_base_url:
            raise _http_error(
                HostedMcpError(
                    code="app_url_unconfigured",
                    message=f"Set {APP_BASE_URL_ENV_VAR} before issuing sign-in links.",
                    status_code=503,
                )
            )
        try:
            normalized = normalize_email(request.email)
        except ValueError as exc:
            raise _http_error(
                HostedMcpError(code="account_login_invalid_email", message=str(exc), status_code=400)
            ) from exc
        raw_token = new_login_token()
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=login_token_ttl)
        statement = insert_login_token_sql(
            token_id=new_login_token_id(),
            token_hash=login_token_hash(raw_token),
            normalized_email=normalized,
            name=_optional_text(request.name),
            workspace_name=_optional_text(request.workspace_name),
            redirect_path=_safe_redirect_path(request.redirect_path),
            expires_at=expires_at,
            user_agent=(user_agent or None),
        )
        billing_connection.execute(statement.sql, statement.params)
        verify_link = f"{resolved_app_base_url}/auth/verify?token={quote(raw_token, safe='')}"
        minutes = max(1, login_token_ttl // 60)
        message = EmailMessage(
            to=normalized,
            subject="Your Yutome sign-in link",
            text=(
                "Click to sign in to Yutome:\n\n"
                f"{verify_link}\n\n"
                f"This link expires in {minutes} minutes and can be used once. "
                "If you didn't request it, you can ignore this email."
            ),
        )
        try:
            resolved_email_sender.send(message)
        except EmailSendError as exc:
            raise _http_error(
                HostedMcpError(
                    code="account_login_email_failed",
                    message="Could not send the sign-in email. Please try again.",
                    status_code=502,
                )
            ) from exc
        response: dict[str, Any] = {"ok": True, "email": normalized, "email_sent": True}
        if resolved_dev_return_login_link:
            response["verify_link"] = verify_link
        return response

    @app.post("/account/login/verify")
    def account_login_verify(
        request: AccountLoginVerifyRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _verify_bearer_token_any(
            authorization=authorization,
            expected_tokens=(normalized_api_token, normalized_account_api_token),
        )
        if billing_connection is None:
            raise _http_error(
                HostedMcpError(
                    code="account_login_connection_unconfigured",
                    message="Hosted login requires a database connection.",
                    status_code=503,
                )
            )
        if not normalized_account_session_secret:
            raise _http_error(
                HostedMcpError(
                    code="account_session_signing_unconfigured",
                    message=f"Set {ACCOUNT_SESSION_HMAC_SECRET_ENV_VAR} before creating hosted account sessions.",
                    status_code=503,
                )
            )
        token = request.token.strip()
        invalid = HostedMcpError(
            code="account_login_token_invalid",
            message="This sign-in link is invalid or has expired.",
            status_code=401,
        )
        if not token:
            raise _http_error(invalid)
        now = datetime.now(timezone.utc)
        consume = consume_login_token_sql(token_hash=login_token_hash(token), now=now)
        rows = _rows_from_result(billing_connection.execute(consume.sql, consume.params))
        if not rows:
            raise _http_error(invalid)
        record = rows[0]
        try:
            expires_at = now + timedelta(seconds=account_session_ttl)
            bootstrap_input = AccountBootstrapInput(
                email=str(record.get("normalized_email") or ""),
                name=_optional_text(record.get("name")),
                workspace_name=_optional_text(record.get("workspace_name")),
            )
            session_token = sign_account_session_token(
                user_id=bootstrap_input.user_id,
                workspace_id=bootstrap_input.workspace_id,
                secret=normalized_account_session_secret,
                expires_at=expires_at,
                issued_at=now,
                audience=normalized_account_session_audience,
            )
            session_input = AccountBootstrapInput(
                email=bootstrap_input.email,
                name=bootstrap_input.name,
                workspace_name=bootstrap_input.workspace_name,
                session_token=session_token,
                session_scopes=(contract.AUTH_SCOPE,),
                session_audience=normalized_account_session_audience,
                session_expires_at=expires_at,
            )
            result = _bootstrap_account_in_transaction(billing_connection, session_input)
        except ValueError as exc:
            raise _http_error(HostedMcpError(code="account_login_invalid", message=str(exc), status_code=400)) from exc
        return {
            "ok": True,
            "principal": result.principal.model_dump(mode="json"),
            "session": {
                "token": session_token,
                "expires_at": expires_at.isoformat(),
                "audience": normalized_account_session_audience,
                "cookie_name": ACCOUNT_SESSION_COOKIE_NAME,
                "max_age_seconds": account_session_ttl,
            },
            "redirect_path": _safe_redirect_path(record.get("redirect_path")),
        }

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request) -> dict[str, Any]:
        if not normalized_stripe_webhook_secret:
            raise _http_error(
                HostedMcpError(
                    code="stripe_webhook_secret_unconfigured",
                    message=f"Set {STRIPE_WEBHOOK_SECRET_ENV_VAR} before accepting Stripe webhooks.",
                    status_code=503,
                )
            )
        if billing_connection is None:
            raise _http_error(
                HostedMcpError(
                    code="billing_connection_unconfigured",
                    message="Hosted billing webhook processing requires a database connection.",
                    status_code=503,
                )
            )
        raw_body = await request.body()
        try:
            verify_stripe_webhook_signature(
                raw_body=raw_body,
                header=request.headers.get("stripe-signature"),
                secret=normalized_stripe_webhook_secret,
            )
        except StripeWebhookVerificationError as exc:
            raise _http_error(
                HostedMcpError(
                    code=str(exc),
                    message="Invalid Stripe webhook signature.",
                    status_code=401,
                )
            ) from exc
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise _http_error(
                HostedMcpError(
                    code="stripe_webhook_invalid_json",
                    message="Stripe webhook body must be valid JSON.",
                    status_code=400,
                )
            ) from exc
        result = process_stripe_webhook_event(payload)
        await run_in_threadpool(
            _execute_billing_statements,
            billing_connection,
            stripe_webhook_processing_statements(result),
        )
        return {
            "ok": True,
            "event_id": result.event.id,
            "event_type": result.event.type,
            "stripe_customer": result.stripe_customer.id if result.stripe_customer else None,
            "ignored": result.ignored,
        }

    def _load_stripe_customer_row(workspace_id: str) -> Mapping[str, Any] | None:
        statement = load_stripe_customer_sql(workspace_id=workspace_id)
        rows = _rows_from_result(billing_connection.execute(statement.sql, statement.params))
        return rows[0] if rows else None

    def _get_or_create_stripe_customer(*, workspace_id: str, user_id: str) -> str:
        existing = _load_stripe_customer_row(workspace_id)
        if existing is not None and existing.get("stripe_customer_id"):
            return str(existing["stripe_customer_id"])
        created = _stripe_post(
            "/v1/customers",
            {"metadata[workspace_id]": workspace_id, "metadata[user_id]": user_id},
        )
        external_id = str(created.get("id") or "")
        if not external_id:
            raise _http_error(
                HostedMcpError(
                    code="stripe_customer_create_failed",
                    message="Stripe did not return a customer id.",
                    status_code=502,
                )
            )
        customer = _StripeCustomer(
            id=_build_stripe_customer_id(stripe_customer_id=external_id),
            workspace_id=workspace_id,
            stripe_customer_id=external_id,
            subscription_status="none",
            metadata={"created_via": "billing_checkout"},
        )
        statement = upsert_stripe_customer_sql(customer)
        billing_connection.execute(statement.sql, statement.params)
        return external_id

    def account_auth_dependency(
        authorization: str | None = Header(default=None),
        account_session: str | None = Header(default=None, alias=ACCOUNT_SESSION_TOKEN_HEADER),
    ) -> AccountApiContext:
        # Dashboard reads use a SEPARATE narrow credential (not the MCP query
        # token) and derive the tenant from the verified session token the BFF
        # forwards, never from a client-supplied workspace header.
        if not normalized_account_api_token:
            raise _http_error(
                HostedMcpError(
                    code="account_api_token_unconfigured",
                    message=f"Set {DASHBOARD_TOKEN_ENV_VAR} before serving hosted account reads.",
                    status_code=503,
                )
            )
        _verify_bearer_token(authorization=authorization, expected_api_token=normalized_account_api_token)
        if not normalized_account_session_secret:
            raise _http_error(
                HostedMcpError(
                    code="account_session_signing_unconfigured",
                    message=f"Set {ACCOUNT_SESSION_HMAC_SECRET_ENV_VAR} before reading hosted account state.",
                    status_code=503,
                )
            )
        if billing_connection is None:
            raise _http_error(
                HostedMcpError(
                    code="account_read_connection_unconfigured",
                    message="Hosted account reads require a database connection.",
                    status_code=503,
                )
            )
        if account_session is None or not account_session.strip():
            raise _http_error(
                HostedMcpError(
                    code="account_session_required",
                    message=f"Missing required {ACCOUNT_SESSION_TOKEN_HEADER} token.",
                    status_code=401,
                )
            )
        account_session_token = account_session.strip()
        try:
            claims = verify_account_session_token(
                account_session_token,
                secret=normalized_account_session_secret,
                audience=normalized_account_session_audience,
                max_age_seconds=account_session_ttl,
            )
        except AccountSessionError as exc:
            raise _http_error(HostedMcpError(code=exc.code, message=exc.message, status_code=exc.status_code)) from exc
        session_hash = session_token_hash(account_session_token)
        statement = load_account_session_sql(session_hash=session_hash)
        session_rows = _rows_from_result(billing_connection.execute(statement.sql, statement.params))
        if session_rows:
            session_row = session_rows[0]
            if session_row.get("status") == "revoked" or session_row.get("revoked_at") is not None:
                raise _http_error(
                    HostedMcpError(
                        code="account_session_revoked",
                        message="Account session has been revoked.",
                        status_code=401,
                    )
                )
        workspace = load_active_workspace(billing_connection, workspace_id=claims.workspace_id)
        if workspace is None:
            raise _http_error(
                HostedMcpError(code="workspace_not_found", message="Workspace not found.", status_code=404)
            )
        return AccountApiContext(
            workspace_id=claims.workspace_id,
            user_id=claims.user_id,
            workspace_name=_optional_text(workspace.get("name")),
        )

    @app.post("/billing/checkout")
    def billing_checkout(context: AccountApiContext = Depends(account_auth_dependency)) -> dict[str, Any]:
        seat_price_id = _stripe_env(STRIPE_SEAT_PRICE_ID_ENV_VAR)
        overage_price_id = _stripe_env(STRIPE_PRICE_ID_ENV_VAR)
        success_url = _stripe_env(STRIPE_CHECKOUT_SUCCESS_URL_ENV_VAR)
        cancel_url = _stripe_env(STRIPE_CHECKOUT_CANCEL_URL_ENV_VAR)
        customer_external_id = _get_or_create_stripe_customer(
            workspace_id=context.workspace_id, user_id=context.user_id
        )
        # Personal plan = flat $4 seat (licensed Price, quantity 1) + metered overage credits
        # (metered Price, quantity OMITTED — Stripe errors if it is sent). 14-day card-gated
        # trial via subscription_data[trial_period_days].
        session = _stripe_post(
            "/v1/checkout/sessions",
            {
                "mode": "subscription",
                "customer": customer_external_id,
                "line_items[0][price]": seat_price_id,
                "line_items[0][quantity]": "1",
                "line_items[1][price]": overage_price_id,
                "subscription_data[trial_period_days]": str(int(TRIAL_PERIOD_DAYS)),
                "success_url": success_url,
                "cancel_url": cancel_url,
                "client_reference_id": context.workspace_id,
                "subscription_data[metadata][workspace_id]": context.workspace_id,
            },
        )
        url = _optional_text(session.get("url"))
        if url is None:
            raise _http_error(
                HostedMcpError(
                    code="stripe_checkout_session_failed",
                    message="Stripe did not return a checkout session url.",
                    status_code=502,
                )
            )
        return {"ok": True, "url": url}

    @app.post("/billing/portal")
    def billing_portal(context: AccountApiContext = Depends(account_auth_dependency)) -> dict[str, Any]:
        return_url = _stripe_env(STRIPE_PORTAL_RETURN_URL_ENV_VAR)
        existing = _load_stripe_customer_row(context.workspace_id)
        if existing is None or not existing.get("stripe_customer_id"):
            raise _http_error(
                HostedMcpError(
                    code="stripe_customer_not_found",
                    message="Subscribe via /billing/checkout before opening the customer portal.",
                    status_code=409,
                )
            )
        session = _stripe_post(
            "/v1/billing_portal/sessions",
            {"customer": str(existing["stripe_customer_id"]), "return_url": return_url},
        )
        url = _optional_text(session.get("url"))
        if url is None:
            raise _http_error(
                HostedMcpError(
                    code="stripe_portal_session_failed",
                    message="Stripe did not return a customer portal url.",
                    status_code=502,
                )
            )
        return {"ok": True, "url": url}

    def cli_auth_dependency(authorization: str | None = Header(default=None)) -> AccountCliApiContext:
        if billing_connection is None:
            raise _http_error(
                HostedMcpError(
                    code="account_cli_connection_unconfigured",
                    message="Hosted CLI account APIs require a database connection.",
                    status_code=503,
                )
            )
        if not normalized_account_session_secret:
            raise _http_error(
                HostedMcpError(
                    code="cli_token_signing_unconfigured",
                    message=f"Set {ACCOUNT_SESSION_HMAC_SECRET_ENV_VAR} before accepting hosted CLI tokens.",
                    status_code=503,
                )
            )
        try:
            claims = verify_cli_token(
                _bearer_token(authorization),
                secret=normalized_account_session_secret,
                audience=DEFAULT_CLI_AUDIENCE,
            )
        except AccountSessionError as exc:
            raise _http_error(HostedMcpError(code=exc.code, message=exc.message, status_code=exc.status_code)) from exc
        statement = load_cli_grant_by_id_sql(grant_id=claims.grant_id)
        rows = _rows_from_result(billing_connection.execute(statement.sql, statement.params))
        grant = rows[0] if rows else None
        if grant is None:
            raise _http_error(
                HostedMcpError(code="cli_grant_not_found", message="Hosted CLI grant not found.", status_code=401)
            )
        if str(grant.get("kind") or "") != "cli_install":
            raise _http_error(
                HostedMcpError(code="cli_grant_kind_invalid", message="Hosted CLI grant is invalid.", status_code=401)
            )
        if str(grant.get("status") or "") != "active" or grant.get("revoked_at") is not None:
            raise _http_error(
                HostedMcpError(code="cli_grant_inactive", message="Hosted CLI grant is not active.", status_code=401)
            )
        expires_at = _row_datetime(grant.get("expires_at"))
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            raise _http_error(
                HostedMcpError(code="cli_grant_expired", message="Hosted CLI grant has expired.", status_code=401)
            )
        grant_version = _row_int(grant.get("token_version"))
        if grant_version is None or grant_version != claims.token_version:
            raise _http_error(
                HostedMcpError(
                    code="cli_token_version_mismatch", message="Hosted CLI token has been superseded.", status_code=401
                )
            )
        if (
            str(grant.get("user_id") or "") != claims.user_id
            or str(grant.get("workspace_id") or "") != claims.workspace_id
        ):
            raise _http_error(
                HostedMcpError(
                    code="cli_token_identity_mismatch", message="Hosted CLI token identity mismatch.", status_code=401
                )
            )
        workspace = load_active_workspace(billing_connection, workspace_id=claims.workspace_id)
        if workspace is None:
            raise _http_error(
                HostedMcpError(code="workspace_not_found", message="Workspace not found.", status_code=404)
            )
        used_statement = mark_cli_grant_used_sql(grant_id=claims.grant_id)
        billing_connection.execute(used_statement.sql, used_statement.params)
        return AccountCliApiContext(
            workspace_id=claims.workspace_id,
            user_id=claims.user_id,
            grant_id=claims.grant_id,
            scopes=set(_row_scopes(grant.get("scopes"))) or set(claims.scopes),
            client_id=claims.client_id,
            install_id=claims.install_id,
        )

    @app.post("/account/cli/authorize")
    def account_cli_authorize(
        request: AccountCliAuthorizeRequest,
        context: AccountApiContext = Depends(account_auth_dependency),
    ) -> dict[str, Any]:
        if billing_connection is None:
            raise _http_error(
                HostedMcpError(
                    code="account_cli_connection_unconfigured",
                    message="Hosted CLI authorization requires a database connection.",
                    status_code=503,
                )
            )
        if request.code_challenge_method.upper() != "S256":
            raise _http_error(
                HostedMcpError(
                    code="cli_pkce_method_invalid", message="Hosted CLI login requires PKCE S256.", status_code=400
                )
            )
        code_challenge = request.code_challenge.strip()
        if len(code_challenge) < 32:
            raise _http_error(
                HostedMcpError(
                    code="cli_code_challenge_invalid", message="PKCE code challenge is too short.", status_code=400
                )
            )
        redirect_uri = _validate_loopback_redirect_uri(request.redirect_uri)
        scopes = _normalize_cli_scopes(request.scopes)
        issued_code = new_authorization_code()
        issued_code_hash = code_hash(issued_code)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=CLI_AUTH_CODE_TTL_SECONDS)
        statement = create_pending_cli_grant_sql(
            code_hash_value=issued_code_hash,
            code_challenge=code_challenge,
            redirect_uri=redirect_uri,
            user_id=context.user_id,
            workspace_id=context.workspace_id,
            scopes=scopes,
            client_id=_optional_text(request.client_id) or DEFAULT_CLI_CLIENT_ID,
            expires_at=expires_at,
            state=_optional_text(request.state),
        )
        rows = _rows_from_result(billing_connection.execute(statement.sql, statement.params))
        grant_id = str((rows[0] if rows else {}).get("id") or statement.params["id"])
        return {
            "ok": True,
            "code": issued_code,
            "grant_id": grant_id,
            "workspace_id": context.workspace_id,
            "scopes": list(scopes),
            "expires_at": expires_at.isoformat(),
            "state": request.state,
        }

    @app.post("/account/cli/token")
    def account_cli_token(request: AccountCliTokenRequest) -> dict[str, Any]:
        if billing_connection is None:
            raise _http_error(
                HostedMcpError(
                    code="account_cli_connection_unconfigured",
                    message="Hosted CLI token exchange requires a database connection.",
                    status_code=503,
                )
            )
        if not normalized_account_session_secret:
            raise _http_error(
                HostedMcpError(
                    code="cli_token_signing_unconfigured",
                    message=f"Set {ACCOUNT_SESSION_HMAC_SECRET_ENV_VAR} before issuing hosted CLI tokens.",
                    status_code=503,
                )
            )
        try:
            request_code_hash = code_hash(request.code)
            expected_challenge = code_challenge_for_verifier(request.code_verifier)
        except ValueError as exc:
            raise _http_error(
                HostedMcpError(code="cli_token_request_invalid", message=str(exc), status_code=400)
            ) from exc
        load_statement = load_cli_grant_by_code_hash_sql(code_hash_value=request_code_hash)
        rows = _rows_from_result(billing_connection.execute(load_statement.sql, load_statement.params))
        grant = rows[0] if rows else None
        if grant is None:
            raise _http_error(
                HostedMcpError(
                    code="cli_authorization_code_invalid",
                    message="Hosted CLI authorization code is invalid.",
                    status_code=401,
                )
            )
        if str(grant.get("status") or "") != "pending":
            raise _http_error(
                HostedMcpError(
                    code="cli_authorization_code_replayed",
                    message="Hosted CLI authorization code was already used.",
                    status_code=401,
                )
            )
        code_expires_at = _row_datetime(grant.get("expires_at"))
        if code_expires_at is not None and code_expires_at <= datetime.now(timezone.utc):
            raise _http_error(
                HostedMcpError(
                    code="cli_authorization_code_expired",
                    message="Hosted CLI authorization code has expired.",
                    status_code=401,
                )
            )
        metadata = _json_object(grant.get("metadata_json"))
        if metadata.get("redirect_uri") != request.redirect_uri:
            raise _http_error(
                HostedMcpError(
                    code="cli_redirect_uri_mismatch", message="Hosted CLI redirect URI does not match.", status_code=401
                )
            )
        if metadata.get("code_challenge") != expected_challenge:
            raise _http_error(
                HostedMcpError(
                    code="cli_pkce_verifier_invalid", message="Hosted CLI PKCE verifier is invalid.", status_code=401
                )
            )
        token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=CLI_TOKEN_TTL_SECONDS)
        install_id = new_install_id()
        activate_statement = activate_pending_cli_grant_sql(
            grant_id=str(grant["id"]),
            code_hash_value=request_code_hash,
            install_id=install_id,
            token_expires_at=token_expires_at,
            metadata={"redirect_uri": request.redirect_uri},
        )
        activated_rows = _rows_from_result(
            billing_connection.execute(activate_statement.sql, activate_statement.params)
        )
        activated = activated_rows[0] if activated_rows else None
        if activated is None:
            raise _http_error(
                HostedMcpError(
                    code="cli_authorization_code_replayed",
                    message="Hosted CLI authorization code was already used.",
                    status_code=401,
                )
            )
        scopes = _row_scopes(activated.get("scopes")) or DEFAULT_CLI_SCOPES
        token = sign_cli_token(
            user_id=str(activated.get("user_id") or grant["user_id"]),
            workspace_id=str(activated.get("workspace_id") or grant["workspace_id"]),
            grant_id=str(activated.get("id") or grant["id"]),
            scopes=scopes,
            secret=normalized_account_session_secret,
            expires_at=token_expires_at,
            audience=str(activated.get("audience") or DEFAULT_CLI_AUDIENCE),
            client_id=str(activated.get("client_id") or DEFAULT_CLI_CLIENT_ID),
            install_id=str(activated.get("install_id") or install_id),
            token_version=_row_int(activated.get("token_version")) or 1,
        )
        return {
            "ok": True,
            "access_token": token,
            "token_type": "Bearer",
            "expires_at": token_expires_at.isoformat(),
            "workspace_id": str(activated.get("workspace_id") or grant["workspace_id"]),
            "grant_id": str(activated.get("id") or grant["id"]),
            "scopes": list(scopes),
        }

    def source_import_response(request: AccountSourcesImportRequest, actor: AccountSourceImportActor) -> dict[str, Any]:
        try:
            return import_sources(billing_connection, request=request, actor=actor)
        except HostedSourceImportError as exc:
            raise _http_error(
                HostedMcpError(code=exc.code, message=exc.message, status_code=exc.status_code, data=exc.data)
            ) from exc

    @app.post("/account/sources")
    def account_sources_create(
        request: AccountSourcesImportRequest,
        context: AccountApiContext = Depends(account_auth_dependency),
    ) -> dict[str, Any]:
        return source_import_response(
            request,
            AccountSourceImportActor(
                workspace_id=context.workspace_id,
                user_id=context.user_id,
                seeded_by="dashboard",
            ),
        )

    @app.post("/account/sources/import")
    def account_sources_import(
        request: AccountSourcesImportRequest,
        context: AccountCliApiContext = Depends(cli_auth_dependency),
    ) -> dict[str, Any]:
        _require_cli_scope(context, CLI_SOURCE_WRITE_SCOPE)
        _require_cli_scope(context, CLI_JOB_WRITE_SCOPE)
        return source_import_response(
            request,
            AccountSourceImportActor(
                workspace_id=context.workspace_id,
                user_id=context.user_id,
                seeded_by="hosted_cli",
                cli_grant_id=context.grant_id,
            ),
        )

    @app.get("/account/youtube/status")
    def account_youtube_status(context: AccountApiContext = Depends(account_auth_dependency)) -> dict[str, Any]:
        return youtube_connection_status(
            billing_connection,
            workspace_id=context.workspace_id,
            user_id=context.user_id,
            configured=resolved_youtube_oauth_settings.configured,
        )

    @app.post("/account/youtube/authorize")
    def account_youtube_authorize(
        request: AccountYouTubeAuthorizeRequest,
        context: AccountApiContext = Depends(account_auth_dependency),
    ) -> dict[str, Any]:
        try:
            return start_youtube_authorization(
                billing_connection,
                settings=resolved_youtube_oauth_settings,
                workspace_id=context.workspace_id,
                user_id=context.user_id,
                redirect_uri=request.redirect_uri,
                state_secret=normalized_account_session_secret or "",
            )
        except HostedYouTubeOAuthError as exc:
            raise _youtube_oauth_http_error(exc) from exc

    @app.post("/account/youtube/callback")
    def account_youtube_callback(
        request: AccountYouTubeCallbackRequest,
        context: AccountApiContext = Depends(account_auth_dependency),
    ) -> dict[str, Any]:
        try:
            return complete_youtube_authorization(
                billing_connection,
                settings=resolved_youtube_oauth_settings,
                workspace_id=context.workspace_id,
                user_id=context.user_id,
                code=request.code,
                state=request.state,
                redirect_uri=request.redirect_uri,
                state_secret=normalized_account_session_secret or "",
            )
        except HostedYouTubeOAuthError as exc:
            raise _youtube_oauth_http_error(exc) from exc

    @app.get("/account/youtube/subscriptions")
    def account_youtube_subscriptions(
        limit: int = 250,
        context: AccountApiContext = Depends(account_auth_dependency),
    ) -> dict[str, Any]:
        try:
            return list_youtube_subscription_channels(
                billing_connection,
                settings=resolved_youtube_oauth_settings,
                workspace_id=context.workspace_id,
                user_id=context.user_id,
                limit=limit,
            )
        except HostedYouTubeOAuthError as exc:
            raise _youtube_oauth_http_error(exc) from exc

    @app.post("/account/youtube/subscriptions/import")
    def account_youtube_subscriptions_import(
        request: AccountYouTubeSubscriptionsImportRequest,
        context: AccountApiContext = Depends(account_auth_dependency),
    ) -> dict[str, Any]:
        try:
            return import_youtube_subscription_channels(
                billing_connection,
                settings=resolved_youtube_oauth_settings,
                workspace_id=context.workspace_id,
                user_id=context.user_id,
                channel_ids=request.channel_ids,
                refresh_enabled=request.refresh_enabled,
                max_new_videos=request.max_new_videos,
                cadence_seconds=request.cadence_seconds,
            )
        except HostedYouTubeOAuthError as exc:
            raise _youtube_oauth_http_error(exc) from exc

    @app.post("/account/youtube/revoke")
    def account_youtube_revoke(context: AccountApiContext = Depends(account_auth_dependency)) -> dict[str, Any]:
        try:
            return revoke_youtube_connection(
                billing_connection,
                workspace_id=context.workspace_id,
                user_id=context.user_id,
            )
        except HostedYouTubeOAuthError as exc:
            raise _youtube_oauth_http_error(exc) from exc

    @app.post("/account/session/revoke")
    def account_session_revoke(
        context: AccountApiContext = Depends(account_auth_dependency),
        account_session: str | None = Header(default=None, alias=ACCOUNT_SESSION_TOKEN_HEADER),
    ) -> dict[str, Any]:
        if account_session is None or not account_session.strip():
            raise _http_error(
                HostedMcpError(
                    code="account_session_required",
                    message=f"Missing required {ACCOUNT_SESSION_TOKEN_HEADER} token.",
                    status_code=401,
                )
            )
        statement = revoke_account_session_sql(
            session_hash=session_token_hash(account_session.strip()),
            now=datetime.now(timezone.utc),
        )
        rows = _rows_from_result(billing_connection.execute(statement.sql, statement.params))
        return {"ok": True, "revoked": bool(rows), "session_id": str(rows[0]["id"]) if rows else None}

    @app.get("/account/source-jobs")
    def account_source_jobs(
        limit: int = 25,
        context: AccountApiContext = Depends(account_auth_dependency),
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        return list_source_jobs(billing_connection, workspace_id=context.workspace_id, limit=limit)

    @app.get("/account/jobs")
    def account_jobs(
        limit: int = 25,
        context: AccountCliApiContext = Depends(cli_auth_dependency),
    ) -> dict[str, Any]:
        _require_cli_scope(context, CLI_ACCOUNT_READ_SCOPE)
        _require_cli_scope(context, CLI_LIBRARY_READ_SCOPE)
        limit = max(1, min(limit, 100))
        return list_source_jobs(billing_connection, workspace_id=context.workspace_id, limit=limit)

    @app.get("/account/summary")
    def account_summary(context: AccountApiContext = Depends(account_auth_dependency)) -> dict[str, Any]:
        summary = read_workspace_summary(billing_connection, workspace_id=context.workspace_id)
        return {"ok": True, **summary.model_dump(mode="json")}

    @app.get("/account/library")
    def account_library(context: AccountApiContext = Depends(account_auth_dependency)) -> dict[str, Any]:
        overview = read_library_overview(billing_connection, workspace_id=context.workspace_id)
        return {"ok": True, **overview.model_dump(mode="json")}

    @app.get("/account/assistants")
    def account_assistants(context: AccountApiContext = Depends(account_auth_dependency)) -> dict[str, Any]:
        assistants = read_active_account_grants(billing_connection, workspace_id=context.workspace_id)
        return {"ok": True, "assistants": [item.model_dump(mode="json") for item in assistants]}

    @app.post("/account/search")
    def account_search(
        request: AccountSearchRequest,
        context: AccountApiContext = Depends(account_auth_dependency),
    ) -> dict[str, Any]:
        # Session-authenticated retrieval for the dashboard. Reuses the same
        # adapter as the MCP query path; the agent-facing /tools/call contract is
        # untouched. Tenant scope comes from the session, never from arguments.
        try:
            result = adapter.call_tool(
                auth=_account_query_auth(context),
                name="find",
                arguments=request.model_dump(exclude_none=True, by_alias=True),
            )
        except HostedMcpError as exc:
            raise _http_error(exc) from exc
        return {"ok": True, "result": result}

    @app.post("/account/show")
    def account_show(
        request: AccountShowRequest,
        context: AccountApiContext = Depends(account_auth_dependency),
    ) -> dict[str, Any]:
        try:
            result = adapter.call_tool(
                auth=_account_query_auth(context),
                name="show",
                arguments=request.model_dump(exclude_none=True),
            )
        except HostedMcpError as exc:
            raise _http_error(exc) from exc
        return {"ok": True, "result": result}

    @app.post("/account/list")
    def account_list(
        request: AccountListRequest,
        context: AccountApiContext = Depends(account_auth_dependency),
    ) -> dict[str, Any]:
        try:
            result = adapter.call_tool(
                auth=_account_query_auth(context),
                name="list",
                arguments=request.model_dump(exclude_none=True),
            )
        except HostedMcpError as exc:
            raise _http_error(exc) from exc
        return {"ok": True, "result": result}

    return app


def close_hosted_connection(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if callable(close):
        close()


def _parse_scopes(scopes_header: str | None) -> set[str]:
    if scopes_header is None:
        return {contract.AUTH_SCOPE}
    return {scope for scope in scopes_header.replace(",", " ").split() if scope}


def _account_query_auth(context: AccountApiContext) -> HostedMcpAuthContext:
    """Build a query auth context from a verified dashboard session. The workspace
    and user come from the session claims; the adapter's `.validated()` enforces
    workspace identity and the required scope.

    `dashboard_read=True` exempts these BFF reads from the trial-expiry read-only deny so the
    existing corpus stays readable after a trial ends; the agent-facing /mcp/tools/call path
    builds its auth context without this flag and is denied."""

    return HostedMcpAuthContext(
        workspace_id=context.workspace_id,
        scopes=frozenset({contract.AUTH_SCOPE}),
        user_id=context.user_id,
        dashboard_read=True,
    ).validated()


def _api_token_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    return _normalize_api_token(env.get(TOKEN_ENV_VAR))


def _dashboard_api_token_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    return _normalize_api_token(env.get(DASHBOARD_TOKEN_ENV_VAR))


def _stripe_webhook_secret_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    return _normalize_api_token(env.get(STRIPE_WEBHOOK_SECRET_ENV_VAR))


def _stripe_env(name: str) -> str:
    value = _normalize_api_token(os.environ.get(name))
    if value is None:
        raise _http_error(
            HostedMcpError(
                code="stripe_configuration_missing",
                message=f"Set {name} before serving Stripe billing routes.",
                status_code=503,
            )
        )
    return value


def _stripe_post(path: str, form: Mapping[str, str]) -> dict[str, Any]:
    import urllib.error
    import urllib.parse
    import urllib.request

    secret_key = _stripe_env(STRIPE_SECRET_KEY_ENV_VAR)
    api_base = (_normalize_api_token(os.environ.get(STRIPE_API_BASE_ENV_VAR)) or "https://api.stripe.com").rstrip("/")
    body = urllib.parse.urlencode({key: value for key, value in form.items() if value is not None}).encode("utf-8")
    request = urllib.request.Request(
        f"{api_base}{path}",
        data=body,
        headers={
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "yutome-hosted-billing/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            text = response.read().decode("utf-8")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        raise _http_error(
            HostedMcpError(
                code="stripe_api_error",
                message=f"Stripe API call failed with HTTP {exc.code}: {error_text[:300]}",
                status_code=502,
            )
        ) from exc


def _account_session_secret_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    return _normalize_api_token(env.get(ACCOUNT_SESSION_HMAC_SECRET_ENV_VAR))


def _account_session_audience_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    return _normalize_api_token(env.get(ACCOUNT_SESSION_AUDIENCE_ENV_VAR))


def _account_session_ttl_seconds_from_env(environ: Mapping[str, str] | None = None) -> int | None:
    env = os.environ if environ is None else environ
    raw = _normalize_api_token(env.get(ACCOUNT_SESSION_MAX_AGE_SECONDS_ENV_VAR))
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _normalize_api_token(token: str | None) -> str | None:
    return token.strip() if token and token.strip() else None


def _app_base_url_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    return _normalize_api_token(env.get(APP_BASE_URL_ENV_VAR))


def _login_token_ttl_seconds_from_env(environ: Mapping[str, str] | None = None) -> int | None:
    env = os.environ if environ is None else environ
    raw = _normalize_api_token(env.get(LOGIN_TOKEN_TTL_SECONDS_ENV_VAR))
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _rate_limit_rpm_from_env(environ: Mapping[str, str] | None = None) -> int | None:
    env = os.environ if environ is None else environ
    raw = _normalize_api_token(env.get(RATE_LIMIT_RPM_ENV_VAR))
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _auth_dev_return_link_from_env(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    raw = (env.get(AUTH_DEV_RETURN_LINK_ENV_VAR) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _safe_redirect_path(value: Any) -> str | None:
    """Accept only internal, single-slash-rooted paths so a magic link can't be
    coerced into an open redirect (`//host`, `https://host`, etc.)."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if (
        not trimmed
        or _CONTROL_CHARACTERS.search(trimmed)
        or "\\" in trimmed
        or not trimmed.startswith("/")
        or trimmed.startswith("//")
    ):
        return None
    try:
        parts = urlsplit(trimmed)
    except ValueError:
        return None
    if (
        parts.scheme
        or parts.netloc
        or not parts.path.startswith("/")
        or parts.path.startswith("//")
        or _ENCODED_SLASH_OR_BACKSLASH.search(parts.path)
    ):
        return None
    return trimmed


def _verify_bearer_token(*, authorization: str | None, expected_api_token: str | None) -> None:
    if not expected_api_token:
        raise _http_error(
            HostedMcpError(
                code="api_token_unconfigured",
                message=f"Set {TOKEN_ENV_VAR} before serving hosted MCP tools or resources.",
                status_code=503,
            )
        )
    if authorization is None or not authorization.strip():
        raise _http_error(
            HostedMcpError(
                code="api_token_required",
                message="Missing required Authorization bearer token.",
                status_code=401,
            )
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _http_error(
            HostedMcpError(
                code="api_token_required",
                message="Missing required Authorization bearer token.",
                status_code=401,
            )
        )
    if not secrets.compare_digest(token.strip(), expected_api_token):
        raise _http_error(
            HostedMcpError(
                code="api_token_invalid",
                message="Invalid Authorization bearer token.",
                status_code=401,
            )
        )


def _bearer_token_or_none(authorization: str | None) -> str | None:
    if authorization is None or not authorization.strip():
        return None
    scheme, _, token = authorization.partition(" ")
    candidate = token.strip()
    if scheme.lower() != "bearer" or not candidate:
        return None
    return candidate


def _rate_limit_client_ip(request: Any) -> str | None:
    forwarded_ip = _client_ip_from_x_forwarded_for(request.headers.get("x-forwarded-for"))
    if forwarded_ip is not None:
        return forwarded_ip
    return request.client.host if request.client is not None else None


def _client_ip_from_x_forwarded_for(value: str | None) -> str | None:
    raw = _optional_text(value)
    if raw is None:
        return None
    # Railway's trusted edge appends the real connecting client to X-Forwarded-For.
    # Any earlier entries may have been supplied by the caller, so the limiter
    # keys on the proxy-inserted right-most hop only.
    candidate = next((part.strip() for part in reversed(raw.split(",")) if part.strip()), None)
    if candidate is None:
        return None
    return _normalize_forwarded_ip(candidate)


def _normalize_forwarded_ip(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith("["):
        host, separator, _rest = candidate[1:].partition("]")
        if separator:
            candidate = host
    elif candidate.count(":") == 1 and "." in candidate:
        host, port = candidate.rsplit(":", 1)
        if port.isdigit():
            candidate = host
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return candidate


def _rate_limit_key(*, bearer_token: str | None, client_ip: str | None) -> str:
    if bearer_token:
        token_hash = hashlib.sha256(bearer_token.encode("utf-8")).hexdigest()[:16]
        return f"tok:{token_hash}"
    return f"ip:{client_ip or 'unknown'}"


def _rate_limit_headers(decision: Any, *, include_retry_after: bool = False) -> dict[str, str]:
    headers = {
        "RateLimit-Limit": str(decision.limit),
        "RateLimit-Remaining": str(decision.remaining),
        "RateLimit-Reset": str(decision.reset_seconds),
    }
    if include_retry_after and decision.retry_after_seconds is not None:
        headers["Retry-After"] = str(decision.retry_after_seconds)
    return headers


def _audit_hosted_request(
    *,
    request: Any,
    response: Any,
    workspace_id: str | None,
    auth_bearer_present: bool,
    client_ip: str | None,
    rate_limit_outcome: str,
) -> None:
    _AUDIT_LOGGER.info(
        "hosted_http_request",
        extra={
            "http_method": request.method,
            "path": request.url.path,
            "status_code": getattr(response, "status_code", None),
            "workspace_id": workspace_id,
            "auth_bearer_present": auth_bearer_present,
            "client_ip": client_ip,
            "rate_limit_outcome": rate_limit_outcome,
        },
    )


def _verify_bearer_token_any(*, authorization: str | None, expected_tokens: tuple[str | None, ...]) -> None:
    """Accept the bearer token if it matches ANY configured token (constant-time)."""
    configured = [token for token in expected_tokens if token]
    if not configured:
        raise _http_error(
            HostedMcpError(
                code="api_token_unconfigured",
                message=f"Set {TOKEN_ENV_VAR} or {DASHBOARD_TOKEN_ENV_VAR} before serving this endpoint.",
                status_code=503,
            )
        )
    if authorization is None or not authorization.strip():
        raise _http_error(
            HostedMcpError(
                code="api_token_required", message="Missing required Authorization bearer token.", status_code=401
            )
        )
    scheme, _, token = authorization.partition(" ")
    candidate = token.strip()
    if scheme.lower() != "bearer" or not candidate:
        raise _http_error(
            HostedMcpError(
                code="api_token_required", message="Missing required Authorization bearer token.", status_code=401
            )
        )
    if any(secrets.compare_digest(candidate, expected) for expected in configured):
        return
    raise _http_error(
        HostedMcpError(code="api_token_invalid", message="Invalid Authorization bearer token.", status_code=401)
    )


def _bearer_token(authorization: str | None) -> str:
    if authorization is None or not authorization.strip():
        raise AccountSessionError("cli_token_required", "Missing required Authorization bearer token.")
    scheme, _, token = authorization.partition(" ")
    candidate = token.strip()
    if scheme.lower() != "bearer" or not candidate:
        raise AccountSessionError("cli_token_required", "Missing required Authorization bearer token.")
    return candidate


def _validate_loopback_redirect_uri(value: str) -> str:
    uri = value.strip()
    parsed = urlsplit(uri)
    host = parsed.hostname or ""
    if parsed.scheme != "http" or host not in {"127.0.0.1", "localhost", "::1"} or parsed.port is None:
        raise _http_error(
            HostedMcpError(
                code="cli_redirect_uri_invalid",
                message="Hosted CLI redirect URI must be an http:// localhost callback.",
                status_code=400,
            )
        )
    if parsed.fragment:
        raise _http_error(
            HostedMcpError(
                code="cli_redirect_uri_invalid",
                message="Hosted CLI redirect URI must not include a fragment.",
                status_code=400,
            )
        )
    return uri


def _normalize_cli_scopes(scopes: list[str]) -> tuple[str, ...]:
    allowed = set(DEFAULT_CLI_SCOPES)
    requested = tuple(scope.strip() for scope in scopes if scope and scope.strip())
    if not requested:
        return DEFAULT_CLI_SCOPES
    unknown = [scope for scope in requested if scope not in allowed]
    if unknown:
        raise _http_error(
            HostedMcpError(
                code="cli_scope_invalid", message=f"Unsupported hosted CLI scope: {unknown[0]}", status_code=400
            )
        )
    return tuple(dict.fromkeys(requested))


def _require_cli_scope(context: AccountCliApiContext, scope: str) -> None:
    if scope not in context.scopes:
        raise _http_error(
            HostedMcpError(
                code="cli_scope_required", message=f"Hosted CLI grant is missing scope {scope}.", status_code=403
            )
        )


_account_jobs_sql = account_jobs_sql
_job_row_json = job_row_json


def _rows_from_result(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, list):
        return [dict(row) for row in result]
    if hasattr(result, "mappings"):
        return [dict(row) for row in result.mappings().all()]
    if hasattr(result, "fetchall"):
        return [dict(row) for row in result.fetchall()]
    return []


def _row_scopes(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _row_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


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


def _http_error(error: HostedMcpError) -> Exception:
    from fastapi import HTTPException

    return HTTPException(status_code=error.status_code, detail=error.to_dict()["error"])


def _youtube_oauth_http_error(error: HostedYouTubeOAuthError) -> Exception:
    return _http_error(
        HostedMcpError(
            code=error.code,
            message=error.message,
            status_code=error.status_code,
            data=error.data,
        )
    )


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _readiness_exception_payload(_exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": "readiness_check_failed"}


def _sanitize_readiness_payload(value: Any, *, field_name: str | None = None) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _sanitize_readiness_payload(item, field_name=str(key).lower()) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_readiness_payload(item, field_name=field_name) for item in value]
    if isinstance(value, str):
        if field_name in _READINESS_ERROR_FIELDS:
            return value if _SAFE_READINESS_ERROR_CODE.fullmatch(value) else "readiness_check_failed"
        if _looks_sensitive_readiness_string(value):
            return "[redacted]"
    return value


def _looks_sensitive_readiness_string(value: str) -> bool:
    lowered = value.lower()
    return "://" in value or "password" in lowered or "credential" in lowered or "secret" in lowered


def _execute_billing_statements(connection: Any, statements: tuple[Any, ...]) -> None:
    transaction = getattr(connection, "transaction", None)
    if callable(transaction):
        with transaction():
            for statement in statements:
                connection.execute(statement.sql, statement.params)
        return
    for statement in statements:
        connection.execute(statement.sql, statement.params)


def _bootstrap_account_in_transaction(connection: Any, account: AccountBootstrapInput) -> Any:
    transaction = getattr(connection, "transaction", None)
    if callable(transaction):
        with transaction():
            return bootstrap_hosted_account(connection, account)
    return bootstrap_hosted_account(connection, account)


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _positive_int(value: int | None, fallback: int) -> int:
    return value if isinstance(value, int) and value > 0 else fallback


def error_body(response_json: Mapping[str, Any]) -> Mapping[str, Any]:
    detail = response_json.get("detail")
    return detail if isinstance(detail, Mapping) else {}
