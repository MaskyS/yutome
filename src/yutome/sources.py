from __future__ import annotations

import csv
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal
from urllib.parse import parse_qs, urlparse

from yutome.channels import LibraryChannel, channel_from_input, upsert_library_channel
from yutome.hashing import sha256_text
from yutome.youtube import canonical_video_url, extract_video_id


SourceType = Literal["youtube_channel", "youtube_video", "youtube_playlist"]
@dataclass(frozen=True)
class LibrarySource:
    source_id: str
    source_type: SourceType
    source: str
    source_url: str
    channel_id: str | None = None
    video_id: str | None = None
    handle: str | None = None
    title: str | None = None
    selected: bool = True
    import_source: str | None = None


def source_from_channel(channel: LibraryChannel) -> LibrarySource:
    return LibrarySource(
        source_id=channel.library_channel_id,
        source_type="youtube_channel",
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
            source_type="youtube_video",
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
            source_type="youtube_playlist",
            source=source,
            source_url=_canonical_playlist_url(playlist_id),
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
    connection: sqlite3.Connection,
    source: LibrarySource,
    *,
    selected: bool | None = None,
) -> None:
    effective_selected = source.selected if selected is None else selected
    if source.source_type == "youtube_channel":
        upsert_library_channel(connection, _source_to_channel(source), selected=effective_selected)
    connection.execute(
        """
        INSERT INTO library_sources(
            source_id, source_type, source, source_url, channel_id, video_id,
            handle, title, selected, import_source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_url) DO UPDATE SET
            source_type = excluded.source_type,
            source = excluded.source,
            channel_id = COALESCE(excluded.channel_id, library_sources.channel_id),
            video_id = COALESCE(excluded.video_id, library_sources.video_id),
            handle = COALESCE(excluded.handle, library_sources.handle),
            title = COALESCE(excluded.title, library_sources.title),
            selected = excluded.selected,
            import_source = COALESCE(excluded.import_source, library_sources.import_source),
            updated_at = datetime('now')
        """,
        (
            source.source_id,
            source.source_type,
            source.source,
            source.source_url,
            source.channel_id,
            source.video_id,
            source.handle,
            source.title,
            1 if effective_selected else 0,
            source.import_source,
        ),
    )


def list_library_sources(
    connection: sqlite3.Connection,
    *,
    selected_only: bool = False,
) -> list[LibrarySource]:
    where = "WHERE selected = 1" if selected_only else ""
    rows = connection.execute(
        f"""
        SELECT source_id, source_type, source, source_url, channel_id, video_id,
               handle, title, selected, import_source
        FROM library_sources
        {where}
        ORDER BY selected DESC, source_type, COALESCE(title, handle, video_id, source_url), source_url
        """
    ).fetchall()
    return [_source_from_row(row) for row in rows]


def set_library_source_selected(
    connection: sqlite3.Connection,
    *,
    selector: str,
    selected: bool,
) -> int:
    if selector == "all":
        source_cursor = connection.execute(
            "UPDATE library_sources SET selected = ?, updated_at = datetime('now')",
            (1 if selected else 0,),
        )
        connection.execute(
            "UPDATE library_channels SET selected = ?, updated_at = datetime('now')",
            (1 if selected else 0,),
        )
        return source_cursor.rowcount
    candidates = _selector_candidates(selector)
    placeholders = ",".join("?" for _ in candidates)
    source_cursor = connection.execute(
        f"""
        UPDATE library_sources
        SET selected = ?, updated_at = datetime('now')
        WHERE source_id IN ({placeholders})
           OR source_url IN ({placeholders})
           OR source IN ({placeholders})
           OR channel_id IN ({placeholders})
           OR video_id IN ({placeholders})
           OR handle IN ({placeholders})
           OR title IN ({placeholders})
        """,
        (
            1 if selected else 0,
            *candidates,
            *candidates,
            *candidates,
            *candidates,
            *candidates,
            *candidates,
            *candidates,
        ),
    )
    connection.execute(
        f"""
        UPDATE library_channels
        SET selected = ?, updated_at = datetime('now')
        WHERE library_channel_id IN ({placeholders})
           OR source_url IN ({placeholders})
           OR source IN ({placeholders})
           OR channel_id IN ({placeholders})
           OR handle IN ({placeholders})
           OR title IN ({placeholders})
        """,
        (1 if selected else 0, *candidates, *candidates, *candidates, *candidates, *candidates, *candidates),
    )
    return source_cursor.rowcount


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


def _source_from_row(row: sqlite3.Row) -> LibrarySource:
    return LibrarySource(
        source_id=row["source_id"],
        source_type=row["source_type"],
        source=row["source"],
        source_url=row["source_url"],
        channel_id=row["channel_id"],
        video_id=row["video_id"],
        handle=row["handle"],
        title=row["title"],
        selected=bool(row["selected"]),
        import_source=row["import_source"],
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
        video_id=source.video_id,
        handle=source.handle,
        title=source.title,
        selected=selected,
        import_source=source.import_source,
    )


def _selector_candidates(selector: str) -> list[str]:
    candidates = {selector.strip()}
    source = source_from_input(selector)
    if source is not None:
        candidates.update({source.source_id, source.source, source.source_url})
        if source.channel_id:
            candidates.add(source.channel_id)
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
