from __future__ import annotations

import os
import re
import secrets
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from yutome import contract
from yutome.hosted.mcp_query import HostedMcpAuthContext, HostedMcpError, HostedMcpQueryAdapter


WORKSPACE_HEADER = "X-Yutome-Workspace-Id"
SCOPES_HEADER = "X-Yutome-Scopes"
USER_HEADER = "X-Yutome-User-Id"
GRANT_HEADER = "X-Yutome-Grant-Id"
CLIENT_HEADER = "X-Yutome-Client-Id"
SESSION_HEADER = "X-Yutome-Session-Id"
TOKEN_ENV_VAR = "YUTOME_HOSTED_API_TOKEN"
_SAFE_READINESS_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_READINESS_ERROR_FIELDS = frozenset({"error", "message", "detail"})


class ToolCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ResourceReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str


AuthDependency = Callable[..., HostedMcpAuthContext]
ReadinessCheck = Callable[[], Any]


def build_postgres_app(
    *,
    connection: Any,
    readiness_check: ReadinessCheck | None = None,
    gate: Any | None = None,
    ledger: Any | None = None,
    index_profile_ref: str | None = None,
    expected_api_token: str | None = None,
) -> Any:
    from yutome.hosted.search_store import PostgresVectorChordSearchStore

    search_store = PostgresVectorChordSearchStore(connection, index_profile_ref=index_profile_ref)
    adapter = HostedMcpQueryAdapter(search_store=search_store, gate=gate, ledger=ledger)
    app = build_app(
        adapter=adapter,
        readiness_check=readiness_check,
        expected_api_token=_normalize_api_token(expected_api_token) or _api_token_from_env(),
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
) -> Any:
    from fastapi import Depends, FastAPI, Header
    from fastapi.responses import JSONResponse

    app = FastAPI(
        title="yutome-hosted-mcp",
        description="Hosted Yutome MCP query API for the Cloudflare MCP edge.",
        version="0.1.0",
    )
    app.state.hosted_adapter = adapter
    normalized_api_token = _normalize_api_token(expected_api_token)
    app.state.hosted_api_auth_required = True
    app.state.hosted_api_auth_configured = auth_dependency is not None or bool(normalized_api_token)

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

    return app


def _parse_scopes(scopes_header: str | None) -> set[str]:
    if scopes_header is None:
        return {contract.AUTH_SCOPE}
    return {scope for scope in scopes_header.replace(",", " ").split() if scope}


def _api_token_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    return _normalize_api_token(env.get(TOKEN_ENV_VAR))


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


def error_body(response_json: Mapping[str, Any]) -> Mapping[str, Any]:
    detail = response_json.get("detail")
    return detail if isinstance(detail, Mapping) else {}
