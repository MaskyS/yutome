from __future__ import annotations

import csv
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Protocol
from urllib.parse import parse_qs, urlparse

from yutome.channels import LibraryChannel, channel_from_input
from yutome.hashing import sha256_text
from yutome.hosted.repositories import SqlStatement
from yutome.youtube import canonical_video_url, extract_video_id


SourceType = Literal["channel", "handle", "playlist", "video", "url"]


class SqlConnection(Protocol):
    def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> Any:
        ...


@dataclass(frozen=True)
class LibrarySource:
    source_id: str
    source_type: SourceType
    source: str
    source_url: str
    channel_id: str | None = None
    playlist_id: str | None = None
    video_id: str | None = None
    handle: str | None = None
    title: str | None = None
    selected: bool = True
    import_source: str | None = None


def source_from_channel(channel: LibraryChannel) -> LibrarySource:
    return LibrarySource(
        source_id=channel.library_channel_id,
        source_type=_source_type_from_channel(channel),
        source=channel.source,
        source_url=channel.source_url,
        channel_id=channel.channel_id,
        handle=channel.handle,
        title=channel.title,
        selected=channel.selected,
        import_source=channel.import_source,
    )


def source_from_input(
    value: str,
    *,
    title: str | None = None,
    import_source: str | None = None,
) -> LibrarySource | None:
    raw = value.strip()
    if not raw:
        return None
    if video_id := extract_video_id(raw):
        source = f"youtube:video:{video_id}"
        return LibrarySource(
            source_id=sha256_text(source)[:24],
            source_type="video",
            source=source,
            source_url=canonical_video_url(video_id),
            video_id=video_id,
            title=title.strip() if title and title.strip() else None,
            import_source=import_source,
        )
    if playlist_id := _playlist_id_from_value(raw):
        source = f"youtube:playlist:{playlist_id}"
        return LibrarySource(
            source_id=sha256_text(source)[:24],
            source_type="playlist",
            source=source,
            source_url=_canonical_playlist_url(playlist_id),
            playlist_id=playlist_id,
            title=title.strip() if title and title.strip() else None,
            import_source=import_source,
        )
    channel = channel_from_input(raw, title=title, import_source=import_source)
    return source_from_channel(channel) if channel is not None else None


def import_sources_from_file(path: Path, *, selected: bool = True) -> list[LibrarySource]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return list(_sources_from_csv(path, selected=selected))
    if suffix in {".opml", ".xml"}:
        return list(_sources_from_opml(path, selected=selected))
    return list(_sources_from_plain_list(path, selected=selected))


def upsert_library_source(
    connection: SqlConnection,
    source: LibrarySource,
    *,
    workspace_id: str,
    selected: bool | None = None,
) -> None:
    statement = upsert_library_source_sql(source, workspace_id=workspace_id, selected=selected)
    connection.execute(statement.sql, statement.params)


def upsert_library_source_sql(
    source: LibrarySource,
    *,
    workspace_id: str,
    selected: bool | None = None,
) -> SqlStatement:
    effective_selected = source.selected if selected is None else selected
    return SqlStatement(
        sql="""
INSERT INTO sources (
    id, workspace_id, source_type, source_url, canonical_channel_id,
    canonical_playlist_id, canonical_video_id, display_name, selected,
    auto_index_allowed, import_source, metadata_json, status
)
VALUES (
    %(id)s, %(workspace_id)s, %(source_type)s, %(source_url)s,
    %(canonical_channel_id)s, %(canonical_playlist_id)s, %(canonical_video_id)s,
    %(display_name)s, %(selected)s, true, %(import_source)s,
    %(metadata_json)s::jsonb, 'active'
)
ON CONFLICT (workspace_id, source_url) DO UPDATE
SET source_type = EXCLUDED.source_type,
    canonical_channel_id = COALESCE(EXCLUDED.canonical_channel_id, sources.canonical_channel_id),
    canonical_playlist_id = COALESCE(EXCLUDED.canonical_playlist_id, sources.canonical_playlist_id),
    canonical_video_id = COALESCE(EXCLUDED.canonical_video_id, sources.canonical_video_id),
    display_name = COALESCE(EXCLUDED.display_name, sources.display_name),
    selected = EXCLUDED.selected,
    import_source = EXCLUDED.import_source,
    metadata_json = sources.metadata_json || EXCLUDED.metadata_json,
    status = EXCLUDED.status,
    updated_at = now()
RETURNING *;
""".strip(),
        params={
            "id": source.source_id,
            "workspace_id": workspace_id,
            "source_type": source.source_type,
            "source_url": source.source_url,
            "canonical_channel_id": source.channel_id,
            "canonical_playlist_id": source.playlist_id,
            "canonical_video_id": source.video_id,
            "display_name": source.title,
            "selected": effective_selected,
            "import_source": source.import_source or "manual",
            "metadata_json": _json_object(
                {
                    "source": source.source,
                    "handle": source.handle,
                }
            ),
        },
    )


