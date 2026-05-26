from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from yutome.hashing import sha256_text
from yutome.hosted.control_plane import Source, SourceImport, SourceType


def source_from_library_source(
    *,
    workspace_id: str,
    library_source: Any,
    auth_grant_id: str | None = None,
) -> Source:
    source_type = _hosted_source_type(str(library_source.source_type))
    import_source = _hosted_import_source(getattr(library_source, "import_source", None), auth_grant_id=auth_grant_id)
    return Source(
        id=hosted_source_id(workspace_id=workspace_id, source_url=str(library_source.source_url)),
        workspace_id=workspace_id,
        source_type=source_type,
        source_url=str(library_source.source_url),
        canonical_channel_id=getattr(library_source, "channel_id", None),
        canonical_playlist_id=_playlist_id_from_library_source(library_source),
        canonical_video_id=getattr(library_source, "video_id", None),
        display_name=getattr(library_source, "title", None) or getattr(library_source, "handle", None),
        selected=bool(getattr(library_source, "selected", True)),
        auto_index_allowed=bool(getattr(library_source, "selected", True)),
        import_source=import_source,
        auth_grant_id=auth_grant_id,
        metadata_jsonb=_source_metadata(library_source),
    )


def sources_from_library_sources(
    *,
    workspace_id: str,
    library_sources: Iterable[Any],
    auth_grant_id: str | None = None,
) -> list[Source]:
    return [
        source_from_library_source(
            workspace_id=workspace_id,
            library_source=source,
            auth_grant_id=auth_grant_id,
        )
        for source in library_sources
    ]


def subscriptions_source(*, workspace_id: str, auth_grant_id: str) -> Source:
    return Source(
        id=hosted_source_id(workspace_id=workspace_id, source_url="youtube://subscriptions/mine"),
        workspace_id=workspace_id,
        source_type="subscriptions",
        source_url="youtube://subscriptions/mine",
        selected=True,
        auto_index_allowed=True,
        import_source="youtube_oauth",
        auth_grant_id=auth_grant_id,
    )


def hosted_source_id(*, workspace_id: str, source_url: str) -> str:
    return f"src_{sha256_text(f'{workspace_id}:{source_url}')[:24]}"


def provider_credentials_in_source(source: Source) -> set[str]:
    provider_keys = {
        "api_key",
        "apiKey",
        "gemini_api_key",
        "google_api_key",
        "voyage_api_key",
        "webshare_username",
        "webshare_password",
        "proxy_password",
        "client_secret",
        "refresh_token",
        "access_token",
    }
    return provider_keys & set(source.metadata_jsonb)


def _hosted_source_type(source_type: str) -> SourceType:
    if source_type == "youtube_channel":
        return "channel"
    if source_type == "youtube_video":
        return "video"
    if source_type == "youtube_playlist":
        return "playlist"
    if source_type in {"subscriptions", "subscription_collection", "channel", "handle", "playlist", "video", "url"}:
        return source_type
    return "url"


def _hosted_import_source(import_source: str | None, *, auth_grant_id: str | None) -> SourceImport:
    if auth_grant_id is not None:
        return "oauth_sync"
    if not import_source:
        return "manual"
    normalized = import_source.replace("-", "_")
    if normalized == "youtube_oauth":
        return "youtube_oauth"
    if normalized.startswith("csv:") or normalized.startswith("opml:") or normalized.startswith("list:"):
        return "manual"
    if normalized in {"manual", "manual_url", "public_api", "public_scrape", "yt_dlp", "onboarding", "cli"}:
        return normalized
    return "manual_url"


def _playlist_id_from_library_source(library_source: Any) -> str | None:
    source = str(getattr(library_source, "source", ""))
    if source.startswith("youtube:playlist:"):
        return source.removeprefix("youtube:playlist:")
    return None


def _source_metadata(library_source: Any) -> dict[str, Any]:
    metadata = {
        "legacy_source_id": getattr(library_source, "source_id", None),
        "legacy_source": getattr(library_source, "source", None),
        "handle": getattr(library_source, "handle", None),
    }
    return {key: value for key, value in metadata.items() if value is not None}


__all__ = [
    "hosted_source_id",
    "provider_credentials_in_source",
    "source_from_library_source",
    "sources_from_library_sources",
    "subscriptions_source",
]
