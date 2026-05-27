from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


EntityName = Literal["chunk", "video", "channel"]
SearchOver = Literal["chunk_text"]
SearchMode = Literal["lexical", "semantic", "hybrid", "none"]
GroupKey = Literal["video", "channel", "transcript_source"]
SortDirection = Literal["asc", "desc"]
ProjectionName = Literal[
    "thin",
    "chunk",
    "metadata",
    "video_card",
    "video_attention",
    "channel_card",
    "group_video",
    "status_breakdown",
]


class StringPredicate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    eq: str | None = None
    in_: list[str] | None = Field(default=None, alias="in")
    not_in: list[str] | None = None
    starts_with: str | None = None
    starts_with_any: list[str] | None = None


class IntPredicate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    eq: int | None = None
    gte: int | None = None
    lte: int | None = None
    in_: list[int] | None = Field(default=None, alias="in")


class DateRange(BaseModel):
    gte: str | None = None
    lte: str | None = None


class BoolPredicate(BaseModel):
    eq: bool | None = None


class Filter(BaseModel):
    video_id: StringPredicate | None = None
    channel_id: StringPredicate | None = None
    channel_handle: StringPredicate | None = None
    published_at: DateRange | None = None
    duration_seconds: IntPredicate | None = None
    ingest_status: StringPredicate | None = None
    live_status: StringPredicate | None = None
    transcript_source: StringPredicate | None = None
    language: StringPredicate | None = None
    is_generated: BoolPredicate | None = None
    transcript_active: BoolPredicate | None = None
    chunk_id: StringPredicate | None = None
    sequence: IntPredicate | None = None
    start_ms: IntPredicate | None = None
    token_count: IntPredicate | None = None
    last_attempt_status: StringPredicate | None = None
    last_attempt_tool: StringPredicate | None = None
    last_attempt_error_class: StringPredicate | None = None
    last_attempt_retryable: BoolPredicate | None = None
    last_attempt_created_at: DateRange | None = None
    channel_selected: BoolPredicate | None = None
    channel_last_synced_at: DateRange | None = None


class Search(BaseModel):
    over: SearchOver = "chunk_text"
    mode: SearchMode = "hybrid"
    text: str = ""
    raw: bool = False


class OrderBy(BaseModel):
    field: Literal[
        "score",
        "published_at",
        "duration_seconds",
        "title",
        "ingest_status",
        "sequence",
        "start_ms",
        "last_attempt_created_at",
    ]
    direction: SortDirection = "desc"


class QueryRequest(BaseModel):
    entity: EntityName = "chunk"
    search: Search | None = None
    filter: Filter = Field(default_factory=Filter)
    group_by: GroupKey | None = None
    order_by: list[OrderBy] = Field(default_factory=list)
    project: ProjectionName = "thin"
    limit: int = Field(default=10, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
    per_group_limit: int = Field(default=3, ge=1, le=20)


class QueryResult(BaseModel):
    rows: list[dict[str, Any]]
    notes: list[str] = Field(default_factory=list)
    total: int | None = None


@dataclass(frozen=True)
class CompiledQuery:
    request: QueryRequest
    kind: str
    notes: list[str] = field(default_factory=list)
