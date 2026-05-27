from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient

from yutome.hosted.account import DEFAULT_ACCOUNT_SESSION_AUDIENCE, sign_account_session_token
from yutome.hosted.account_cli import code_challenge_for_verifier, new_code_verifier
from yutome.hosted.http_api import ACCOUNT_SESSION_TOKEN_HEADER, build_app, error_body
from yutome.hosted.mcp_query import HostedMcpQueryAdapter


MCP_TOKEN = "mcp-test-token"
DASHBOARD_TOKEN = "dashboard-test-token"
HMAC_SECRET = "account-session-secret"


class _NoopSearchStore:
    pass


class StatefulAccountConnection:
    def __init__(self) -> None:
        self.workspace = {"id": "ws_cli", "name": "CLI Workspace", "status": "active"}
        self.grants: dict[str, dict[str, Any]] = {}
        self.sources: dict[str, dict[str, Any]] = {}
        self.policies: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params = dict(params or {})
        self.calls.append((statement, params))
        if "FROM workspaces" in statement:
            return [self.workspace] if params.get("workspace_id") == self.workspace["id"] else []
        if statement.startswith("INSERT INTO account_grants"):
            row = {
                "id": params["id"],
                "user_id": params["user_id"],
                "workspace_id": params["workspace_id"],
                "kind": "cli_install",
                "scopes": params["scopes"],
                "status": "pending",
                "audience": params["audience"],
                "client_id": params["client_id"],
                "install_id": params["install_id"],
                "token_version": 1,
                "metadata_json": json.loads(params["metadata_json"]),
                "created_at": datetime.now(timezone.utc),
                "last_used_at": None,
                "expires_at": params["expires_at"],
                "revoked_at": None,
            }
            self.grants[row["id"]] = row
            return [dict(row)]
        if "FROM account_grants" in statement and "install_id" in statement:
            return [dict(row) for row in self.grants.values() if row["install_id"] == params["code_hash"]]
        if statement.startswith("UPDATE account_grants") and "status = 'active'" in statement:
            row = self.grants.get(params["grant_id"])
            if row is None or row["status"] != "pending" or row["install_id"] != params["code_hash"]:
                return []
            row.update(
                {
                    "status": "active",
                    "install_id": params["install_id"],
                    "token_version": 1,
                    "expires_at": params["token_expires_at"],
                    "last_used_at": datetime.now(timezone.utc),
                }
            )
            row["metadata_json"].update(json.loads(params["metadata_json"]))
            return [dict(row)]
        if "FROM account_grants" in statement and "id = %(grant_id)s" in statement:
            row = self.grants.get(params["grant_id"])
            return [dict(row)] if row else []
        if statement.startswith("UPDATE account_grants") and "last_used_at" in statement:
            row = self.grants.get(params["grant_id"])
            if row:
                row["last_used_at"] = datetime.now(timezone.utc)
            return []
        if statement.startswith("INSERT INTO sources"):
            row = {
                "id": params["id"],
                "workspace_id": params["workspace_id"],
                "source_type": params["source_type"],
                "source_url": params["source_url"],
                "canonical_channel_id": params["canonical_channel_id"],
                "canonical_playlist_id": params["canonical_playlist_id"],
                "canonical_video_id": params["canonical_video_id"],
                "display_name": params["display_name"],
                "selected": params["selected"],
                "auto_index_allowed": params["auto_index_allowed"],
                "import_source": params["import_source"],
                "auth_grant_id": params["auth_grant_id"],
                "metadata_json": json.loads(params["metadata_json"]),
                "status": params["status"],
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
            self.sources[row["id"]] = row
            return [dict(row)]
        if statement.startswith("INSERT INTO source_refresh_policies"):
            row = {
                "id": params["id"],
                "workspace_id": params["workspace_id"],
                "source_id": params["source_id"],
                "enabled": params["enabled"],
                "cadence_seconds": params["cadence_seconds"],
                "next_run_at": params["next_run_at"],
            }
            self.policies[row["id"]] = row
            return [dict(row)]
        if statement.startswith("INSERT INTO jobs"):
            row = {
                "id": params["id"],
                "workspace_id": params["workspace_id"],
                "source_id": params["source_id"],
                "job_type": "index_video",
                "status": "queued",
                "priority": params["priority"],
                "created_at": params["created_at"],
                "started_at": None,
                "finished_at": None,
                "cancelled_at": None,
                "error_code": None,
                "error_message": None,
                "metadata_json": json.loads(params["metadata_json"]),
            }
            self.jobs[row["id"]] = row
            return [dict(row)]
        if "FROM jobs" in statement:
            return sorted((dict(row) for row in self.jobs.values()), key=lambda row: row["created_at"], reverse=True)
        return []


def build_client(connection: StatefulAccountConnection) -> TestClient:
    return TestClient(
        build_app(
            adapter=HostedMcpQueryAdapter(search_store=_NoopSearchStore()),
            billing_connection=connection,
            expected_api_token=MCP_TOKEN,
            expected_account_api_token=DASHBOARD_TOKEN,
            account_session_secret=HMAC_SECRET,
            account_session_audience=DEFAULT_ACCOUNT_SESSION_AUDIENCE,
        )
    )


def mint_session() -> str:
    now = datetime.now(timezone.utc)
    return sign_account_session_token(
        user_id="usr_cli",
        workspace_id="ws_cli",
        secret=HMAC_SECRET,
        expires_at=now + timedelta(hours=1),
        issued_at=now,
    )


def account_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {DASHBOARD_TOKEN}", ACCOUNT_SESSION_TOKEN_HEADER: mint_session()}


def authorize_and_exchange(client: TestClient, *, verifier: str | None = None) -> dict[str, Any]:
    verifier = verifier or new_code_verifier()
    redirect_uri = "http://127.0.0.1:49152/callback"
    authorize = client.post(
        "/account/cli/authorize",
        json={
            "code_challenge": code_challenge_for_verifier(verifier),
            "code_challenge_method": "S256",
            "redirect_uri": redirect_uri,
            "state": "state_1",
        },
        headers=account_headers(),
    )
    assert authorize.status_code == 200, authorize.text
    token = client.post(
        "/account/cli/token",
        json={"code": authorize.json()["code"], "code_verifier": verifier, "redirect_uri": redirect_uri},
    )
    assert token.status_code == 200, token.text
    return token.json()


def test_cli_authorize_token_import_and_jobs_derive_workspace() -> None:
    connection = StatefulAccountConnection()
    client = build_client(connection)
    token = authorize_and_exchange(client)

    response = client.post(
        "/account/sources/import",
        json={
            "sources": [
                {
                    "source_url": "https://www.youtube.com/watch?v=OEDoJyhQhXs",
                    "workspace_id": "ws_evil",
                    "display_name": "Manual video",
                },
                {
                    "source_url": "https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw",
                    "channel_id": "UC_x5XG1OV2P6uZZ5FSM9Ttw",
                    "display_name": "Small channel",
                    "import_source": "youtube_oauth",
                },
            ]
        },
        headers={"Authorization": f"Bearer {token['access_token']}"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workspace_id"] == "ws_cli"
    assert len(body["imported"]) == 2
    assert body["jobs"][0]["job_type"] == "index_video"
    assert len(body["refresh_policies"]) == 1
    assert {row["workspace_id"] for row in connection.sources.values()} == {"ws_cli"}
    assert {row["workspace_id"] for row in connection.jobs.values()} == {"ws_cli"}

    jobs = client.get("/account/jobs", headers={"Authorization": f"Bearer {token['access_token']}"})
    assert jobs.status_code == 200, jobs.text
    assert jobs.json()["jobs"][0]["job_type"] == "index_video"


def test_cli_token_exchange_rejects_replay_wrong_verifier_and_expiry() -> None:
    connection = StatefulAccountConnection()
    client = build_client(connection)
    verifier = new_code_verifier()
    redirect_uri = "http://127.0.0.1:49153/callback"
    authorize = client.post(
        "/account/cli/authorize",
        json={
            "code_challenge": code_challenge_for_verifier(verifier),
            "code_challenge_method": "S256",
            "redirect_uri": redirect_uri,
        },
        headers=account_headers(),
    )
    code = authorize.json()["code"]

    wrong = client.post(
        "/account/cli/token",
        json={"code": code, "code_verifier": new_code_verifier(), "redirect_uri": redirect_uri},
    )
    assert wrong.status_code == 401
    assert error_body(wrong.json())["code"] == "cli_pkce_verifier_invalid"

    first = client.post(
        "/account/cli/token",
        json={"code": code, "code_verifier": verifier, "redirect_uri": redirect_uri},
    )
    assert first.status_code == 200, first.text
    replay = client.post(
        "/account/cli/token",
        json={"code": code, "code_verifier": verifier, "redirect_uri": redirect_uri},
    )
    assert replay.status_code == 401
    assert error_body(replay.json())["code"] == "cli_authorization_code_invalid"

    expired_verifier = new_code_verifier()
    expired = client.post(
        "/account/cli/authorize",
        json={
            "code_challenge": code_challenge_for_verifier(expired_verifier),
            "code_challenge_method": "S256",
            "redirect_uri": redirect_uri,
        },
        headers=account_headers(),
    )
    expired_code = expired.json()["code"]
    for grant in connection.grants.values():
        if grant["status"] == "pending":
            grant["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    expired_exchange = client.post(
        "/account/cli/token",
        json={"code": expired_code, "code_verifier": expired_verifier, "redirect_uri": redirect_uri},
    )
    assert expired_exchange.status_code == 401
    assert error_body(expired_exchange.json())["code"] == "cli_authorization_code_expired"


def test_cli_authorize_requires_account_session() -> None:
    client = build_client(StatefulAccountConnection())

    response = client.post(
        "/account/cli/authorize",
        json={
            "code_challenge": code_challenge_for_verifier(new_code_verifier()),
            "code_challenge_method": "S256",
            "redirect_uri": "http://127.0.0.1:49154/callback",
        },
        headers={"Authorization": f"Bearer {DASHBOARD_TOKEN}"},
    )

    assert response.status_code == 401
    assert error_body(response.json())["code"] == "account_session_required"


def test_source_import_rejects_credential_like_fields() -> None:
    connection = StatefulAccountConnection()
    client = build_client(connection)
    token = authorize_and_exchange(client)

    response = client.post(
        "/account/sources/import",
        json={
            "sources": [
                {
                    "source_url": "https://www.youtube.com/watch?v=OEDoJyhQhXs",
                    "metadata": {"access_token": "secret"},
                }
            ]
        },
        headers={"Authorization": f"Bearer {token['access_token']}"},
    )

    assert response.status_code == 400
    assert error_body(response.json())["code"] == "source_import_credentials_rejected"

