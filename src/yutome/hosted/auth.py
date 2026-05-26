from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from yutome.hosted.control_plane import AccountGrant


FORBIDDEN_PROVIDER_CREDENTIAL_KEYS = frozenset(
    {
        "api_key",
        "apiKey",
        "secret",
        "client_secret",
        "clientSecret",
        "gemini_api_key",
        "google_api_key",
        "voyage_api_key",
        "webshare_username",
        "webshare_password",
        "proxy_password",
        "refresh_token",
        "access_token",
    }
)


class HostedAccessTokenProps(BaseModel):
    """OAuth token props for hosted Yutome MCP/API callers.

    Provider credentials deliberately do not appear here. Provider allocation
    lookup happens in the hosted control plane after workspace authorization.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str
    workspace_id: str
    grant_id: str
    scopes: set[str] = Field(default_factory=set)
    audience: str | None = None
    client_id: str | None = None
    install_id: str | None = None
    token_version: int = 1
    expires_at: datetime | None = None

    def allows_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def public_claims(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "grant_id": self.grant_id,
            "scopes": sorted(self.scopes),
            "audience": self.audience,
            "client_id": self.client_id,
            "install_id": self.install_id,
            "token_version": self.token_version,
            "expires_at": self.expires_at,
        }


def access_token_props_from_grant(grant: AccountGrant) -> HostedAccessTokenProps:
    return HostedAccessTokenProps(
        user_id=grant.user_id,
        workspace_id=grant.workspace_id,
        grant_id=grant.id,
        scopes=set(grant.scopes),
        audience=grant.audience,
        client_id=grant.client_id,
        install_id=grant.install_id,
        token_version=grant.token_version,
        expires_at=grant.expires_at,
    )


def token_props_have_provider_credentials(props: Mapping[str, Any]) -> bool:
    return bool(provider_credential_keys_in_mapping(props))


def provider_credential_keys_in_mapping(props: Mapping[str, Any]) -> set[str]:
    found: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                key_text = str(key)
                if key_text in FORBIDDEN_PROVIDER_CREDENTIAL_KEYS:
                    found.add(key_text)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(props)
    return found


__all__ = [
    "FORBIDDEN_PROVIDER_CREDENTIAL_KEYS",
    "HostedAccessTokenProps",
    "access_token_props_from_grant",
    "provider_credential_keys_in_mapping",
    "token_props_have_provider_credentials",
]
