from __future__ import annotations

import os
import re
import secrets
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

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
from yutome.hosted.account_read import (
    load_active_workspace,
    read_active_account_grants,
    read_library_overview,
    read_workspace_summary,
)
from yutome.hosted.mcp_query import HostedMcpAuthContext, HostedMcpError, HostedMcpQueryAdapter


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


class AccountApiContext(BaseModel):
    """Authenticated dashboard caller. workspace_id is derived from the verified
    session token, never from a client-supplied header."""

    workspace_id: str
    user_id: str
    workspace_name: str | None = None


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

    return app


def _parse_scopes(scopes_header: str | None) -> set[str]:
    if scopes_header is None:
        return {contract.AUTH_SCOPE}
    return {scope for scope in scopes_header.replace(",", " ").split() if scope}


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