def list_library_sources(
    connection: SqlConnection,
    *,
    workspace_id: str,
    selected_only: bool = False,
) -> list[LibrarySource]:
    statement = list_library_sources_sql(workspace_id=workspace_id, selected_only=selected_only)
    rows = _rows_from_result(connection.execute(statement.sql, statement.params))
    return [_source_from_row(row) for row in rows]


def list_library_sources_sql(*, workspace_id: str, selected_only: bool = False) -> SqlStatement:
    return SqlStatement(
        sql="""
SELECT
    id AS source_id,
    source_type,
    source_url,
    canonical_channel_id AS channel_id,
    canonical_playlist_id AS playlist_id,
    canonical_video_id AS video_id,
    display_name AS title,
    selected,
    import_source,
    metadata_json
FROM sources
WHERE workspace_id = %(workspace_id)s
  AND (%(selected_only)s::boolean = false OR selected = true)
ORDER BY selected DESC, source_type, COALESCE(display_name, canonical_video_id, canonical_playlist_id, canonical_channel_id, source_url), source_url;
""".strip(),
        params={"workspace_id": workspace_id, "selected_only": selected_only},
    )


def set_library_source_selected(
    connection: SqlConnection,
    *,
    workspace_id: str,
    selector: str,
    selected: bool,
) -> int:
    candidates = _selector_candidates(selector)
    statement = set_library_source_selected_sql(
        workspace_id=workspace_id,
        candidates=candidates,
        selected=selected,
        all_sources=selector == "all",
    )
    cursor = connection.execute(statement.sql, statement.params)
    return int(getattr(cursor, "rowcount", 0) or 0)


def set_library_source_selected_sql(
    *,
    workspace_id: str,
    candidates: list[str],
    selected: bool,
    all_sources: bool = False,
) -> SqlStatement:
    return SqlStatement(
        sql="""
UPDATE sources
SET selected = %(selected)s,
    updated_at = now()
WHERE workspace_id = %(workspace_id)s
  AND (
    %(all_sources)s::boolean = true
    OR id = ANY(%(candidates)s::text[])
    OR source_url = ANY(%(candidates)s::text[])
    OR canonical_channel_id = ANY(%(candidates)s::text[])
    OR canonical_playlist_id = ANY(%(candidates)s::text[])
    OR canonical_video_id = ANY(%(candidates)s::text[])
    OR display_name = ANY(%(candidates)s::text[])
    OR metadata_json->>'source' = ANY(%(candidates)s::text[])
    OR metadata_json->>'handle' = ANY(%(candidates)s::text[])
    OR ('@' || metadata_json->>'handle') = ANY(%(candidates)s::text[])
  );
""".strip(),
        params={
            "workspace_id": workspace_id,
            "selected": selected,
            "all_sources": all_sources,
            "candidates": candidates,
        },
    )


def _source_to_channel(source: LibrarySource) -> LibraryChannel:
    return LibraryChannel(
        library_channel_id=source.source_id,
        source=source.source,
        source_url=source.source_url,
        channel_id=source.channel_id,
        handle=source.handle,
        title=source.title,
        selected=source.selected,
        import_source=source.import_source,
    )


def _source_from_row(row: Mapping[str, Any]) -> LibrarySource:
    metadata = _metadata(row.get("metadata_json"))
    source_url = str(row["source_url"])
    source_type = _source_type_from_row(row)
    channel_id = _optional_str(row.get("channel_id"))
    playlist_id = _optional_str(row.get("playlist_id"))
    video_id = _optional_str(row.get("video_id"))
    handle = _optional_str(metadata.get("handle")) or _handle_from_url(source_url)
    return LibrarySource(
        source_id=str(row["source_id"]),
        source_type=source_type,
        source=_optional_str(metadata.get("source"))
        or _canonical_source_key(source_url, source_type=source_type, channel_id=channel_id, playlist_id=playlist_id, video_id=video_id, handle=handle),
        source_url=source_url,
        channel_id=channel_id,
        playlist_id=playlist_id,
        video_id=video_id,
        handle=handle,
        title=_optional_str(row.get("title")),
        selected=bool(row.get("selected")),
        import_source=_optional_str(row.get("import_source")),
    )


