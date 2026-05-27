from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol
from urllib.parse import parse_qs, urlparse, urlunparse

from yutome.hashing import sha256_text
from yutome.hosted.repositories import SqlStatement


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}


@dataclass(frozen=True)
class LibraryChannel:
    library_channel_id: str
    source: str
    source_url: str
    channel_id: str | None = None
    handle: str | None = None
    title: str | None = None
    selected: bool = True
    import_source: str | None = None


@dataclass(frozen=True)
class ChannelImportStats:
    imported: int
    skipped: int = 0


def channel_from_input(
    value: str,
    *,
    title: str | None = None,
    import_source: str | None = None,
) -> LibraryChannel | None:
    raw = value.strip()
    if not raw:
        return None
    channel_id = _channel_id_from_value(raw)
    handle = _handle_from_value(raw)
    source_url = _canonical_source_url(raw, channel_id=channel_id, handle=handle)
    source = _channel_source_key(source_url, channel_id=channel_id, handle=handle)
    return LibraryChannel(
        library_channel_id=sha256_text(source)[:24],
        source=source,
        source_url=source_url,
        channel_id=channel_id,
        handle=handle,
        title=title.strip() if title and title.strip() else None,
        import_source=import_source,
    )


def import_channels_from_file(path: Path, *, selected: bool = True) -> list[LibraryChannel]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return list(_channels_from_csv(path, selected=selected))
    if suffix in {".opml", ".xml"}:
        return list(_channels_from_opml(path, selected=selected))
    return list(_channels_from_plain_list(path, selected=selected))


class SqlConnection(Protocol):
    def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> Any:
        ...


def upsert_library_channel(
    connection: SqlConnection,
    channel: LibraryChannel,
    *,
    workspace_id: str,
    selected: bool | None = None,
) -> None:
    statement = upsert_library_channel_sql(channel, workspace_id=workspace_id, selected=selected)
    connection.execute(statement.sql, statement.params)


def upsert_library_channel_sql(
    channel: LibraryChannel,
    *,
    workspace_id: str,
    selected: bool | None = None,
) -> SqlStatement:
    effective_selected = channel.selected if selected is None else selected
    return SqlStatement(
        sql="""
INSERT INTO sources (
    id, workspace_id, source_type, source_url, canonical_channel_id,
    display_name, selected, auto_index_allowed, import_source, metadata_json, status
)
VALUES (
    %(id)s, %(workspace_id)s, %(source_type)s, %(source_url)s,
    %(canonical_channel_id)s, %(display_name)s, %(selected)s, true,
    %(import_source)s, %(metadata_json)s::jsonb, 'active'
)
ON CONFLICT (workspace_id, source_url) DO UPDATE
SET source_type = EXCLUDED.source_type,
    canonical_channel_id = COALESCE(EXCLUDED.canonical_channel_id, sources.canonical_channel_id),
    display_name = COALESCE(EXCLUDED.display_name, sources.display_name),
    selected = EXCLUDED.selected,
    import_source = EXCLUDED.import_source,
    metadata_json = sources.metadata_json || EXCLUDED.metadata_json,
    status = EXCLUDED.status,
    updated_at = now()
RETURNING *;
""".strip(),
        params={
            "id": channel.library_channel_id,
            "workspace_id": workspace_id,
            "source_type": _postgres_channel_source_type(channel),
            "source_url": channel.source_url,
            "canonical_channel_id": channel.channel_id,
            "display_name": channel.title,
            "selected": effective_selected,
            "import_source": channel.import_source or "manual",
            "metadata_json": _json_object(
                {
                    "source": channel.source,
                    "handle": channel.handle,
                }
            ),
        },
    )


def list_library_channels(
    connection: SqlConnection,
    *,
    workspace_id: str,
    selected_only: bool = False,
) -> list[LibraryChannel]:
    statement = list_library_channels_sql(workspace_id=workspace_id, selected_only=selected_only)
    rows = _rows_from_result(connection.execute(statement.sql, statement.params))
    return [_channel_from_postgres_row(row) for row in rows]


