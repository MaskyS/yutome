from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from yutome.hosted.account import (
    STARTER_ALLOWED_OPERATIONS,
    AccountBootstrapInput,
    STARTER_PROVIDER_OPERATIONS,
    STARTER_SERVICE_OPERATIONS,
    account_bootstrap_sql,
    bootstrap_hosted_account,
    deterministic_user_id,
    normalize_email,
    session_token_hash,
    sql_params_contain_provider_credentials,
    starter_provider_allocation_id,
    starter_service_allocation_id,
    upsert_user_sql,
)
from yutome.hosted.postgres import phase1_schema_statements


class RecordingAccountConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> Sequence[Mapping[str, Any]]:
        exact_params = dict(params or {})
        self.calls.append((statement, exact_params))
        if "INSERT INTO users" in statement:
            return [{"id": exact_params["id"], "normalized_email": exact_params["normalized_email"]}]
        if "INSERT INTO workspaces" in statement:
            return [{"id": exact_params["id"], "owner_user_id": exact_params["owner_user_id"]}]
        if "INSERT INTO entitlement_policies" in statement:
            return [{"id": exact_params["id"], "workspace_id": exact_params["workspace_id"]}]
        if "INSERT INTO account_sessions" in statement:
            return [{"id": exact_params["id"], "session_hash": exact_params["session_hash"]}]
        return [exact_params]


def test_normalized_email_drives_deterministic_user_lookup() -> None:
    first = AccountBootstrapInput(email="  Alice.Example@YUTOME.COM  ", name="Alice")
    second = AccountBootstrapInput(email="alice.example@yutome.com")

    assert normalize_email("  Alice.Example@YUTOME.COM  ") == "alice.example@yutome.com"
    assert first.normalized_email == second.normalized_email
    assert first.user_id == second.user_id == deterministic_user_id("alice.example@yutome.com")

    statement = upsert_user_sql(first)

    assert "ON CONFLICT (normalized_email) DO UPDATE" in statement.sql
    assert statement.params["normalized_email"] == "alice.example@yutome.com"
    assert statement.params["id"] == second.user_id


def test_phase1_schema_includes_workspace_members_and_account_sessions() -> None:
    joined = "\n".join(phase1_schema_statements())

    assert "normalized_email text" in joined
    assert "idx_users_normalized_email" in joined
    assert "CREATE TABLE IF NOT EXISTS workspace_members" in joined
    assert "PRIMARY KEY (workspace_id, user_id)" in joined
    assert "CREATE TABLE IF NOT EXISTS account_sessions" in joined
    assert "idx_account_sessions_session_hash" in joined


def test_account_bootstrap_sql_creates_owner_membership_starter_entitlements_and_allocations() -> None:
    account = AccountBootstrapInput(email="alice@yutome.com", session_token="session-token", session_scopes=("mcp",))

    keyed_statements = account_bootstrap_sql(account)
    keys = [key for key, _statement in keyed_statements]
    statements = {key: statement for key, statement in keyed_statements}

    assert keys[:6] == [
        "user",
        "workspace",
        "workspace_member",
        "starter_price_book",
        "entitlement_policy",
        "workspace_balance",
    ]
    assert "account_session" in statements
    assert "ON CONFLICT (workspace_id, user_id) DO UPDATE" in statements["workspace_member"].sql
    assert "role = 'owner'" in statements["workspace_member"].sql

    policy_params = statements["entitlement_policy"].params
    balance_params = statements["workspace_balance"].params
    assert policy_params["allowed_operations"] == list(STARTER_ALLOWED_OPERATIONS)
    assert "search_store.lexical_query" in policy_params["allowed_operations"]
    assert "search_store.resource_read" in policy_params["allowed_operations"]
    assert "voyage.embed_query" in policy_params["allowed_operations"]
    assert json.loads(balance_params["remaining_units_jsonb"])["total_tokens"] > 0
    assert json.loads(balance_params["remaining_units_jsonb"])["queries"] > 0
    assert json.loads(balance_params["remaining_units_jsonb"])["resource_reads"] > 0
    assert "ON CONFLICT (workspace_id) DO UPDATE" in statements["workspace_balance"].sql

    provider_ids = {
        statements[f"provider_allocation_{provider}_{operation}"].params["id"]
        for provider, operation in STARTER_PROVIDER_OPERATIONS
    }
    assert provider_ids == {
        starter_provider_allocation_id(account.workspace_id, provider, operation)
        for provider, operation in STARTER_PROVIDER_OPERATIONS
    }
    for service, operation in STARTER_SERVICE_OPERATIONS:
        statement = statements[f"service_allocation_{service}_{operation}"]
        assert statement.params["id"] == starter_service_allocation_id(account.workspace_id, service, operation)
        assert statement.params["index_profile_ref"] is None


def test_bootstrap_helper_is_idempotent_and_persists_hashed_session_only() -> None:
    account = AccountBootstrapInput(
        email="ALICE@YUTOME.COM",
        session_token="raw-session-token",
        session_scopes=("mcp", "account"),
        session_audience="hosted_mcp",
        session_client_id="client_123",
    )
    connection = RecordingAccountConnection()

    first = bootstrap_hosted_account(connection, account)
    second = bootstrap_hosted_account(connection, account)

    assert first == second
    assert first.principal.normalized_email == "alice@yutome.com"
    assert first.session is not None
    assert first.session.session_hash == session_token_hash("raw-session-token")
    assert first.session.session_hash != "raw-session-token"
    assert len(connection.calls) == len(account_bootstrap_sql(account)) * 2

    user_calls = [params for sql, params in connection.calls if "INSERT INTO users" in sql]
    assert {params["id"] for params in user_calls} == {account.user_id}
    assert {params["normalized_email"] for params in user_calls} == {"alice@yutome.com"}


def test_account_bootstrap_sql_does_not_persist_provider_credentials() -> None:
    account = AccountBootstrapInput(email="alice@yutome.com", session_token="raw-session-token")

    for _key, statement in account_bootstrap_sql(account):
        assert sql_params_contain_provider_credentials(statement) is False
        assert "raw-session-token" not in json.dumps(statement.params, default=str)
