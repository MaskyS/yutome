from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import TIMESTAMP, Integer, and_, bindparam, case, cast, distinct, func, literal, literal_column, or_, select, text, true
from sqlalchemy.dialects.postgresql import JSONB

from yutome.hosted.repositories import SqlStatement
from yutome.hosted.schema import chunks, sources, transcript_versions, videos
from yutome.hosted.sqlalchemy_core import compile_postgres_statement


TRANSCRIPT_TEXT_CAP = 200_000


class HostedResourceNotFound(LookupError):
    def __init__(self, *, kind: str, id_: str) -> None:
        self.kind = kind
        self.id = id_
        super().__init__(f"{kind} not found: {id_}")


@dataclass(frozen=True)
class HostedResourceQueries:
    connection: Any

    def chunk(self, *, workspace_id: str, chunk_id: str) -> dict[str, Any]:
        statement = chunk_resource_sql(workspace_id=workspace_id, chunk_id=chunk_id)
        row = _one(self.connection.execute(statement.sql, statement.params))
        if row is None:
            raise HostedResourceNotFound(kind="chunk", id_=chunk_id)
        return format_chunk_resource(row)

    def context(
        self,
        *,
        workspace_id: str,
        chunk_id: str | None = None,
        video_id: str | None = None,
        time_seconds: int | None = None,
        youtube_url: str | None = None,
        token_budget: int = 3000,
    ) -> dict[str, Any]:
        if youtube_url:
            parsed_video_id, parsed_time_seconds = parse_youtube_location(youtube_url)
            video_id = video_id or parsed_video_id
            time_seconds = time_seconds if time_seconds is not None else parsed_time_seconds
        statement = context_anchor_sql(
            workspace_id=workspace_id,
            chunk_id=chunk_id,
            video_id=video_id,
            time_seconds=time_seconds,
        )
        anchor = _one(self.connection.execute(statement.sql, statement.params))
        if anchor is None:
            raise HostedResourceNotFound(kind="context", id_=chunk_id or video_id or youtube_url or "")
        chunks_statement = context_chunks_sql(
            workspace_id=workspace_id,
            transcript_version_id=str(anchor["transcript_version_id"]),
        )
        transcript_chunks = _rows(self.connection.execute(chunks_statement.sql, chunks_statement.params))
        return format_context_resource(anchor, transcript_chunks, token_budget=token_budget)

    def video(self, *, workspace_id: str, video_id: str) -> dict[str, Any]:
        statement = video_resource_sql(workspace_id=workspace_id, video_id=video_id)
        row = _one(self.connection.execute(statement.sql, statement.params))
        if row is None:
            raise HostedResourceNotFound(kind="video", id_=video_id)
        return format_video_resource(row)

    def channel(self, *, workspace_id: str, channel_id: str) -> dict[str, Any]:
        statement = channel_resource_sql(workspace_id=workspace_id, channel_id=channel_id)
        row = _one(self.connection.execute(statement.sql, statement.params))
        if row is None:
            raise HostedResourceNotFound(kind="channel", id_=channel_id)
        return format_channel_resource(row)

    def transcript(self, *, workspace_id: str, transcript_version_id: str, offset: int = 0, limit: int | None = None) -> dict[str, Any]:
        statement = transcript_resource_sql(
            workspace_id=workspace_id,
            transcript_version_id=transcript_version_id,
            offset=offset,
            limit=limit,
        )
        rows = _rows(self.connection.execute(statement.sql, statement.params))
        if not rows:
            raise HostedResourceNotFound(kind="transcript", id_=transcript_version_id)
        return format_transcript_resource(rows, offset=offset, limit=limit)

    def source(self, *, workspace_id: str, source_id: str) -> dict[str, Any]:
        statement = source_resource_sql(workspace_id=workspace_id, source_id=source_id)
        row = _one(self.connection.execute(statement.sql, statement.params))
        if row is None:
            raise HostedResourceNotFound(kind="source", id_=source_id)
        return format_source_resource(row)

    def list_status(self, *, workspace_id: str) -> dict[str, Any]:
        statement = list_status_sql(workspace_id=workspace_id)
        row = _one(self.connection.execute(statement.sql, statement.params))
        return format_status_row(row or {})

    def list_videos(
        self,
        *,
        workspace_id: str,
        limit: int,
        offset: int = 0,
        channel: str | None = None,
        video_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        status: str | None = None,
        source: str | None = None,
        language: str | None = None,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        statement = list_videos_sql(
            workspace_id=workspace_id,
            limit=limit,
            offset=offset,
            channel=channel,
            video_id=video_id,
            since=since,
            until=until,
            status=status,
            source=source,
            language=language,
            order_by=order_by,
        )
        return [format_video_list_row(row) for row in _rows(self.connection.execute(statement.sql, statement.params))]

    def list_channels(
        self,
        *,
        workspace_id: str,
        limit: int,
        offset: int = 0,
        channel: str | None = None,
        since: str | None = None,
        until: str | None = None,
        status: str | None = None,
        source: str | None = None,
        language: str | None = None,
        selected: bool | None = None,
    ) -> list[dict[str, Any]]:
        statement = list_channels_sql(
            workspace_id=workspace_id,
            limit=limit,
            offset=offset,
            channel=channel,
            since=since,
            until=until,
            status=status,
            source=source,
            language=language,
            selected=selected,
        )
        return [format_channel_list_row(row) for row in _rows(self.connection.execute(statement.sql, statement.params))]


def _chunk_resource_columns() -> list[Any]:
    return [
        chunks.c.id.label("chunk_id"),
        chunks.c.video_id,
        videos.c.youtube_video_id,
        chunks.c.transcript_version_id,
        chunks.c.chunk_index,
        chunks.c.start_seconds,
        chunks.c.end_seconds,
        chunks.c.text,
        chunks.c.metadata_json.label("chunk_metadata"),
        videos.c.title,
        videos.c.channel_id,
        videos.c.source_id,
        transcript_versions.c.source.label("transcript_source"),
        transcript_versions.c.language_code.label("language"),
        transcript_versions.c.metadata_json.label("transcript_metadata"),
    ]


def _chunk_resource_join() -> Any:
    return chunks.join(
        videos,
        and_(videos.c.id == chunks.c.video_id, videos.c.workspace_id == chunks.c.workspace_id),
    ).join(
        transcript_versions,
        and_(
            transcript_versions.c.id == chunks.c.transcript_version_id,
            transcript_versions.c.workspace_id == chunks.c.workspace_id,
        ),
    )


def chunk_resource_sql(*, workspace_id: str, chunk_id: str) -> SqlStatement:
    workspace_param = bindparam("workspace_id", value=workspace_id)
    chunk_param = bindparam("chunk_id", value=chunk_id)
    statement = (
        select(*_chunk_resource_columns())
        .select_from(_chunk_resource_join())
        .where(
            chunks.c.workspace_id == workspace_param,
            chunks.c.id == chunk_param,
        )
        .limit(literal_column("1"))
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def context_anchor_sql(
    *,
    workspace_id: str,
    chunk_id: str | None = None,
    video_id: str | None = None,
    time_seconds: int | None = None,
) -> SqlStatement:
    if not chunk_id and not video_id:
        raise ValueError("context requires a chunk id, video id, or timestamped YouTube URL")
    anchor_chunk_id = _blank_to_none(chunk_id)
    anchor_video_id = _blank_to_none(video_id)
    workspace_param = bindparam("workspace_id", value=workspace_id)

    # The raw query branched on whether each id param was NULL; building the
    # match conditions in Python reproduces that without per-row SQL NULL checks.
    if anchor_chunk_id is not None:
        match_condition: Any = chunks.c.id == bindparam("chunk_id", value=anchor_chunk_id)
    else:
        video_param = bindparam("video_id", value=anchor_video_id)
        match_condition = and_(
            anchor_video_id is not None,
            or_(videos.c.id == video_param, videos.c.youtube_video_id == video_param),
            videos.c.active_transcript_version_id == chunks.c.transcript_version_id,
        )

    time_bound = None if time_seconds is None else cast(literal(time_seconds), Integer)
    rank_cases: list[Any] = []
    if time_bound is not None:
        rank_cases.append(
            (
                and_(chunks.c.start_seconds <= time_bound, chunks.c.end_seconds >= time_bound),
                0,
            )
        )
    if anchor_chunk_id is not None:
        rank_cases.append((chunks.c.id == anchor_chunk_id, 0))
    rank = case(*rank_cases, else_=1) if rank_cases else literal(1)
    distance = func.abs(
        func.coalesce(chunks.c.start_seconds, 0) - func.coalesce(time_bound, 0)
    )

    statement = (
        select(*_chunk_resource_columns())
        .select_from(_chunk_resource_join())
        .where(chunks.c.workspace_id == workspace_param, match_condition)
        .order_by(rank, distance, chunks.c.chunk_index)
        .limit(literal_column("1"))
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def context_chunks_sql(*, workspace_id: str, transcript_version_id: str) -> SqlStatement:
    workspace_param = bindparam("workspace_id", value=workspace_id)
    statement = (
        select(*_chunk_resource_columns())
        .select_from(_chunk_resource_join())
        .where(
            chunks.c.workspace_id == workspace_param,
            chunks.c.transcript_version_id == bindparam("transcript_version_id", value=transcript_version_id),
        )
        .order_by(chunks.c.chunk_index)
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def video_resource_sql(*, workspace_id: str, video_id: str) -> SqlStatement:
    workspace_param = bindparam("workspace_id", value=workspace_id)
    video_param = bindparam("video_id", value=video_id)
    join = videos.outerjoin(
        sources,
        and_(sources.c.id == videos.c.source_id, sources.c.workspace_id == videos.c.workspace_id),
    ).outerjoin(
        chunks,
        and_(chunks.c.video_id == videos.c.id, chunks.c.workspace_id == videos.c.workspace_id),
    )
    statement = (
        select(
            videos.c.id.label("video_id"),
            videos.c.youtube_video_id,
            videos.c.source_id,
            videos.c.active_transcript_version_id,
            videos.c.channel_id,
            videos.c.title,
            videos.c.description,
            videos.c.published_at,
            videos.c.duration_seconds,
            videos.c.metadata_json,
            sources.c.display_name.label("source_display_name"),
            sources.c.source_url,
            sources.c.source_type,
            func.count(chunks.c.id)
            .filter(chunks.c.transcript_version_id == videos.c.active_transcript_version_id)
            .label("active_chunk_count"),
        )
        .select_from(join)
        .where(
            videos.c.workspace_id == workspace_param,
            or_(videos.c.id == video_param, videos.c.youtube_video_id == video_param),
        )
        .group_by(videos.c.id, sources.c.id)
        .limit(literal_column("1"))
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def channel_resource_sql(*, workspace_id: str, channel_id: str) -> SqlStatement:
    workspace_param = bindparam("workspace_id", value=workspace_id)
    channel_param = bindparam("channel_id", value=channel_id)
    join = videos.outerjoin(
        sources,
        and_(sources.c.id == videos.c.source_id, sources.c.workspace_id == videos.c.workspace_id),
    )
    channel_id_expr = func.coalesce(videos.c.channel_id, sources.c.canonical_channel_id, channel_param)
    statement = (
        select(
            channel_id_expr.label("channel_id"),
            func.max(
                func.coalesce(videos.c.metadata_json["channel_title"].astext, sources.c.display_name)
            ).label("title"),
            func.max(videos.c.metadata_json["channel_handle"].astext).label("channel_handle"),
            func.count(distinct(videos.c.id)).label("video_count"),
            func.max(videos.c.published_at).label("latest_published_at"),
            func.array_remove(func.array_agg(distinct(sources.c.id)), None).label("source_ids"),
        )
        .select_from(join)
        .where(
            videos.c.workspace_id == workspace_param,
            or_(
                videos.c.channel_id == channel_param,
                videos.c.metadata_json["channel_handle"].astext == channel_param,
                sources.c.canonical_channel_id == channel_param,
            ),
        )
        .group_by(channel_id_expr)
        .limit(literal_column("1"))
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def transcript_resource_sql(
    *,
    workspace_id: str,
    transcript_version_id: str,
    offset: int = 0,
    limit: int | None = None,
) -> SqlStatement:
    bounded_offset = max(0, offset)
    bounded_limit = None if limit is None else max(1, min(limit, 5000))
    workspace_param = bindparam("workspace_id", value=workspace_id)
    transcript_param = bindparam("transcript_version_id", value=transcript_version_id)

    selected_transcript = (
        select(
            transcript_versions.c.id.label("transcript_version_id"),
            transcript_versions.c.video_id,
            videos.c.youtube_video_id,
            transcript_versions.c.source,
            transcript_versions.c.language_code,
            transcript_versions.c.content_hash,
            transcript_versions.c.metadata_json,
            transcript_versions.c.created_at,
            (videos.c.active_transcript_version_id == transcript_versions.c.id).label("active"),
            func.count(chunks.c.id).label("segment_count"),
        )
        .select_from(
            transcript_versions.join(
                videos,
                and_(
                    videos.c.id == transcript_versions.c.video_id,
                    videos.c.workspace_id == transcript_versions.c.workspace_id,
                ),
            ).outerjoin(
                chunks,
                and_(
                    chunks.c.transcript_version_id == transcript_versions.c.id,
                    chunks.c.workspace_id == transcript_versions.c.workspace_id,
                ),
            )
        )
        .where(
            transcript_versions.c.workspace_id == workspace_param,
            or_(
                transcript_versions.c.id == transcript_param,
                videos.c.id == transcript_param,
                videos.c.youtube_video_id == transcript_param,
            ),
        )
        .group_by(transcript_versions.c.id, videos.c.id)
        .order_by(
            (transcript_versions.c.id == transcript_param).desc(),
            literal_column("active").desc(),
            transcript_versions.c.created_at.desc(),
        )
        .limit(literal_column("1"))
        .cte("selected_transcript")
    )

    selected_chunks = (
        select(chunks)
        .select_from(
            chunks.join(
                selected_transcript,
                selected_transcript.c.transcript_version_id == chunks.c.transcript_version_id,
            )
        )
        .where(chunks.c.workspace_id == workspace_param)
        .order_by(chunks.c.chunk_index)
        .offset(bindparam("offset", value=bounded_offset))
        .limit(func.coalesce(cast(bindparam("limit", value=bounded_limit), Integer), 2147483647))
        .cte("selected_chunks")
    )

    final_join = selected_transcript.outerjoin(selected_chunks, true())
    statement = (
        select(
            selected_transcript.c.transcript_version_id,
            selected_transcript.c.video_id,
            selected_transcript.c.youtube_video_id,
            selected_transcript.c.source,
            selected_transcript.c.language_code,
            selected_transcript.c.content_hash,
            selected_transcript.c.metadata_json,
            selected_transcript.c.created_at,
            selected_transcript.c.active,
            selected_transcript.c.segment_count,
            selected_chunks.c.id.label("chunk_id"),
            selected_chunks.c.chunk_index,
            selected_chunks.c.start_seconds,
            selected_chunks.c.end_seconds,
            selected_chunks.c.text,
        )
        .select_from(final_join)
        .order_by(selected_chunks.c.chunk_index.nullslast())
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def source_resource_sql(*, workspace_id: str, source_id: str) -> SqlStatement:
    workspace_param = bindparam("workspace_id", value=workspace_id)
    source_param = bindparam("source_id", value=source_id)
    statement = (
        select(
            sources.c.id.label("source_id"),
            sources.c.source_type,
            sources.c.source_url,
            sources.c.canonical_channel_id,
            sources.c.canonical_playlist_id,
            sources.c.canonical_video_id,
            sources.c.display_name,
            sources.c.selected,
            sources.c.auto_index_allowed,
            sources.c.import_source,
            sources.c.auth_grant_id,
            sources.c.metadata_json,
            sources.c.status,
            sources.c.last_discovered_at,
            sources.c.last_indexed_at,
            sources.c.created_at,
            sources.c.updated_at,
        )
        .where(
            sources.c.workspace_id == workspace_param,
            sources.c.id == source_param,
        )
        .limit(literal_column("1"))
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def list_status_sql(*, workspace_id: str) -> SqlStatement:
    workspace_param = bindparam("workspace_id", value=workspace_id)
    videos_count = (
        select(func.count()).select_from(videos).where(videos.c.workspace_id == workspace_param).scalar_subquery().label("videos")
    )
    searchable_now = (
        select(func.count())
        .select_from(videos)
        .where(videos.c.workspace_id == workspace_param, videos.c.active_transcript_version_id.is_not(None))
        .scalar_subquery()
        .label("searchable_now")
    )
    still_indexing = (
        select(func.count())
        .select_from(videos)
        .where(videos.c.workspace_id == workspace_param, videos.c.active_transcript_version_id.is_(None))
        .scalar_subquery()
        .label("still_indexing")
    )
    chunks_count = (
        select(func.count()).select_from(chunks).where(chunks.c.workspace_id == workspace_param).scalar_subquery().label("chunks")
    )
    transcript_versions_count = (
        select(func.count())
        .select_from(transcript_versions)
        .where(transcript_versions.c.workspace_id == workspace_param)
        .scalar_subquery()
        .label("transcript_versions")
    )
    channels_count = (
        select(func.count())
        .select_from(sources)
        .where(sources.c.workspace_id == workspace_param, sources.c.source_type == bindparam("source_type", value="channel"))
        .scalar_subquery()
        .label("channels")
    )
    status_label = case(
        (videos.c.active_transcript_version_id.is_(None), bindparam("pending_status", value="pending")),
        else_=bindparam("indexed_status", value="indexed"),
    ).label("status")
    status_counts = (
        select(status_label, func.count().label("count"))
        .where(videos.c.workspace_id == workspace_param)
        .group_by(literal_column("1"))
        .subquery("status_counts")
    )
    statuses = (
        select(
            func.coalesce(
                func.jsonb_object_agg(status_counts.c.status, status_counts.c.count),
                cast(text("'{}'"), JSONB),
            )
        )
        .select_from(status_counts)
        .scalar_subquery()
        .label("statuses")
    )
    statement = select(
        videos_count,
        searchable_now,
        still_indexing,
        cast(literal(0), Integer).label("needs_attention"),
        chunks_count,
        transcript_versions_count,
        channels_count,
        statuses,
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def _video_ingest_status_expr() -> Any:
    return func.coalesce(
        videos.c.metadata_json["ingest_status"].astext,
        case((videos.c.active_transcript_version_id.is_(None), "pending"), else_="indexed"),
    )


def _list_videos_order_by(order_by: str | None) -> list[Any]:
    orderings: dict[str | None, list[Any]] = {
        None: [videos.c.published_at.desc().nullslast(), videos.c.created_at.desc(), videos.c.id],
        "newest": [videos.c.published_at.desc().nullslast(), videos.c.created_at.desc(), videos.c.id],
        "oldest": [videos.c.published_at.asc().nullslast(), videos.c.created_at.asc(), videos.c.id],
        "longest": [
            videos.c.duration_seconds.desc().nullslast(),
            videos.c.published_at.desc().nullslast(),
            videos.c.id,
        ],
        "shortest": [
            videos.c.duration_seconds.asc().nullslast(),
            videos.c.published_at.desc().nullslast(),
            videos.c.id,
        ],
        "title": [videos.c.title.asc(), videos.c.published_at.desc().nullslast(), videos.c.id],
    }
    return orderings.get(
        order_by,
        [videos.c.published_at.desc().nullslast(), videos.c.created_at.desc(), videos.c.id],
    )


def list_videos_sql(
    *,
    workspace_id: str,
    limit: int,
    offset: int = 0,
    channel: str | None = None,
    video_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    source: str | None = None,
    language: str | None = None,
    order_by: str | None = None,
) -> SqlStatement:
    since_value = _blank_to_none(since)
    until_value = _blank_to_none(until)
    status_exact = _status_exact(status)
    status_prefix = _status_prefix(status)
    source_value = _blank_to_none(source)
    source_prefix = _prefix(source)
    language_value = _blank_to_none(language)
    ingest_status = _video_ingest_status_expr()
    workspace_param = bindparam("workspace_id", value=workspace_id)

    join = (
        videos.outerjoin(
            sources,
            and_(sources.c.id == videos.c.source_id, sources.c.workspace_id == videos.c.workspace_id),
        )
        .outerjoin(
            transcript_versions,
            and_(
                transcript_versions.c.id == videos.c.active_transcript_version_id,
                transcript_versions.c.workspace_id == videos.c.workspace_id,
            ),
        )
        .outerjoin(
            chunks,
            and_(chunks.c.video_id == videos.c.id, chunks.c.workspace_id == videos.c.workspace_id),
        )
    )

    conditions: list[Any] = [videos.c.workspace_id == workspace_param]
    if video_id is not None:
        video_param = bindparam("video_id", value=video_id)
        conditions.append(or_(videos.c.id == video_param, videos.c.youtube_video_id == video_param))
    if channel is not None:
        channel_param = bindparam("channel", value=channel)
        conditions.append(
            or_(
                videos.c.channel_id == channel_param,
                videos.c.metadata_json["channel_handle"].astext == channel_param,
                videos.c.metadata_json["channel_title"].astext == channel_param,
                sources.c.canonical_channel_id == channel_param,
                sources.c.display_name == channel_param,
            )
        )
    if since_value is not None:
        conditions.append(videos.c.published_at >= cast(bindparam("since", value=since_value), TIMESTAMP(timezone=True)))
    if until_value is not None:
        conditions.append(videos.c.published_at <= cast(bindparam("until", value=until_value), TIMESTAMP(timezone=True)))
    if status_exact is not None:
        conditions.append(ingest_status == bindparam("status", value=status_exact))
    elif status_prefix is not None:
        conditions.append(ingest_status.like(bindparam("status_prefix", value=status_prefix)))
    if source_value is not None:
        source_param = bindparam("source", value=source_value)
        source_prefix_param = bindparam("source_prefix", value=source_prefix)
        conditions.append(
            or_(
                transcript_versions.c.source == source_param,
                transcript_versions.c.source.like(source_prefix_param),
                sources.c.id == source_param,
                sources.c.source_url == source_param,
                sources.c.import_source == source_param,
            )
        )
    if language_value is not None:
        conditions.append(transcript_versions.c.language_code == bindparam("language", value=language_value))

    statement = (
        select(
            videos.c.id.label("video_id"),
            videos.c.youtube_video_id,
            videos.c.source_id,
            videos.c.active_transcript_version_id,
            videos.c.channel_id,
            videos.c.title,
            videos.c.description,
            videos.c.published_at,
            videos.c.duration_seconds,
            videos.c.metadata_json,
            sources.c.display_name.label("source_display_name"),
            sources.c.source_url,
            sources.c.source_type,
            func.count(chunks.c.id)
            .filter(chunks.c.transcript_version_id == videos.c.active_transcript_version_id)
            .label("active_chunk_count"),
        )
        .select_from(join)
        .where(*conditions)
        .group_by(videos.c.id, sources.c.id)
        .order_by(*_list_videos_order_by(order_by))
        .limit(bindparam("limit", value=max(1, min(limit, 200))))
        .offset(bindparam("offset", value=max(0, offset)))
    )
    sql, params = compile_postgres_statement(statement)
    params.setdefault("video_id", video_id)
    params.setdefault("channel", channel)
    params.setdefault("source_prefix", source_prefix)
    params.setdefault("language", language_value)
    return SqlStatement(sql=sql + ";", params=params)


def list_channels_sql(
    *,
    workspace_id: str,
    limit: int,
    offset: int = 0,
    channel: str | None = None,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    source: str | None = None,
    language: str | None = None,
    selected: bool | None = None,
) -> SqlStatement:
    since_value = _blank_to_none(since)
    until_value = _blank_to_none(until)
    status_exact = _status_exact(status)
    status_prefix = _status_prefix(status)
    source_value = _blank_to_none(source)
    source_prefix = _prefix(source)
    language_value = _blank_to_none(language)
    workspace_param = bindparam("workspace_id", value=workspace_id)

    cs_channel_id = func.coalesce(sources.c.canonical_channel_id, sources.c.id)
    cs_conditions: list[Any] = [
        sources.c.workspace_id == workspace_param,
        sources.c.source_type == bindparam("source_type", value="channel"),
    ]
    if selected is not None:
        cs_conditions.append(sources.c.selected == bindparam("selected", value=selected))
    channel_sources = (
        select(
            cs_channel_id.label("channel_id"),
            func.max(sources.c.display_name).label("title"),
            func.bool_or(sources.c.selected).label("selected"),
            func.count(distinct(sources.c.id)).label("source_count"),
            func.array_remove(func.array_agg(distinct(sources.c.id)), None).label("source_ids"),
            func.max(sources.c.last_discovered_at).label("last_discovered_at"),
            func.max(sources.c.last_indexed_at).label("last_indexed_at"),
        )
        .where(*cs_conditions)
        .group_by(cs_channel_id)
        .cte("channel_sources")
    )

    cv_channel_id = func.coalesce(videos.c.channel_id, sources.c.canonical_channel_id)
    ingest_status = _video_ingest_status_expr()
    cv_conditions: list[Any] = [videos.c.workspace_id == workspace_param]
    if since_value is not None:
        cv_conditions.append(videos.c.published_at >= cast(bindparam("since", value=since_value), TIMESTAMP(timezone=True)))
    if until_value is not None:
        cv_conditions.append(videos.c.published_at <= cast(bindparam("until", value=until_value), TIMESTAMP(timezone=True)))
    if status_exact is not None:
        cv_conditions.append(ingest_status == bindparam("status", value=status_exact))
    elif status_prefix is not None:
        cv_conditions.append(ingest_status.like(bindparam("status_prefix", value=status_prefix)))
    if source_value is not None:
        source_param = bindparam("source", value=source_value)
        source_prefix_param = bindparam("source_prefix", value=source_prefix)
        cv_conditions.append(
            or_(
                transcript_versions.c.source == source_param,
                transcript_versions.c.source.like(source_prefix_param),
                sources.c.id == source_param,
                sources.c.source_url == source_param,
                sources.c.import_source == source_param,
            )
        )
    if language_value is not None:
        cv_conditions.append(transcript_versions.c.language_code == bindparam("language", value=language_value))
    channel_videos = (
        select(
            cv_channel_id.label("channel_id"),
            func.max(
                func.coalesce(videos.c.metadata_json["channel_title"].astext, sources.c.display_name)
            ).label("title"),
            func.max(videos.c.metadata_json["channel_handle"].astext).label("channel_handle"),
            func.count(distinct(videos.c.id)).label("video_count"),
            func.max(videos.c.published_at).label("latest_published_at"),
        )
        .select_from(
            videos.outerjoin(
                sources,
                and_(sources.c.id == videos.c.source_id, sources.c.workspace_id == videos.c.workspace_id),
            ).outerjoin(
                transcript_versions,
                and_(
                    transcript_versions.c.id == videos.c.active_transcript_version_id,
                    transcript_versions.c.workspace_id == videos.c.workspace_id,
                ),
            )
        )
        .where(*cv_conditions)
        .group_by(cv_channel_id)
        .cte("channel_videos")
    )

    coalesced_channel_id = func.coalesce(channel_sources.c.channel_id, channel_videos.c.channel_id)
    coalesced_title = func.coalesce(channel_videos.c.title, channel_sources.c.title)
    join = channel_sources.outerjoin(
        channel_videos,
        channel_sources.c.channel_id == channel_videos.c.channel_id,
        full=True,
    )
    final_conditions: list[Any] = [coalesced_channel_id.is_not(None)]
    if channel is not None:
        channel_param = bindparam("channel", value=channel)
        final_conditions.append(
            or_(
                coalesced_channel_id == channel_param,
                channel_videos.c.channel_handle == channel_param,
                coalesced_title == channel_param,
            )
        )
    statement = (
        select(
            coalesced_channel_id.label("channel_id"),
            coalesced_title.label("title"),
            channel_videos.c.channel_handle,
            func.coalesce(channel_videos.c.video_count, 0).label("video_count"),
            channel_videos.c.latest_published_at,
            func.coalesce(channel_sources.c.selected, true()).label("selected"),
            func.coalesce(channel_sources.c.source_count, 0).label("source_count"),
            func.coalesce(channel_sources.c.source_ids, literal_column("ARRAY[]::text[]")).label("source_ids"),
            channel_sources.c.last_discovered_at,
            channel_sources.c.last_indexed_at,
        )
        .select_from(join)
        .where(*final_conditions)
        .order_by(
            channel_videos.c.latest_published_at.desc().nullslast(),
            coalesced_title.asc(),
            coalesced_channel_id,
        )
        .limit(bindparam("limit", value=max(1, min(limit, 200))))
        .offset(bindparam("offset", value=max(0, offset)))
    )
    sql, params = compile_postgres_statement(statement)
    params.setdefault("channel", channel)
    params.setdefault("source_prefix", source_prefix)
    params.setdefault("language", language_value)
    params.setdefault("selected", selected)
    return SqlStatement(sql=sql + ";", params=params)


def format_chunk_resource(row: Mapping[str, Any]) -> dict[str, Any]:
    chunk_id = str(row["chunk_id"])
    video_id = str(row["video_id"])
    start_ms = _seconds_to_ms(row.get("start_seconds"))
    return _compact(
        {
            "chunk_id": chunk_id,
            "resource_uri": f"yutome://chunk/{chunk_id}",
            "video_id": video_id,
            "youtube_video_id": row.get("youtube_video_id"),
            "title": row.get("title"),
            "youtube_url": _youtube_url(str(row.get("youtube_video_id") or video_id), start_ms),
            "start_ms": start_ms,
            "end_ms": _seconds_to_ms(row.get("end_seconds")),
            "text": row.get("text") or "",
            "token_count": _metadata_value(row.get("chunk_metadata"), "token_count"),
            "sequence": row.get("chunk_index"),
            "transcript_version_id": row.get("transcript_version_id"),
            "transcript_source": row.get("transcript_source"),
            "language": row.get("language"),
            "is_generated": _metadata_bool(row.get("transcript_metadata"), "is_generated"),
            "chunker_version": _metadata_value(row.get("chunk_metadata"), "chunking_version"),
            "channel_id": row.get("channel_id"),
            "source_id": row.get("source_id"),
        }
    )


def format_video_resource(row: Mapping[str, Any]) -> dict[str, Any]:
    video_id = str(row["video_id"])
    return _compact(
        {
            "video_id": video_id,
            "resource_uri": f"yutome://video/{video_id}",
            "youtube_video_id": row.get("youtube_video_id"),
            "youtube_url": _youtube_url(str(row.get("youtube_video_id") or video_id), 0),
            "source_id": row.get("source_id"),
            "source_url": row.get("source_url"),
            "source_type": row.get("source_type"),
            "active_transcript_version_id": row.get("active_transcript_version_id"),
            "channel_id": row.get("channel_id"),
            "channel_title": _metadata_value(row.get("metadata_json"), "channel_title") or row.get("source_display_name"),
            "channel_handle": _metadata_value(row.get("metadata_json"), "channel_handle"),
            "title": row.get("title"),
            "description": row.get("description"),
            "published_at": _json_value(row.get("published_at")),
            "duration_seconds": row.get("duration_seconds"),
            "thumbnail_url": _metadata_value(row.get("metadata_json"), "thumbnail_url"),
            "active_chunk_count": row.get("active_chunk_count"),
        }
    )


def format_channel_resource(row: Mapping[str, Any]) -> dict[str, Any]:
    channel_id = str(row["channel_id"])
    return _compact(
        {
            "channel_id": channel_id,
            "resource_uri": f"yutome://channel/{channel_id}",
            "title": row.get("title"),
            "channel_handle": row.get("channel_handle"),
            "video_count": row.get("video_count"),
            "latest_published_at": _json_value(row.get("latest_published_at")),
            "source_ids": list(row.get("source_ids") or []),
        }
    )


def format_transcript_resource(rows: list[Mapping[str, Any]], *, offset: int = 0, limit: int | None = None) -> dict[str, Any]:
    first = rows[0]
    transcript_id = str(first["transcript_version_id"])
    chunks = [row for row in rows if row.get("chunk_id") is not None]
    text_parts = [_timestamped_text(row) for row in chunks]
    text = "\n".join(part for part in text_parts if part)
    text_truncated = len(text) > TRANSCRIPT_TEXT_CAP
    if text_truncated:
        text = text[:TRANSCRIPT_TEXT_CAP]
    returned_segments = len(chunks)
    segment_count = int(first.get("segment_count") or 0)
    next_offset = None
    if limit is not None and offset + returned_segments < segment_count:
        next_offset = offset + returned_segments
    return _compact(
        {
            "resource_uri": f"yutome://transcript/{transcript_id}",
            "transcript_version_id": transcript_id,
            "video_id": first.get("video_id"),
            "youtube_video_id": first.get("youtube_video_id"),
            "source": first.get("source"),
            "language": first.get("language_code"),
            "is_generated": _metadata_bool(first.get("metadata_json"), "is_generated"),
            "segment_count": segment_count,
            "active": bool(first.get("active")),
            "created_at": _json_value(first.get("created_at")),
            "content_hash": first.get("content_hash"),
            "text_truncated": text_truncated,
            "text_char_limit": TRANSCRIPT_TEXT_CAP,
            "offset": offset,
            "limit": limit,
            "returned_segments": returned_segments,
            "next_offset": next_offset,
            "text": text,
        }
    )


def format_context_resource(anchor: Mapping[str, Any], chunks: list[Mapping[str, Any]], *, token_budget: int) -> dict[str, Any]:
    bounded_budget = max(200, min(int(token_budget), 8000))
    anchor_id = str(anchor["chunk_id"])
    anchor_index = next((index for index, row in enumerate(chunks) if str(row.get("chunk_id")) == anchor_id), None)
    if anchor_index is None:
        chunks = [anchor]
        anchor_index = 0
    selected_indexes = {anchor_index}
    total_tokens = _chunk_token_count(chunks[anchor_index])
    left = anchor_index - 1
    right = anchor_index + 1
    while left >= 0 or right < len(chunks):
        added = False
        for index in (left, right):
            if index < 0 or index >= len(chunks) or index in selected_indexes:
                continue
            candidate_tokens = _chunk_token_count(chunks[index])
            if total_tokens + candidate_tokens > bounded_budget and selected_indexes:
                continue
            selected_indexes.add(index)
            total_tokens += candidate_tokens
            added = True
        left -= 1
        right += 1
        if not added:
            break
    selected_chunks = [chunks[index] for index in sorted(selected_indexes)]
    formatted_chunks = [format_chunk_resource(chunk) for chunk in selected_chunks]
    text = _merge_chunk_text([str(chunk.get("text") or "") for chunk in selected_chunks])
    return {
        "anchor": format_chunk_resource(anchor),
        "token_budget": bounded_budget,
        "estimated_tokens": total_tokens,
        "text": text,
        "chunks": formatted_chunks,
        "citations": [
            {
                "chunk_id": chunk["chunk_id"],
                "video_id": chunk["video_id"],
                "youtube_video_id": chunk.get("youtube_video_id"),
                "title": chunk.get("title"),
                "youtube_url": chunk.get("youtube_url"),
                "start_ms": chunk.get("start_ms"),
                "end_ms": chunk.get("end_ms"),
                "transcript_version_id": chunk.get("transcript_version_id"),
                "transcript_source": chunk.get("transcript_source"),
            }
            for chunk in formatted_chunks
        ],
    }


def format_source_resource(row: Mapping[str, Any]) -> dict[str, Any]:
    source_id = str(row["source_id"])
    return _compact(
        {
            "source_id": source_id,
            "resource_uri": f"yutome://source/{source_id}",
            "source_type": row.get("source_type"),
            "source_url": row.get("source_url"),
            "canonical_channel_id": row.get("canonical_channel_id"),
            "canonical_playlist_id": row.get("canonical_playlist_id"),
            "canonical_video_id": row.get("canonical_video_id"),
            "display_name": row.get("display_name"),
            "selected": row.get("selected"),
            "auto_index_allowed": row.get("auto_index_allowed"),
            "import_source": row.get("import_source"),
            "auth_grant_id": row.get("auth_grant_id"),
            "status": row.get("status"),
            "last_discovered_at": _json_value(row.get("last_discovered_at")),
            "last_indexed_at": _json_value(row.get("last_indexed_at")),
            "created_at": _json_value(row.get("created_at")),
            "updated_at": _json_value(row.get("updated_at")),
        }
    )


def format_status_row(row: Mapping[str, Any]) -> dict[str, Any]:
    statuses = row.get("statuses") or {}
    if not isinstance(statuses, Mapping):
        statuses = {}
    return {
        "searchable_now": int(row.get("searchable_now") or 0),
        "still_indexing": int(row.get("still_indexing") or 0),
        "needs_attention": int(row.get("needs_attention") or 0),
        "channels": int(row.get("channels") or 0),
        "videos": int(row.get("videos") or 0),
        "chunks": int(row.get("chunks") or 0),
        "transcript_versions": int(row.get("transcript_versions") or 0),
        "statuses": dict(statuses),
    }


def format_video_list_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return format_video_resource(row)


def format_channel_list_row(row: Mapping[str, Any]) -> dict[str, Any]:
    channel_id = str(row["channel_id"])
    return _compact(
        {
            "channel_id": channel_id,
            "library_channel_id": channel_id,
            "resource_uri": f"yutome://channel/{channel_id}",
            "title": row.get("title"),
            "channel_handle": row.get("channel_handle"),
            "selected": row.get("selected"),
            "video_count": row.get("video_count"),
            "latest_published_at": _json_value(row.get("latest_published_at")),
            "source_count": row.get("source_count"),
            "source_ids": list(row.get("source_ids") or []),
            "last_discovered_at": _json_value(row.get("last_discovered_at")),
            "last_indexed_at": _json_value(row.get("last_indexed_at")),
        }
    )


def _one(result: Any) -> dict[str, Any] | None:
    rows = _rows(result)
    return rows[0] if rows else None


def _rows(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if hasattr(result, "mappings"):
        return [dict(row) for row in result.mappings()]
    if hasattr(result, "fetchall"):
        rows = result.fetchall()
    elif isinstance(result, list):
        rows = result
    else:
        rows = list(result)
    return [dict(row) for row in rows]


def _seconds_to_ms(value: Any) -> int:
    if value is None:
        return 0
    return int(round(float(value) * 1000))


def _youtube_url(video_id: str, start_ms: int) -> str:
    if start_ms <= 0:
        return f"https://youtube.com/watch?v={video_id}"
    return f"https://youtube.com/watch?v={video_id}&t={start_ms // 1000}s"


def _timestamped_text(row: Mapping[str, Any]) -> str:
    start_ms = _seconds_to_ms(row.get("start_seconds"))
    text = str(row.get("text") or "").strip()
    if not text:
        return ""
    return f"[{_format_timestamp(start_ms)}] {text}"


def parse_youtube_location(url: str) -> tuple[str | None, int | None]:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    video_id = query.get("v", [None])[0]
    if parsed.hostname and "youtu.be" in parsed.hostname:
        path_id = parsed.path.strip("/")
        video_id = path_id or video_id
    time_seconds = _parse_time_seconds(query.get("t", [None])[0] or query.get("start", [None])[0])
    return video_id, time_seconds


def _parse_time_seconds(value: str | None) -> int | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized.isdigit():
        return int(normalized)
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?", normalized)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _chunk_token_count(row: Mapping[str, Any]) -> int:
    metadata_count = _metadata_value(row.get("chunk_metadata"), "token_count")
    if isinstance(metadata_count, int) and metadata_count > 0:
        return metadata_count
    return max(1, len(str(row.get("text") or "").split()))


def _merge_chunk_text(parts: list[str]) -> str:
    merged_words: list[str] = []
    for part in parts:
        words = part.split()
        if not words:
            continue
        if not merged_words:
            merged_words.extend(words)
            continue
        max_overlap = min(150, len(merged_words), len(words))
        overlap = 0
        for size in range(max_overlap, 0, -1):
            if merged_words[-size:] == words[:size]:
                overlap = size
                break
        merged_words.extend(words[overlap:])
    return " ".join(merged_words).strip()


def _format_timestamp(ms: int) -> str:
    total = ms // 1000
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _metadata_value(metadata: Any, key: str) -> Any:
    if isinstance(metadata, Mapping):
        return metadata.get(key)
    return None


def _metadata_bool(metadata: Any, key: str) -> bool:
    return bool(_metadata_value(metadata, key))


def _json_value(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _prefix(value: str | None) -> str | None:
    stripped = _blank_to_none(value)
    return f"{stripped}%" if stripped else None


def _status_exact(value: str | None) -> str | None:
    stripped = _blank_to_none(value)
    if not stripped or stripped.endswith("*"):
        return None
    return stripped


def _status_prefix(value: str | None) -> str | None:
    stripped = _blank_to_none(value)
    if not stripped or not stripped.endswith("*"):
        return None
    return f"{stripped[:-1]}%"


def _compact(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


__all__ = [
    "HostedResourceNotFound",
    "HostedResourceQueries",
    "TRANSCRIPT_TEXT_CAP",
    "channel_resource_sql",
    "context_anchor_sql",
    "context_chunks_sql",
    "chunk_resource_sql",
    "format_context_resource",
    "format_channel_list_row",
    "format_status_row",
    "format_video_list_row",
    "list_channels_sql",
    "list_status_sql",
    "list_videos_sql",
    "source_resource_sql",
    "transcript_resource_sql",
    "video_resource_sql",
]