def _sources_from_csv(path: Path, *, selected: bool) -> Iterable[LibrarySource]:
    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        for row in reader:
            normalized = {_normalize_header(key): value for key, value in row.items() if key is not None}
            channel_id = _first_value(normalized, "channelid", "channel_id", "id")
            url = _first_value(normalized, "channelurl", "channel_url", "videourl", "video_url", "url")
            title = _first_value(normalized, "channeltitle", "channel_title", "videotitle", "video_title", "title", "name")
            value = url or channel_id
            if not value:
                continue
            source = source_from_input(value, title=title, import_source=f"csv:{path.name}")
            if source is not None:
                yield _with_selected(source, selected)


def _sources_from_opml(path: Path, *, selected: bool) -> Iterable[LibrarySource]:
    root = ET.parse(path).getroot()
    for outline in root.iter("outline"):
        url = outline.attrib.get("htmlUrl") or outline.attrib.get("url") or outline.attrib.get("xmlUrl")
        if not url:
            continue
        title = outline.attrib.get("title") or outline.attrib.get("text")
        source = source_from_input(url, title=title, import_source=f"opml:{path.name}")
        if source is not None:
            yield _with_selected(source, selected)


def _sources_from_plain_list(path: Path, *, selected: bool) -> Iterable[LibrarySource]:
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        source = source_from_input(stripped, import_source=f"list:{path.name}")
        if source is not None:
            yield _with_selected(source, selected)


def _with_selected(source: LibrarySource, selected: bool) -> LibrarySource:
    return LibrarySource(
        source_id=source.source_id,
        source_type=source.source_type,
        source=source.source,
        source_url=source.source_url,
        channel_id=source.channel_id,
        playlist_id=source.playlist_id,
        video_id=source.video_id,
        handle=source.handle,
        title=source.title,
        selected=selected,
        import_source=source.import_source,
    )


def _source_type_from_channel(channel: LibraryChannel) -> SourceType:
    if channel.channel_id:
        return "channel"
    if channel.handle:
        return "handle"
    return "url"


def _source_type_from_row(row: Mapping[str, Any]) -> SourceType:
    value = str(row.get("source_type") or "url")
    if value in {"channel", "handle", "playlist", "video", "url"}:
        return value  # type: ignore[return-value]
    if value == "youtube_channel":
        return "channel"
    if value == "youtube_playlist":
        return "playlist"
    if value == "youtube_video":
        return "video"
    return "url"


def _canonical_source_key(
    source_url: str,
    *,
    source_type: SourceType,
    channel_id: str | None,
    playlist_id: str | None,
    video_id: str | None,
    handle: str | None,
) -> str:
    if source_type == "video" and video_id:
        return f"youtube:video:{video_id}"
    if source_type == "playlist" and playlist_id:
        return f"youtube:playlist:{playlist_id}"
    if source_type == "channel" and channel_id:
        return f"youtube:channel:{channel_id}"
    if source_type == "handle" and handle:
        return f"youtube:handle:{handle.lower().lstrip('@')}"
    return f"youtube:url:{source_url.lower()}"


def _handle_from_url(value: str) -> str | None:
    parsed = urlparse(value if "://" in value else f"https://www.youtube.com/{value.lstrip('/')}")
    match = re.search(r"/@([^/?#]+)", parsed.path)
    return match.group(1) if match else None


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_object(value: Mapping[str, Any]) -> str:
    compact = {key: item for key, item in value.items() if item is not None}
    return json.dumps(compact, sort_keys=True, separators=(",", ":"))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _rows_from_result(result: Any) -> list[dict[str, Any]]:
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


def _selector_candidates(selector: str) -> list[str]:
    candidates = {selector.strip()}
    source = source_from_input(selector)
    if source is not None:
        candidates.update({source.source_id, source.source, source.source_url})
        if source.channel_id:
            candidates.add(source.channel_id)
        if source.playlist_id:
            candidates.add(source.playlist_id)
        if source.video_id:
            candidates.add(source.video_id)
        if source.handle:
            candidates.add(source.handle)
            candidates.add(f"@{source.handle}")
    return sorted(candidate for candidate in candidates if candidate)


def _playlist_id_from_value(value: str) -> str | None:
    stripped = value.strip()
    parsed = urlparse(stripped if "://" in stripped else f"https://www.youtube.com/{stripped.lstrip('/')}")
    query = parse_qs(parsed.query)
    if playlist_id := query.get("list", [None])[0]:
        return playlist_id
    if re.fullmatch(r"(PL|UU|LL|RD|OLAK5uy_)[A-Za-z0-9_-]{10,}", stripped):
        return stripped
    return None


def _canonical_playlist_url(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "", value.strip().lower().replace(" ", "_"))


def _first_value(row: dict[str, str | None], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value and value.strip():
            return value.strip()
    return None