def list_library_channels_sql(*, workspace_id: str, selected_only: bool = False) -> SqlStatement:
    return SqlStatement(
        sql="""
SELECT
    id AS library_channel_id,
    source_type,
    source_url,
    canonical_channel_id AS channel_id,
    display_name AS title,
    selected,
    import_source,
    metadata_json
FROM sources
WHERE workspace_id = %(workspace_id)s
  AND source_type IN ('channel', 'handle', 'url')
  AND (%(selected_only)s::boolean = false OR selected = true)
ORDER BY selected DESC, COALESCE(display_name, metadata_json->>'handle', source_url), source_url;
""".strip(),
        params={"workspace_id": workspace_id, "selected_only": selected_only},
    )


def set_library_channel_selected(
    connection: SqlConnection,
    *,
    workspace_id: str,
    selector: str,
    selected: bool,
) -> int:
    candidates = _selector_candidates(selector)
    statement = set_library_channel_selected_sql(
        workspace_id=workspace_id,
        candidates=candidates,
        selected=selected,
        all_channels=selector == "all",
    )
    cursor = connection.execute(statement.sql, statement.params)
    return int(getattr(cursor, "rowcount", 0) or 0)


def set_library_channel_selected_sql(
    *,
    workspace_id: str,
    candidates: list[str],
    selected: bool,
    all_channels: bool = False,
) -> SqlStatement:
    return SqlStatement(
        sql="""
UPDATE sources
SET selected = %(selected)s,
    updated_at = now()
WHERE workspace_id = %(workspace_id)s
  AND source_type IN ('channel', 'handle', 'url')
  AND (
    %(all_channels)s::boolean = true
    OR id = ANY(%(candidates)s::text[])
    OR source_url = ANY(%(candidates)s::text[])
    OR canonical_channel_id = ANY(%(candidates)s::text[])
    OR display_name = ANY(%(candidates)s::text[])
    OR metadata_json->>'source' = ANY(%(candidates)s::text[])
    OR metadata_json->>'handle' = ANY(%(candidates)s::text[])
    OR ('@' || metadata_json->>'handle') = ANY(%(candidates)s::text[])
  );
""".strip(),
        params={
            "workspace_id": workspace_id,
            "selected": selected,
            "all_channels": all_channels,
            "candidates": candidates,
        },
    )


