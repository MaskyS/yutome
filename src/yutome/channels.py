from __future__ import annotations

import csv
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse, urlunparse

from yutome.hashing import sha256_text


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


def upsert_library_channel(
    connection: sqlite3.Connection,
    channel: LibraryChannel,
    *,
    selected: bool | None = None,
) -> None:
    if channel.channel_id:
        connection.execute(
            """
            INSERT INTO channels(channel_id, handle, source_url, title)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                handle = COALESCE(channels.handle, excluded.handle),
                source_url = COALESCE(channels.source_url, excluded.source_url),
                title = COALESCE(channels.title, excluded.title)
            """,
            (channel.channel_id, channel.handle, channel.source_url, channel.title),
        )
    effective_selected = channel.selected if selected is None else selected
    connection.execute(
        """
        INSERT INTO library_channels(
            library_channel_id, source, source_url, channel_id, handle, title, selected, import_source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_url) DO UPDATE SET
            source = excluded.source,
            channel_id = COALESCE(excluded.channel_id, library_channels.channel_id),
            handle = COALESCE(excluded.handle, library_channels.handle),
            title = COALESCE(excluded.title, library_channels.title),
            selected = excluded.selected,
            import_source = COALESCE(excluded.import_source, library_channels.import_source),
            updated_at = datetime('now')
        """,
        (
            channel.library_channel_id,
            channel.source,
            channel.source_url,
            channel.channel_id,
            channel.handle,
            channel.title,
            1 if effective_selected else 0,
            channel.import_source,
        ),
    )


def list_library_channels(
    connection: sqlite3.Connection,
    *,
    selected_only: bool = False,
) -> list[LibraryChannel]:
    where = "WHERE selected = 1" if selected_only else ""
    rows = connection.execute(
        f"""
        SELECT library_channel_id, source, source_url, channel_id, handle, title, selected, import_source
        FROM library_channels
        {where}
        ORDER BY selected DESC, COALESCE(title, handle, source_url), source_url
        """
    ).fetchall()
    return [
        LibraryChannel(
            library_channel_id=row["library_channel_id"],
            source=row["source"],
            source_url=row["source_url"],
            channel_id=row["channel_id"],
            handle=row["handle"],
            title=row["title"],
            selected=bool(row["selected"]),
            import_source=row["import_source"],
        )
        for row in rows
    ]


def set_library_channel_selected(
    connection: sqlite3.Connection,
    *,
    selector: str,
    selected: bool,
) -> int:
    if selector == "all":
        cursor = connection.execute(
            "UPDATE library_channels SET selected = ?, updated_at = datetime('now')",
            (1 if selected else 0,),
        )
        return cursor.rowcount
    candidates = _selector_candidates(selector)
    placeholders = ",".join("?" for _ in candidates)
    cursor = connection.execute(
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
    return cursor.rowcount


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
