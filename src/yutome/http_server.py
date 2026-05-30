"""HTTP API for yutome query verbs."""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from yutome import contract
from yutome.mcp_server import configure


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
TOKEN_ENV_VAR = "YUTOME_HTTP_TOKEN"
CORS_ENV_VAR = "YUTOME_HTTP_CORS_ORIGINS"
LOGGER = logging.getLogger(__name__)

NO_TOKEN_AUTH_DETAIL = (
    f"{TOKEN_ENV_VAR} is not configured; protected HTTP endpoints deny unauthenticated access by default. "
    f"Set {TOKEN_ENV_VAR} or restart with --insecure/--allow-no-auth to opt into unauthenticated access."
)
INSECURE_NO_AUTH_WARNING = (
    f"INSECURE: {TOKEN_ENV_VAR} is not configured and no-auth HTTP mode is enabled. "
    "Protected endpoints will accept unauthenticated requests."
)


class FindRequest(BaseModel):
    text: str
    mode: Literal["lexical", "semantic", "hybrid", "none"] | None = None
    channel: str | None = None
    since: str | None = None
    until: str | None = None
    source: str | None = None
    language: str | None = None
    group_by: Literal["video", "channel", "transcript_source"] | None = None
    limit: int = Field(10, ge=1, le=200)
    offset: int = Field(0, ge=0)
    project: str | None = None


class ListRequest(BaseModel):
    entity: Literal["video", "videos", "channel", "channels", "status"]
    channel: str | None = None
    since: str | None = None
    until: str | None = None
    status: str | None = None
    source: str | None = None
    language: str | None = None
    selected: bool | None = None
    order_by: str | None = None
    limit: int = Field(20, ge=1, le=200)
    offset: int = Field(0, ge=0)
    project: str | None = None


class ShowRequest(BaseModel):
    kind: Literal["chunk", "video", "channel", "transcript", "context", "source"]
    id: str | None = None
    token_budget: int = Field(3000, ge=200, le=8000)
    video_id: str | None = None
    time_seconds: int | None = None
    youtube_url: str | None = None
    transcript_offset: int = Field(0, ge=0)
    transcript_limit: int | None = Field(None, ge=1, le=5000)


def _configured_token() -> str:
    return os.environ.get(TOKEN_ENV_VAR, "").strip()


def _verify_token_dependency(*, allow_no_auth: bool = False):  # noqa: ANN202 - factory returns a FastAPI Depends value.
    from fastapi import Header, HTTPException

    def _verify(authorization: str | None = Header(default=None)) -> None:
        expected = _configured_token()
        if not expected:
            if allow_no_auth:
                return
            raise HTTPException(status_code=401, detail=NO_TOKEN_AUTH_DETAIL)
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        if not secrets.compare_digest(authorization.removeprefix("Bearer ").strip(), expected):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    return _verify


def _auth_required(*, allow_no_auth: bool) -> bool:
    return bool(_configured_token()) or not allow_no_auth


def _log_auth_mode(*, allow_no_auth: bool) -> None:
    if _configured_token():
        return
    if allow_no_auth:
        LOGGER.warning(INSECURE_NO_AUTH_WARNING)
        return
    LOGGER.warning(NO_TOKEN_AUTH_DETAIL)


def _cors_origins_from_env() -> list[str]:
    raw = os.environ.get(CORS_ENV_VAR, "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def build_app(*, allow_no_auth: bool = False) -> Any:
    """Build and return the FastAPI app. Runtime config must already be loaded."""
    from fastapi import Depends, FastAPI, HTTPException

    _log_auth_mode(allow_no_auth=allow_no_auth)
    app = FastAPI(
        title="yutome",
        description="Postgres+VectorChord YouTube corpus HTTP API.",
        version="0.1.0",
    )
    cors_origins = _cors_origins_from_env()
    if cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
        )

    verify_token = _verify_token_dependency(allow_no_auth=allow_no_auth)
    auth = [Depends(verify_token)]

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
            "auth_required": _auth_required(allow_no_auth=allow_no_auth),
            "cors_enabled": bool(cors_origins),
        }

    @app.get("/readyz", dependencies=auth)
    def readyz() -> dict[str, Any]:
        status = contract.tool_list(entity="status")
        row = status["rows"][0] if status.get("rows") else {}
        return {
            "ok": True,
            "auth_required": _auth_required(allow_no_auth=allow_no_auth),
            "searchable_now": row.get("searchable_now", 0),
            "needs_attention": row.get("needs_attention", 0),
            "videos": row.get("videos", 0),
            "chunks": row.get("chunks", 0),
        }

    @app.post("/find", dependencies=auth)
    def find(req: FindRequest) -> dict[str, Any]:
        try:
            return contract.tool_find(
                text=req.text,
                mode=req.mode,
                channel=req.channel,
                since=req.since,
                until=req.until,
                source=req.source,
                language=req.language,
                group_by=req.group_by,
                limit=req.limit,
                offset=req.offset,
                project=req.project,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/list", dependencies=auth)
    def list_endpoint(req: ListRequest) -> dict[str, Any]:
        try:
            return contract.tool_list(
                entity=req.entity,
                channel=req.channel,
                since=req.since,
                until=req.until,
                status=req.status,
                source=req.source,
                language=req.language,
                selected=req.selected,
                order_by=req.order_by,
                limit=req.limit,
                offset=req.offset,
                project=req.project,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/show", dependencies=auth)
    def show(req: ShowRequest) -> dict[str, Any]:
        try:
            return contract.tool_show(
                kind=req.kind,
                id_=req.id,
                token_budget=req.token_budget,
                video_id=req.video_id,
                time_seconds=req.time_seconds,
                youtube_url=req.youtube_url,
                transcript_offset=req.transcript_offset,
                transcript_limit=req.transcript_limit,
            )
        except ValueError as exc:
            status_code = 400 if str(exc).startswith("Provide ") else 404
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @app.post("/q", dependencies=auth)
    def raw_query(req: dict[str, Any]) -> dict[str, Any]:
        try:
            return contract.tool_q(request=req)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/chunks/{chunk_id}", dependencies=auth)
    def chunk(chunk_id: str) -> dict[str, Any]:
        try:
            return contract.resource_chunk(chunk_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/videos/{video_id}", dependencies=auth)
    def video(video_id: str) -> dict[str, Any]:
        try:
            return contract.resource_video(video_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/channels/{channel_id}", dependencies=auth)
    def channel(channel_id: str) -> dict[str, Any]:
        try:
            return contract.resource_channel(channel_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/transcripts/{transcript_version_id}", dependencies=auth)
    def transcript(transcript_version_id: str) -> dict[str, Any]:
        try:
            return contract.resource_transcript(transcript_version_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


def run_http_server(
    config_path: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    require_token_for_non_loopback: bool = True,
    allow_no_auth: bool = False,
    cors_origins: list[str] | None = None,
) -> None:
    """Configure runtime, build the FastAPI app, and run uvicorn."""
    import uvicorn

    configure(config_path)
    if cors_origins:
        os.environ[CORS_ENV_VAR] = ",".join(cors_origins)
    if require_token_for_non_loopback and not allow_no_auth and not _is_loopback_host(host) and not _configured_token():
        raise RuntimeError(
            f"{TOKEN_ENV_VAR} is required when binding the HTTP API to non-loopback host {host!r}. "
            "Set the token or pass --insecure/--allow-no-auth to opt into unauthenticated access."
        )
    app = build_app(allow_no_auth=allow_no_auth)
    uvicorn.run(app, host=host, port=port, log_level="info")


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    return normalized in {"127.0.0.1", "localhost", "::1"} or normalized.startswith("127.")