def _channels_from_csv(path: Path, *, selected: bool) -> Iterable[LibraryChannel]:
    with path.open("r", encoding="utf-8-sig", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        for row in reader:
            normalized = {_normalize_header(key): value for key, value in row.items() if key is not None}
            channel_id = _first_value(normalized, "channelid", "channel_id", "id")
            url = _first_value(normalized, "channelurl", "channel_url", "url")
            title = _first_value(normalized, "channeltitle", "channel_title", "title", "name")
            value = url or channel_id
            if not value:
                continue
            channel = channel_from_input(value, title=title, import_source=f"csv:{path.name}")
            if channel is not None:
                yield _with_selected(channel, selected)


def _channels_from_opml(path: Path, *, selected: bool) -> Iterable[LibraryChannel]:
    root = ET.parse(path).getroot()
    for outline in root.iter("outline"):
        url = outline.attrib.get("htmlUrl") or outline.attrib.get("url") or outline.attrib.get("xmlUrl")
        if not url:
            continue
        title = outline.attrib.get("title") or outline.attrib.get("text")
        channel = channel_from_input(url, title=title, import_source=f"opml:{path.name}")
        if channel is not None:
            yield _with_selected(channel, selected)


def _channels_from_plain_list(path: Path, *, selected: bool) -> Iterable[LibraryChannel]:
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        channel = channel_from_input(stripped, import_source=f"list:{path.name}")
        if channel is not None:
            yield _with_selected(channel, selected)


def _with_selected(channel: LibraryChannel, selected: bool) -> LibraryChannel:
    return LibraryChannel(
        library_channel_id=channel.library_channel_id,
        source=channel.source,
        source_url=channel.source_url,
        channel_id=channel.channel_id,
        handle=channel.handle,
        title=channel.title,
        selected=selected,
        import_source=channel.import_source,
    )


def _postgres_channel_source_type(channel: LibraryChannel) -> str:
    if channel.channel_id:
        return "channel"
    if channel.handle:
        return "handle"
    return "url"


def _channel_from_postgres_row(row: Mapping[str, Any]) -> LibraryChannel:
    metadata = _metadata(row.get("metadata_json"))
    channel_id = _optional_str(row.get("channel_id"))
    handle = _optional_str(metadata.get("handle")) or _handle_from_value(str(row.get("source_url") or ""))
    source_url = str(row["source_url"])
    source = _optional_str(metadata.get("source")) or _channel_source_key(
        source_url,
        channel_id=channel_id,
        handle=handle,
    )
    return LibraryChannel(
        library_channel_id=str(row["library_channel_id"]),
        source=source,
        source_url=source_url,
        channel_id=channel_id,
        handle=handle,
        title=_optional_str(row.get("title")),
        selected=bool(row.get("selected")),
        import_source=_optional_str(row.get("import_source")),
    )


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        import json

        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_object(value: Mapping[str, Any]) -> str:
    import json

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


def _canonical_source_url(raw: str, *, channel_id: str | None, handle: str | None) -> str:
    if channel_id:
        return f"https://www.youtube.com/channel/{channel_id}"
    if handle:
        return f"https://www.youtube.com/@{handle.lstrip('@')}"
    if raw.startswith("@"):
        return f"https://www.youtube.com/{raw}"
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        return f"https://www.youtube.com/{raw.lstrip('/')}"
    parsed = urlparse(raw)
    host = "www.youtube.com" if parsed.netloc in YOUTUBE_HOSTS else parsed.netloc
    return urlunparse(("https", host, parsed.path.rstrip("/") or "/", "", parsed.query, ""))


def _channel_source_key(source_url: str, *, channel_id: str | None, handle: str | None) -> str:
    if channel_id:
        return f"youtube:channel:{channel_id}"
    if handle:
        return f"youtube:handle:{handle.lower().lstrip('@')}"
    return f"youtube:url:{source_url.lower()}"


def _channel_id_from_value(value: str) -> str | None:
    stripped = value.strip()
    if re.fullmatch(r"UC[A-Za-z0-9_-]{20,}", stripped):
        return stripped
    parsed = urlparse(stripped if "://" in stripped else f"https://www.youtube.com/{stripped.lstrip('/')}")
    query = parse_qs(parsed.query)
    if channel_id := query.get("channel_id", [None])[0]:
        return channel_id
    match = re.search(r"/channel/(UC[A-Za-z0-9_-]+)", parsed.path)
    return match.group(1) if match else None


def _handle_from_value(value: str) -> str | None:
    stripped = value.strip()
    if stripped.startswith("@"):
        return stripped.lstrip("@")
    parsed = urlparse(stripped if "://" in stripped else f"https://www.youtube.com/{stripped.lstrip('/')}")
    match = re.search(r"/@([^/?#]+)", parsed.path)
    return match.group(1) if match else None


def _selector_candidates(selector: str) -> list[str]:
    candidates = [selector]
    if channel := channel_from_input(selector):
        candidates.extend([channel.library_channel_id, channel.source, channel.source_url])
        if channel.channel_id:
            candidates.append(channel.channel_id)
        if channel.handle:
            candidates.extend([channel.handle, f"@{channel.handle}"])
    return sorted(set(candidates))


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "", value.strip().lower().replace(" ", "_"))


def _first_value(row: dict[str, str | None], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value and value.strip():
            return value.strip()
    return None
