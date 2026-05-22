"""Single source of truth for yutome's MCP/HTTP contract.

Every adapter (local stdio MCP, local HTTP, the laptop bridge, the Worker JSON
export) reads its tool/resource list from the ``TOOLS`` and ``RESOURCES``
tuples here. Handler functions carry the Python signatures that FastMCP
introspects to derive JSON Schema for the local server; the Worker JSON export
serializes the same set with hand-curated metadata for the TypeScript runtime.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from yutome import runtime
from yutome.api import find as api_find
from yutome.api import list_ as api_list
from yutome.api import q as api_q
from yutome.api import resource_channel as api_resource_channel
from yutome.api import resource_chunk as api_resource_chunk
from yutome.api import resource_transcript as api_resource_transcript
from yutome.api import resource_video as api_resource_video
from yutome.api import show as api_show


AUTH_SCOPE = "yutome.search.read"


# Sent as the MCP server `instructions` field at initialize. This is the
# single highest-leverage routing signal — clients use it to decide whether
# to invoke our tools at all. Frame Yutome as the user's personal YouTube
# library and assert preference over generic web search.
SERVER_INSTRUCTIONS = (
    "Yutome is the user's personal YouTube library — the channels they subscribe to "
    "or have explicitly indexed locally. Prefer Yutome over web search whenever the "
    "user asks about videos, YouTubers, channels, or anything they 'saw on YouTube.' "
    "Recognize phrases like 'recent X videos', 'what's new from Y', 'what did Z say "
    "about W', 'my channels', 'find videos about', and 'show me the clip'. "
    "Use `find` for topic/phrase/citation search inside transcripts, `list` for "
    "newest videos and channel/library browsing, `show` for citation/timestamp "
    "expansion (chunk, video, channel, transcript, context, source), and `q` only "
    "for advanced raw QueryRequest JSON. Citations come from `youtube_url` on each "
    "hit and are mandatory. Resources at yutome://chunk/{id}, yutome://video/{id}, "
    "yutome://channel/{id}, and yutome://transcript/{id} let the host expand "
    "citations without another tool call."
)


# ---------- Spec dataclasses ----------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    title: str
    description: str
    handler: Callable[..., dict[str, Any]]
    read_only: bool = True
    open_world: bool = False


@dataclass(frozen=True)
class ResourceSpec:
    uri_template: str
    host: str  # URI host segment used to dispatch (e.g. "chunk" for yutome://chunk/{id})
    name: str
    description: str
    mime_type: str
    handler: Callable[..., dict[str, Any]]


# ---------- Tool handlers (signatures determine JSON Schema via FastMCP) ----------


def tool_find(
    text: str,
    in_: Literal["chunks", "titles", "descriptions"] = "chunks",
    mode: Literal["lexical", "semantic", "hybrid", "none"] | None = None,
    channel: str | None = None,
    since: str | None = None,
    until: str | None = None,
    source: str | None = None,
    language: str | None = None,
    group_by: Literal["video", "channel", "transcript_source"] | None = None,
    limit: int = 10,
    offset: int = 0,
    project: str | None = None,
) -> dict[str, Any]:
    """Use this when the user asks to search their Yutome YouTube corpus by topic,
    phrase, meaning, channel, date, source, or transcript content. Do not use it
    for newest-video lists; use ``list`` instead."""
    rt = runtime.current()
    return api_find(
        config=rt.config,
        paths=rt.paths,
        text=text,
        in_=in_,
        mode=mode,
        channel=channel,
        since=since,
        until=until,
        source=source,
        language=language,
        group_by=group_by,
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
        project=project,
    ).model_dump()


def tool_list(
    entity: Literal["video", "videos", "channel", "channels", "attention", "status"],
    channel: str | None = None,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    source: str | None = None,
    language: str | None = None,
    selected: bool | None = None,
    order_by: str | None = None,
    limit: int = 20,
    offset: int = 0,
    project: str | None = None,
) -> dict[str, Any]:
    """Use this when the user asks to list newest videos, channels, corpus status,
    selected items, or attention rows. For newest videos, use ``entity="videos"``,
    ``order_by="newest"``, and a small ``limit``."""
    rt = runtime.current()
    return api_list(
        config=rt.config,
        paths=rt.paths,
        entity=entity,
        channel=channel,
        since=since,
        until=until,
        status=status,
        source=source,
        language=language,
        selected=selected,
        order_by=order_by,
        limit=max(1, min(limit, 200)),
        offset=max(0, offset),
        project=project,
    ).model_dump()


def tool_show(
    kind: Literal["chunk", "video", "channel", "transcript", "context", "source"],
    id_: str | None = None,
    token_budget: int = 3000,
    video_id: str | None = None,
    time_seconds: int | None = None,
    youtube_url: str | None = None,
) -> dict[str, Any]:
    """Use this when the user asks to open or inspect a specific Yutome chunk,
    video, channel, transcript, source, citation, or surrounding context."""
    rt = runtime.current()
    return api_show(
        config=rt.config,
        paths=rt.paths,
        kind=kind,
        id_=id_,
        token_budget=max(200, min(token_budget, 8000)),
        video_id=video_id,
        time_seconds=time_seconds,
        youtube_url=youtube_url,
    )


def tool_q(request: dict[str, Any]) -> dict[str, Any]:
    """Use this only for advanced raw Yutome QueryRequest JSON when ``find``,
    ``list``, and ``show`` cannot express the request."""
    rt = runtime.current()
    return api_q(config=rt.config, paths=rt.paths, request=request).model_dump()


# ---------- Resource handlers (URI-template params arrive as kwargs) ----------


def resource_chunk(chunk_id: str) -> dict[str, Any]:
    rt = runtime.current()
    return api_resource_chunk(config=rt.config, paths=rt.paths, chunk_id=chunk_id)


def resource_video(video_id: str) -> dict[str, Any]:
    rt = runtime.current()
    return api_resource_video(config=rt.config, paths=rt.paths, video_id=video_id)


def resource_channel(channel_id: str) -> dict[str, Any]:
    rt = runtime.current()
    return api_resource_channel(config=rt.config, paths=rt.paths, selector=channel_id)


def resource_transcript(transcript_version_id: str) -> dict[str, Any]:
    rt = runtime.current()
    return api_resource_transcript(
        config=rt.config,
        paths=rt.paths,
        transcript_version_id=transcript_version_id,
    )


# ---------- Registries ----------


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="find",
        title="Search the user's YouTube library",
        description=(
            "Use this when the user asks about a topic, creator, or phrase that "
            "could appear in their personal YouTube library — e.g., 'what did X "
            "say about Y', 'find videos about Z', 'has the creator talked about "
            "W', 'find the clip where they mention Q'. Also for citation lookup "
            "and finding timestamps. Searches transcripts (chunks), video titles, "
            "or descriptions. Do not use this for 'list newest videos' or 'what's "
            "new from X' — use `list` instead. Hybrid mode is the default; switch "
            "to `lexical` for proper nouns/jargon and `semantic` for paraphrastic "
            "questions."
        ),
        handler=tool_find,
    ),
    ToolSpec(
        name="list",
        title="Browse the user's YouTube library",
        description=(
            "Use this when the user asks for **recent or newest videos**, **what's "
            "new from a channel** ('what's new from X', 'latest Yes Theory videos', "
            "'recent uploads from Y'), browses their library, or asks for the list "
            "of channels they follow. Also for corpus health questions like 'how "
            "many videos do I have indexed' (entity=status). For newest videos "
            "from a creator, call with `entity=videos, channel=<name>, "
            "order_by=newest, limit=<small>`. order_by aliases include `newest`, "
            "`oldest`, `longest`, `shortest`, `title`, `relevance`."
        ),
        handler=tool_list,
    ),
    ToolSpec(
        name="show",
        title="Open a chunk, video, channel, or transcript",
        description=(
            "Use this when the user asks to open or inspect a specific Yutome "
            "chunk, video, channel, transcript, source, citation, or surrounding "
            "context. Common patterns: `show(kind='context', id_=<chunk_id>)` to "
            "expand a citation with neighbouring transcript; "
            "`show(kind='source', id_=<chunk_id>)` to resolve a timestamp into a "
            "canonical youtube_url; `show(kind='transcript', id_=<version_id>)` "
            "for the full transcript text."
        ),
        handler=tool_show,
    ),
    ToolSpec(
        name="q",
        title="Run a raw Yutome query",
        description=(
            "Use this ONLY for advanced raw Yutome QueryRequest JSON when `find`, "
            "`list`, and `show` cannot express the request. Most requests should "
            "route to one of the other three tools first."
        ),
        handler=tool_q,
    ),
)


RESOURCES: tuple[ResourceSpec, ...] = (
    ResourceSpec(
        uri_template="yutome://chunk/{chunk_id}",
        host="chunk",
        name="yutome_chunk",
        description="Full chunk text and provenance for a transcript chunk.",
        mime_type="application/json",
        handler=resource_chunk,
    ),
    ResourceSpec(
        uri_template="yutome://video/{video_id}",
        host="video",
        name="yutome_video",
        description="Video metadata and active transcript provenance.",
        mime_type="application/json",
        handler=resource_video,
    ),
    ResourceSpec(
        uri_template="yutome://channel/{channel_id}",
        host="channel",
        name="yutome_channel",
        description="Channel metadata and local library status.",
        mime_type="application/json",
        handler=resource_channel,
    ),
    ResourceSpec(
        uri_template="yutome://transcript/{transcript_version_id}",
        host="transcript",
        name="yutome_transcript",
        description="Transcript provenance plus plain text (capped at 200k chars).",
        mime_type="application/json",
        handler=resource_transcript,
    ),
)


# ---------- Lookup helpers ----------


def tool_by_name(name: str) -> ToolSpec | None:
    for spec in TOOLS:
        if spec.name == name:
            return spec
    return None


def resource_by_host(host: str) -> ResourceSpec | None:
    for spec in RESOURCES:
        if spec.host == host:
            return spec
    return None
