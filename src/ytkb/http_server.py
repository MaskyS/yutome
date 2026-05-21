"""Local HTTP API for ytkb query verbs."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ytkb.api import find as api_find
from ytkb.api import list_ as api_list
from ytkb.api import q as api_q
from ytkb.api import show as api_show
from ytkb.mcp_server import configure, resource_channel, resource_chunk, resource_transcript, resource_video
from ytkb.query import QueryRequest


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
TOKEN_ENV_VAR = "YTKB_HTTP_TOKEN"


class FindRequest(BaseModel):
    text: str
    in_: Literal["chunks", "titles", "descriptions"] = Field("chunks", alias="in")
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
    entity: Literal["video", "videos", "channel", "channels", "attention", "status"]
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


def _verify_token_dependency():  # noqa: ANN202 - factory returns a FastAPI Depends value.
    from fastapi import Header, HTTPException

    def _verify(authorization: str | None = Header(default=None)) -> None:
        expected = os.environ.get(TOKEN_ENV_VAR)
        if not expected:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        if authorization.removeprefix("Bearer ").strip() != expected:
            raise HTTPException(status_code=401, detail="invalid bearer token")

    return _verify


def build_app() -> Any:
    """Build and return the FastAPI app. Runtime config must already be loaded."""
    from fastapi import Depends, FastAPI, HTTPException
    from ytkb.mcp_server import _runtime

    app = FastAPI(
        title="ytkb",
        description="Local-first YouTube channel knowledge base HTTP API.",
        version="0.1.0",
    )

    verify_token = _verify_token_dependency()
    auth = [Depends(verify_token)]

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "auth_required": bool(os.environ.get(TOKEN_ENV_VAR))}

    @app.post("/find", dependencies=auth)
    def find(req: FindRequest) -> dict[str, Any]:
        runtime = _runtime()
        try:
            return api_find(
                config=runtime.config,
                paths=runtime.paths,
                text=req.text,
                in_=req.in_,
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
            ).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/list", dependencies=auth)
    def list_endpoint(req: ListRequest) -> dict[str, Any]:
        runtime = _runtime()
        try:
            return api_list(
                config=runtime.config,
                paths=runtime.paths,
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
            ).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/show", dependencies=auth)
    def show(req: ShowRequest) -> dict[str, Any]:
        runtime = _runtime()
        try:
            return api_show(
                config=runtime.config,
                paths=runtime.paths,
                kind=req.kind,
                id_=req.id,
                token_budget=req.token_budget,
                video_id=req.video_id,
                time_seconds=req.time_seconds,
                youtube_url=req.youtube_url,
            )
        except ValueError as exc:
            status_code = 400 if str(exc).startswith("Provide ") else 404
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc

    @app.post("/q", dependencies=auth)
    def raw_query(req: QueryRequest) -> dict[str, Any]:
        runtime = _runtime()
        try:
            return api_q(config=runtime.config, paths=runtime.paths, request=req).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/chunks/{chunk_id}", dependencies=auth)
    def chunk(chunk_id: str) -> dict[str, Any]:
        try:
            return resource_chunk(chunk_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/videos/{video_id}", dependencies=auth)
    def video(video_id: str) -> dict[str, Any]:
        try:
            return resource_video(video_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/channels/{channel_id}", dependencies=auth)
    def channel(channel_id: str) -> dict[str, Any]:
        try:
            return resource_channel(channel_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/transcripts/{transcript_version_id}", dependencies=auth)
    def transcript(transcript_version_id: str) -> dict[str, Any]:
        try:
            return resource_transcript(transcript_version_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return app


def run_http_server(
    config_path: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> None:
    """Configure runtime, build the FastAPI app, and run uvicorn."""
    import uvicorn

    configure(config_path)
    app = build_app()
    uvicorn.run(app, host=host, port=port, log_level="info")
