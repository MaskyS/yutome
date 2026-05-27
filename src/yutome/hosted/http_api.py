from __future__ import annotations

import os
import re
import secrets
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request

from yutome import contract
from yutome.hosted.account import (
    DEFAULT_ACCOUNT_SESSION_AUDIENCE,
    AccountBootstrapInput,
    AccountSessionError,
    bootstrap_hosted_account,
    sign_account_session_token,
    verify_account_session_token,
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
from yutome.hosted.control_plane import Source, SourceRefreshPolicy
from yutome.hosted.ids import input_hash
from yutome.hosted.indexing import (
    enqueue_discover_source_job_sql,
    enqueue_index_video_job_sql,
    source_from_public_youtube_input,
)
from yutome.hosted.mcp_query import HostedMcpAuthContext, HostedMcpError, HostedMcpQueryAdapter
from yutome.hosted.repositories import SqlStatement
from yutome.hosted.runtime import upsert_hosted_source_sql, upsert_source_refresh_policy_sql
from yutome.hosted.source_registry import hosted_source_id, provider_credentials_in_source


WORKSPACE_HEADER = "X-Yutome-Workspace-Id"
SCOPES_HEADER = "X-Yutome-Scopes"
USER_HEADER = "X-Yutome-User-Id"
GRANT_HEADER = "X-Yutome-Grant-Id"
CLIENT_HEADER = "X-Yutome-Client-Id"
SESSION_HEADER = "X-Yutome-Session-Id"
ACCOUNT_SESSION_TOKEN_HEADER = "X-Yutome-Account-Session"
TOKEN_ENV_VAR = "YUTOME_HOSTED_API_TOKEN"
DASHBOARD_TOKEN_ENV_VAR = "YUTOME_DASHBOARD_API_TOKEN"
POLAR_WEBHOOK_SECRET_ENV_VAR = "POLAR_WEBHOOK_SECRET"
ACCOUNT_SESSION_HMAC_SECRET_ENV_VAR = "YUTOME_ACCOUNT_SESSION_HMAC_SECRET"
ACCOUNT_SESSION_AUDIENCE_ENV_VAR = "YUTOME_ACCOUNT_SESSION_AUDIENCE"
ACCOUNT_SESSION_MAX_AGE_SECONDS_ENV_VAR = "YUTOME_ACCOUNT_SESSION_MAX_AGE_SECONDS"
ACCOUNT_SESSION_COOKIE_NAME = "yutome_account_session"
ACCOUNT_SESSION_TTL_SECONDS = 60 * 60
_SAFE_READINESS_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_READINESS_ERROR_FIELDS = frozenset({"error", "message", "detail"})


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


class AccountSourceImportDescriptor(BaseModel):
    model_config = ConfigDict(extra="allow")

    source_url: str | None = None
    url: str | None = None
    value: str | None = None
    source_type: str | None = None
    display_name: str | None = None
    title: str | None = None
    channel_id: str | None = None
    playlist_id: str | None = None
    video_id: str | None = None
    import_source: str | None = None
    selected: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class AccountSourcesImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[AccountSourceImportDescriptor]
    cadence_seconds: int = Field(default=900, ge=1)
    max_new_videos: int = Field(default=25, ge=1)
    refresh_enabled: bool = True


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


class AccountSourceImportActor(BaseModel):
    """Internal source-import actor derived from either account-session auth or
    hosted CLI auth. This is Yutome auth, not a YouTube OAuth grant."""

    workspace_id: str
    user_id: str
    seeded_by: str
    cli_grant_id: str | None = None


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
    polar_webhook_secret: str | None = None,
    account_session_secret: str | None = None,
    account_session_audience: str | None = None,
    account_session_ttl_seconds: int | None = None,
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
    )
    app = build_app(
        adapter=adapter,
        readiness_check=readiness_check,
        expected_api_token=_normalize_api_token(expected_api_token) or _api_token_from_env(),
        expected_account_api_token=_normalize_api_token(expected_account_api_token) or _dashboard_api_token_from_env(),
        billing_connection=connection,
        polar_webhook_secret=_normalize_api_token(polar_webhook_secret) or _polar_webhook_secret_from_env(),
        account_session_secret=_normalize_api_token(account_session_secret) or _account_session_secret_from_env(),
        account_session_audience=_normalize_api_token(account_session_audience) or _account_session_audience_from_env(),
        account_session_ttl_seconds=account_session_ttl_seconds or _account_session_ttl_seconds_from_env(),
    )
    app.state.hosted_connection = connection
    app.state.hosted_search_store = search_store
    app.state.hosted_adapter = adapter
    return app


