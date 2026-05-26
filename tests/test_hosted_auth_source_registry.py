from __future__ import annotations

from datetime import datetime, timedelta, timezone

from yutome.hosted.auth import access_token_props_from_grant, provider_credential_keys_in_mapping
from yutome.hosted.control_plane import AccountGrant, YouTubeGrant, discoverable_sources, source_discovery_decision
from yutome.hosted.source_registry import (
    provider_credentials_in_source,
    source_from_library_source,
    sources_from_library_sources,
    subscriptions_source,
)
from yutome.sources import source_from_input


NOW = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)


def test_access_token_props_from_account_grant_include_workspace_scopes_not_credentials() -> None:
    grant = AccountGrant(
        id="grant_mcp",
        user_id="usr_alice",
        workspace_id="ws_alice",
        kind="mcp_client",
        scopes={"mcp:query", "sources:read"},
        audience="mcp",
        client_id="client_chatgpt",
        install_id="install_1",
        expires_at=NOW + timedelta(hours=1),
        metadata_jsonb={"provider": "gemini"},
    )

    props = access_token_props_from_grant(grant)
    claims = props.public_claims()

    assert claims["workspace_id"] == "ws_alice"
    assert claims["grant_id"] == "grant_mcp"
    assert claims["scopes"] == ["mcp:query", "sources:read"]
    assert props.allows_scope("mcp:query") is True
    assert provider_credential_keys_in_mapping(claims) == set()
    assert "provider" not in claims


def test_provider_credential_key_detection_scans_nested_claims() -> None:
    assert provider_credential_keys_in_mapping({"nested": {"voyage_api_key": "secret"}}) == {"voyage_api_key"}


def test_source_registry_maps_public_local_sources_to_hosted_sources_without_auth_grant() -> None:
    library_source = source_from_input("https://www.youtube.com/watch?v=OEDoJyhQhXs", title="Test video", import_source="cli")
    assert library_source is not None

    source = source_from_library_source(workspace_id="ws_alice", library_source=library_source)

    assert source.workspace_id == "ws_alice"
    assert source.source_type == "video"
    assert source.import_source == "cli"
    assert source.auth_grant_id is None
    assert source.canonical_video_id == "OEDoJyhQhXs"
    assert source.requires_youtube_grant is False
    assert source.is_public_source is True
    assert provider_credentials_in_source(source) == set()


def test_source_registry_maps_oauth_subscription_and_synced_channels_separately() -> None:
    grant = YouTubeGrant(
        id="yt_grant",
        user_id="usr_alice",
        workspace_id="ws_alice",
        expires_at=NOW + timedelta(hours=1),
    )
    channel = source_from_input("UC1234567890123456789012", title="Subscribed", import_source="youtube-oauth")
    assert channel is not None

    subscription_root = subscriptions_source(workspace_id="ws_alice", auth_grant_id=grant.id)
    synced_sources = sources_from_library_sources(
        workspace_id="ws_alice",
        library_sources=[channel],
        auth_grant_id=grant.id,
    )

    assert subscription_root.source_type == "subscriptions"
    assert subscription_root.requires_youtube_grant is True
    assert synced_sources[0].source_type == "channel"
    assert synced_sources[0].import_source == "oauth_sync"
    assert synced_sources[0].auth_grant_id == grant.id
    assert [source.id for source in discoverable_sources([subscription_root, *synced_sources], [grant], now=NOW)] == [
        subscription_root.id,
        synced_sources[0].id,
    ]


def test_expired_oauth_source_does_not_block_public_source_registry_rows() -> None:
    expired = YouTubeGrant(
        id="yt_expired",
        user_id="usr_alice",
        workspace_id="ws_alice",
        status="expired",
        expires_at=NOW - timedelta(minutes=1),
    )
    oauth_source = subscriptions_source(workspace_id="ws_alice", auth_grant_id=expired.id)
    public_library_source = source_from_input("https://youtube.com/@leoandlongevity", import_source="manual_url")
    assert public_library_source is not None
    public_source = source_from_library_source(workspace_id="ws_alice", library_source=public_library_source)

    oauth_decision = source_discovery_decision(oauth_source, [expired], now=NOW)
    public_decision = source_discovery_decision(public_source, [expired], now=NOW)

    assert oauth_decision.discoverable is False
    assert oauth_decision.code == "source_auth_failed"
    assert public_decision.discoverable is True
    assert public_decision.code == "discoverable"