def build_app(
    *,
    adapter: HostedMcpQueryAdapter,
    auth_dependency: AuthDependency | None = None,
    readiness_check: ReadinessCheck | None = None,
    expected_api_token: str | None = None,
    expected_account_api_token: str | None = None,
    billing_connection: Any | None = None,
    polar_webhook_secret: str | None = None,
    account_session_secret: str | None = None,
    account_session_audience: str | None = None,
    account_session_ttl_seconds: int | None = None,
) -> Any:
    from fastapi import Depends, FastAPI, Header
    from fastapi.responses import JSONResponse
    from yutome.hosted.billing import (
        PolarWebhookVerificationError,
        polar_webhook_processing_statements,
        process_polar_webhook_payload,
        verify_standard_webhook_signature,
    )

    app = FastAPI(
        title="yutome-hosted-mcp",
        description="Hosted Yutome MCP query API for the Cloudflare MCP edge.",
        version="0.1.0",
    )
    app.state.hosted_adapter = adapter
    app.state.hosted_billing_connection = billing_connection
    normalized_api_token = _normalize_api_token(expected_api_token)
    normalized_account_api_token = _normalize_api_token(expected_account_api_token)
    normalized_polar_webhook_secret = _normalize_api_token(polar_webhook_secret)
    normalized_account_session_secret = _normalize_api_token(account_session_secret)
    normalized_account_session_audience = _normalize_api_token(account_session_audience) or DEFAULT_ACCOUNT_SESSION_AUDIENCE
    account_session_ttl = _positive_int(account_session_ttl_seconds, ACCOUNT_SESSION_TTL_SECONDS)
    app.state.hosted_api_auth_required = True
    app.state.hosted_api_auth_configured = auth_dependency is not None or bool(normalized_api_token)
    app.state.polar_webhook_configured = bool(normalized_polar_webhook_secret)
    app.state.account_session_signing_configured = bool(normalized_account_session_secret)
    app.state.hosted_account_api_configured = bool(normalized_account_api_token)

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
        # Account creation is performed by either the MCP edge worker (MCP token)
        # or the dashboard BFF (separate dashboard token); accept either.
        _verify_bearer_token_any(
            authorization=authorization,
            expected_tokens=(normalized_api_token, normalized_account_api_token),
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

    @app.post("/billing/polar/webhook")
    @app.post("/webhooks/polar")
    async def polar_webhook(request: Request) -> dict[str, Any]:
        if not normalized_polar_webhook_secret:
            raise _http_error(
                HostedMcpError(
                    code="polar_webhook_secret_unconfigured",
                    message=f"Set {POLAR_WEBHOOK_SECRET_ENV_VAR} before accepting Polar webhooks.",
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
            webhook_event_id = verify_standard_webhook_signature(
                raw_body=raw_body,
                headers=dict(request.headers),
                secret=normalized_polar_webhook_secret,
            )
        except PolarWebhookVerificationError as exc:
            raise _http_error(
                HostedMcpError(
                    code=str(exc),
                    message="Invalid Polar webhook signature.",
                    status_code=401,
                )
            ) from exc
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise _http_error(
                HostedMcpError(
                    code="polar_webhook_invalid_json",
                    message="Polar webhook body must be valid JSON.",
                    status_code=400,
                )
            ) from exc
        result = process_polar_webhook_payload(payload, raw_body=raw_body, webhook_event_id=webhook_event_id)
        _execute_billing_statements(billing_connection, polar_webhook_processing_statements(result))
        return {
            "ok": True,
            "event_id": result.snapshot.webhook_event_id,
            "payload_hash": result.snapshot.payload_hash,
            "event_type": result.snapshot.event_type,
            "credit_entries": len(result.credit_entries),
            "billing_customer": result.billing_customer.id if result.billing_customer else None,
            "ignored": result.ignored,
        }

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
        try:
            claims = verify_account_session_token(
                account_session.strip(),
                secret=normalized_account_session_secret,
                audience=normalized_account_session_audience,
                max_age_seconds=account_session_ttl,
            )
        except AccountSessionError as exc:
            raise _http_error(HostedMcpError(code=exc.code, message=exc.message, status_code=exc.status_code)) from exc
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
            raise _http_error(HostedMcpError(code="cli_grant_not_found", message="Hosted CLI grant not found.", status_code=401))
        if str(grant.get("kind") or "") != "cli_install":
            raise _http_error(HostedMcpError(code="cli_grant_kind_invalid", message="Hosted CLI grant is invalid.", status_code=401))
        if str(grant.get("status") or "") != "active" or grant.get("revoked_at") is not None:
            raise _http_error(HostedMcpError(code="cli_grant_inactive", message="Hosted CLI grant is not active.", status_code=401))
        expires_at = _row_datetime(grant.get("expires_at"))
        if expires_at is not None and expires_at <= datetime.now(timezone.utc):
            raise _http_error(HostedMcpError(code="cli_grant_expired", message="Hosted CLI grant has expired.", status_code=401))
        grant_version = _row_int(grant.get("token_version"))
        if grant_version is None or grant_version != claims.token_version:
            raise _http_error(
                HostedMcpError(code="cli_token_version_mismatch", message="Hosted CLI token has been superseded.", status_code=401)
            )
        if str(grant.get("user_id") or "") != claims.user_id or str(grant.get("workspace_id") or "") != claims.workspace_id:
            raise _http_error(HostedMcpError(code="cli_token_identity_mismatch", message="Hosted CLI token identity mismatch.", status_code=401))
        workspace = load_active_workspace(billing_connection, workspace_id=claims.workspace_id)
        if workspace is None:
            raise _http_error(HostedMcpError(code="workspace_not_found", message="Workspace not found.", status_code=404))
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
                HostedMcpError(code="cli_pkce_method_invalid", message="Hosted CLI login requires PKCE S256.", status_code=400)
            )
        code_challenge = request.code_challenge.strip()
        if len(code_challenge) < 32:
            raise _http_error(
                HostedMcpError(code="cli_code_challenge_invalid", message="PKCE code challenge is too short.", status_code=400)
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
            raise _http_error(HostedMcpError(code="cli_token_request_invalid", message=str(exc), status_code=400)) from exc
        load_statement = load_cli_grant_by_code_hash_sql(code_hash_value=request_code_hash)
        rows = _rows_from_result(billing_connection.execute(load_statement.sql, load_statement.params))
        grant = rows[0] if rows else None
        if grant is None:
            raise _http_error(
                HostedMcpError(code="cli_authorization_code_invalid", message="Hosted CLI authorization code is invalid.", status_code=401)
            )
        if str(grant.get("status") or "") != "pending":
            raise _http_error(
                HostedMcpError(code="cli_authorization_code_replayed", message="Hosted CLI authorization code was already used.", status_code=401)
            )
        code_expires_at = _row_datetime(grant.get("expires_at"))
        if code_expires_at is not None and code_expires_at <= datetime.now(timezone.utc):
            raise _http_error(
                HostedMcpError(code="cli_authorization_code_expired", message="Hosted CLI authorization code has expired.", status_code=401)
            )
        metadata = _json_object(grant.get("metadata_json"))
        if metadata.get("redirect_uri") != request.redirect_uri:
            raise _http_error(
                HostedMcpError(code="cli_redirect_uri_mismatch", message="Hosted CLI redirect URI does not match.", status_code=401)
            )
        if metadata.get("code_challenge") != expected_challenge:
            raise _http_error(
                HostedMcpError(code="cli_pkce_verifier_invalid", message="Hosted CLI PKCE verifier is invalid.", status_code=401)
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
        activated_rows = _rows_from_result(billing_connection.execute(activate_statement.sql, activate_statement.params))
        activated = activated_rows[0] if activated_rows else None
        if activated is None:
            raise _http_error(
                HostedMcpError(code="cli_authorization_code_replayed", message="Hosted CLI authorization code was already used.", status_code=401)
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
        if not request.sources:
            raise _http_error(HostedMcpError(code="source_import_empty", message="At least one source is required.", status_code=400))
        if len(request.sources) > 250:
            raise _http_error(HostedMcpError(code="source_import_too_large", message="Import at most 250 sources per request.", status_code=400))

        now = datetime.now(timezone.utc)
        imported: list[dict[str, Any]] = []
        jobs: list[dict[str, Any]] = []
        policies: list[dict[str, Any]] = []
        transaction = getattr(billing_connection, "transaction", None)
        manager = transaction() if callable(transaction) else None
        if manager is None:
            _execute_source_import(
                billing_connection,
                request=request,
                actor=actor,
                now=now,
                imported=imported,
                jobs=jobs,
                policies=policies,
            )
        else:
            with manager:
                _execute_source_import(
                    billing_connection,
                    request=request,
                    actor=actor,
                    now=now,
                    imported=imported,
                    jobs=jobs,
                    policies=policies,
                )
        return {
            "ok": True,
            "workspace_id": actor.workspace_id,
            "imported": imported,
            "jobs": jobs,
            "refresh_policies": policies,
        }

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

    @app.get("/account/source-jobs")
    def account_source_jobs(
        limit: int = 25,
        context: AccountApiContext = Depends(account_auth_dependency),
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        statement = _account_jobs_sql(workspace_id=context.workspace_id, limit=limit)
        rows = _rows_from_result(billing_connection.execute(statement.sql, statement.params))
        return {"ok": True, "workspace_id": context.workspace_id, "jobs": [_job_row_json(row) for row in rows]}

    @app.get("/account/jobs")
    def account_jobs(
        limit: int = 25,
        context: AccountCliApiContext = Depends(cli_auth_dependency),
    ) -> dict[str, Any]:
        _require_cli_scope(context, CLI_ACCOUNT_READ_SCOPE)
        _require_cli_scope(context, CLI_LIBRARY_READ_SCOPE)
        limit = max(1, min(limit, 100))
        statement = _account_jobs_sql(workspace_id=context.workspace_id, limit=limit)
        rows = _rows_from_result(billing_connection.execute(statement.sql, statement.params))
        return {"ok": True, "workspace_id": context.workspace_id, "jobs": [_job_row_json(row) for row in rows]}

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

    return app


def _parse_scopes(scopes_header: str | None) -> set[str]:
    if scopes_header is None:
        return {contract.AUTH_SCOPE}
    return {scope for scope in scopes_header.replace(",", " ").split() if scope}


def _account_query_auth(context: AccountApiContext) -> HostedMcpAuthContext:
    """Build a query auth context from a verified dashboard session. The workspace
    and user come from the session claims; the adapter's `.validated()` enforces
    workspace identity and the required scope."""

    return HostedMcpAuthContext(
        workspace_id=context.workspace_id,
        scopes=frozenset({contract.AUTH_SCOPE}),
        user_id=context.user_id,
    ).validated()


def _api_token_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    return _normalize_api_token(env.get(TOKEN_ENV_VAR))


def _dashboard_api_token_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    return _normalize_api_token(env.get(DASHBOARD_TOKEN_ENV_VAR))


def _polar_webhook_secret_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    return _normalize_api_token(env.get(POLAR_WEBHOOK_SECRET_ENV_VAR))


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
            HostedMcpError(code="api_token_required", message="Missing required Authorization bearer token.", status_code=401)
        )
    scheme, _, token = authorization.partition(" ")
    candidate = token.strip()
    if scheme.lower() != "bearer" or not candidate:
        raise _http_error(
            HostedMcpError(code="api_token_required", message="Missing required Authorization bearer token.", status_code=401)
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
            HostedMcpError(code="cli_redirect_uri_invalid", message="Hosted CLI redirect URI must not include a fragment.", status_code=400)
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
            HostedMcpError(code="cli_scope_invalid", message=f"Unsupported hosted CLI scope: {unknown[0]}", status_code=400)
        )
    return tuple(dict.fromkeys(requested))


def _require_cli_scope(context: AccountCliApiContext, scope: str) -> None:
    if scope not in context.scopes:
        raise _http_error(
            HostedMcpError(code="cli_scope_required", message=f"Hosted CLI grant is missing scope {scope}.", status_code=403)
        )


def _execute_source_import(
    connection: Any,
    *,
    request: AccountSourcesImportRequest,
    actor: AccountSourceImportActor,
    now: datetime,
    imported: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
    policies: list[dict[str, Any]],
) -> None:
    for descriptor in request.sources:
        payload = descriptor.model_dump(mode="json")
        if _contains_credential_shape(payload):
            raise _http_error(
                HostedMcpError(
                    code="source_import_credentials_rejected",
                    message="Hosted source import accepts public source descriptors only, not provider credentials.",
                    status_code=400,
                )
            )
        source = _source_from_import_descriptor(workspace_id=actor.workspace_id, descriptor=descriptor)
        if provider_credentials_in_source(source):
            raise _http_error(
                HostedMcpError(
                    code="source_import_credentials_rejected",
                    message="Hosted source import accepts public source descriptors only, not provider credentials.",
                    status_code=400,
                )
            )
        source_statement = upsert_hosted_source_sql(source)
        source_rows = _rows_from_result(connection.execute(source_statement.sql, source_statement.params))
        persisted_source = source_rows[0] if source_rows else {}
        imported.append(
            {
                "source_id": str(persisted_source.get("id") or source.id),
                "source_type": str(persisted_source.get("source_type") or source.source_type),
                "source_url": str(persisted_source.get("source_url") or source.source_url),
                "canonical_video_id": source.canonical_video_id,
                "canonical_channel_id": source.canonical_channel_id,
                "canonical_playlist_id": source.canonical_playlist_id,
            }
        )
        if source.canonical_video_id:
            metadata = {"seeded_by": actor.seeded_by, "user_id": actor.user_id}
            if actor.cli_grant_id:
                metadata["cli_grant_id"] = actor.cli_grant_id
            job_statement = enqueue_index_video_job_sql(
                workspace_id=actor.workspace_id,
                source_id=source.id,
                video_id=source.canonical_video_id,
                priority=100,
                now=now,
                metadata=metadata,
            )
            job_rows = _rows_from_result(connection.execute(job_statement.sql, job_statement.params))
            job_row = job_rows[0] if job_rows else {}
            jobs.append(
                {
                    "job_id": str(job_row.get("id") or job_statement.params["id"]),
                    "job_type": str(job_row.get("job_type") or "index_video"),
                    "status": str(job_row.get("status") or "queued"),
                    "source_id": source.id,
                    "youtube_video_id": source.canonical_video_id,
                }
            )
            continue
        policy_id = f"srp_{input_hash({'workspace_id': actor.workspace_id, 'source_id': source.id}, prefix='').lstrip('_')[:24]}"
        policy = SourceRefreshPolicy(
            id=policy_id,
            workspace_id=actor.workspace_id,
            source_id=source.id,
            enabled=request.refresh_enabled,
            cadence_seconds=request.cadence_seconds,
            next_run_at=now,
            max_new_videos_per_run=request.max_new_videos,
        )
        policy_statement = upsert_source_refresh_policy_sql(policy)
        policy_rows = _rows_from_result(connection.execute(policy_statement.sql, policy_statement.params))
        policy_row = policy_rows[0] if policy_rows else {}
        policies.append(
            {
                "refresh_policy_id": str(policy_row.get("id") or policy.id),
                "source_id": source.id,
                "enabled": bool(policy_row.get("enabled") if "enabled" in policy_row else policy.enabled),
                "cadence_seconds": int(policy_row.get("cadence_seconds") or policy.cadence_seconds),
            }
        )
        job_metadata = {
            "seeded_by": actor.seeded_by,
            "user_id": actor.user_id,
            "source_type": source.source_type,
        }
        if actor.cli_grant_id:
            job_metadata["cli_grant_id"] = actor.cli_grant_id
        discover_statement = enqueue_discover_source_job_sql(
            workspace_id=actor.workspace_id,
            source_id=source.id,
            priority=100,
            now=now,
            policy_id=policy.id,
            max_new_videos_per_run=request.max_new_videos,
            trigger="source_import",
            metadata=job_metadata,
        )
        discover_rows = _rows_from_result(connection.execute(discover_statement.sql, discover_statement.params))
        discover_row = discover_rows[0] if discover_rows else {}
        jobs.append(
            {
                "job_id": str(discover_row.get("id") or discover_statement.params["id"]),
                "job_type": str(discover_row.get("job_type") or "discover_source"),
                "status": str(discover_row.get("status") or "queued"),
                "source_id": source.id,
                "youtube_video_id": None,
            }
        )


def _source_from_import_descriptor(*, workspace_id: str, descriptor: AccountSourceImportDescriptor) -> Source:
    value = _descriptor_value(descriptor)
    import_source = _public_import_source(descriptor.import_source)
    display_name = _optional_text(descriptor.display_name) or _optional_text(descriptor.title)
    metadata = dict(descriptor.metadata)
    if descriptor.import_source:
        metadata.setdefault("local_import_source", descriptor.import_source)
    if descriptor.channel_id:
        source_url = descriptor.source_url or descriptor.url or f"https://www.youtube.com/channel/{descriptor.channel_id}"
        return Source(
            id=hosted_source_id(workspace_id=workspace_id, source_url=source_url),
            workspace_id=workspace_id,
            source_type="channel",
            source_url=source_url,
            canonical_channel_id=descriptor.channel_id,
            display_name=display_name or descriptor.channel_id,
            selected=descriptor.selected,
            auto_index_allowed=descriptor.selected,
            import_source=import_source,
            metadata_jsonb=metadata,
        )
    if descriptor.playlist_id and not descriptor.video_id:
        source_url = descriptor.source_url or descriptor.url or f"https://www.youtube.com/playlist?list={descriptor.playlist_id}"
        return Source(
            id=hosted_source_id(workspace_id=workspace_id, source_url=source_url),
            workspace_id=workspace_id,
            source_type="playlist",
            source_url=source_url,
            canonical_playlist_id=descriptor.playlist_id,
            display_name=display_name or descriptor.playlist_id,
            selected=descriptor.selected,
            auto_index_allowed=descriptor.selected,
            import_source=import_source,
            metadata_jsonb=metadata,
        )
    source = source_from_public_youtube_input(
        workspace_id=workspace_id,
        source_id=hosted_source_id(workspace_id=workspace_id, source_url=value),
        value=value,
        import_source=import_source,
        display_name=display_name,
    )
    return source.model_copy(
        update={
            "selected": descriptor.selected,
            "auto_index_allowed": descriptor.selected,
            "metadata_jsonb": metadata,
        }
    )


def _descriptor_value(descriptor: AccountSourceImportDescriptor) -> str:
    for value in (descriptor.source_url, descriptor.url, descriptor.value, descriptor.video_id, descriptor.playlist_id, descriptor.channel_id):
        if value and value.strip():
            return value.strip()
    raise _http_error(HostedMcpError(code="source_import_value_required", message="Source descriptor is missing a URL or id.", status_code=400))


def _public_import_source(raw: str | None) -> str:
    value = (raw or "cli").strip().replace("-", "_")
    if value in {"public_api", "public_scrape", "yt_dlp", "manual_url", "manual", "cli"}:
        return value
    # Local OAuth/cookie subscription imports upload public channel rows for v1;
    # hosted Google/YouTube refresh-token grants are intentionally not created here.
    return "cli"


def _contains_credential_shape(value: Any) -> bool:
    credential_fragments = ("api_key", "apikey", "access_token", "refresh_token", "client_secret", "secret", "password", "credential")
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered != "credential_mode" and any(fragment in lowered for fragment in credential_fragments):
                return True
            if _contains_credential_shape(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_credential_shape(item) for item in value)
    return False


def _account_jobs_sql(*, workspace_id: str, limit: int) -> SqlStatement:
    return SqlStatement(
        sql="""
SELECT id, workspace_id, source_id, job_type, status, priority, created_at,
       started_at, finished_at, cancelled_at, error_code, error_message, metadata_json
FROM jobs
WHERE workspace_id = %(workspace_id)s
ORDER BY created_at DESC
LIMIT %(limit)s;
""".strip(),
        params={"workspace_id": workspace_id, "limit": limit},
    )


def _job_row_json(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "job_id": row.get("id"),
        "workspace_id": row.get("workspace_id"),
        "source_id": row.get("source_id"),
        "job_type": row.get("job_type"),
        "status": row.get("status"),
        "priority": row.get("priority"),
        "created_at": _datetime_json(row.get("created_at")),
        "started_at": _datetime_json(row.get("started_at")),
        "finished_at": _datetime_json(row.get("finished_at")),
        "cancelled_at": _datetime_json(row.get("cancelled_at")),
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
        "metadata": _json_object(row.get("metadata_json")),
    }


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
        return {
            str(key): _sanitize_readiness_payload(item, field_name=str(key).lower())
            for key, item in value.items()
        }
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
