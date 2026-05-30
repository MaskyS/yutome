from __future__ import annotations

from typing import Any

import pytest

from yutome.hosted.account_cleanup import (
    AccountCleanupError,
    WORKSPACE_CHILD_TABLES_DELETE_ORDER,
    delete_synthetic_workspace,
    synthetic_workspace_guard,
    workspace_cleanup_statements,
)


def _delete_targets(statements: list[Any]) -> list[str]:
    return [statement.sql.split("DELETE FROM ", 1)[1].split()[0] for statement in statements]


def test_workspace_cleanup_statements_children_before_parent() -> None:
    statements = workspace_cleanup_statements("ws_abc")
    targets = _delete_targets(statements)

    assert targets[-1] == "workspaces"
    assert [table.name for table in WORKSPACE_CHILD_TABLES_DELETE_ORDER] == targets[:-1]
    assert targets.index("chunk_embeddings") < targets.index("chunks")
    assert targets.index("chunks") < targets.index("videos")
    assert targets.index("chunks") < targets.index("transcript_versions")
    assert targets.index("videos") < targets.index("sources")
    assert targets.index("usage_events") < targets.index("usage_reservations")
    assert targets.index("sources") < targets.index("youtube_grants")
    assert all(statement.params == {"workspace_id": "ws_abc"} for statement in statements)
    assert all("%(workspace_id)s" in statement.sql for statement in statements)


def test_workspace_cleanup_excludes_shared_catalog_tables() -> None:
    targets = set(_delete_targets(workspace_cleanup_statements("ws_abc")))

    assert "users" not in targets
    assert "price_books" not in targets
    assert "api_keys" in targets


def test_guard_refuses_active_subscription() -> None:
    with pytest.raises(AccountCleanupError) as exc:
        synthetic_workspace_guard({"id": "ws_x", "status": "active", "subscription_status": "active"})

    assert exc.value.status_code == 409
    assert exc.value.code == "workspace_not_synthetic"

    synthetic_workspace_guard(
        {"id": "ws_x", "status": "active", "subscription_status": "active"},
        allow_paid=True,
    )


def test_guard_allows_trialing_workspace() -> None:
    synthetic_workspace_guard({"id": "ws_x", "status": "active", "subscription_status": "trialing"})


def test_guard_refuses_non_ws_prefixed_id() -> None:
    with pytest.raises(AccountCleanupError) as exc:
        synthetic_workspace_guard({"id": "acct_123", "status": "active", "subscription_status": "trialing"})

    assert exc.value.code == "workspace_id_invalid"


class _Transaction:
    def __init__(self, connection: "_CleanupConnection") -> None:
        self.connection = connection

    def __enter__(self) -> None:
        self.connection.events.append("begin")

    def __exit__(self, *_exc: object) -> None:
        self.connection.events.append("commit")


class _CleanupConnection:
    def __init__(
        self,
        *,
        workspace_row: dict[str, Any] | None = None,
        stripe_customer_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.workspace_row = workspace_row or {
            "id": "ws_synth123",
            "name": "Synthetic",
            "status": "active",
            "subscription_status": "trialing",
            "owner_user_id": "user_synth123",
        }
        self.stripe_customer_rows = stripe_customer_rows or []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.events: list[str] = []

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        captured = dict(params or {})
        self.calls.append((statement, captured))
        if "FROM workspaces" in statement:
            if captured.get("workspace_id") == self.workspace_row["id"]:
                return [dict(self.workspace_row)]
            return []
        if "FROM stripe_customers" in statement:
            return [dict(row) for row in self.stripe_customer_rows]
        return []


def test_delete_synthetic_workspace_runs_all_statements_in_order() -> None:
    connection = _CleanupConnection()

    result = delete_synthetic_workspace(connection, workspace_id="ws_synth123")

    delete_calls = [(sql, params) for sql, params in connection.calls if sql.startswith("DELETE FROM")]
    targets = [sql.split("DELETE FROM ", 1)[1].split()[0] for sql, _params in delete_calls]
    assert result.workspace_id == "ws_synth123"
    assert result.deleted is True
    assert targets == [table.name for table in WORKSPACE_CHILD_TABLES_DELETE_ORDER] + ["workspaces"]
    assert targets[-1] == "workspaces"
    assert all(params == {"workspace_id": "ws_synth123"} for _sql, params in delete_calls)
    assert connection.events == ["begin", "commit"]


def test_delete_synthetic_workspace_refuses_stripe_subscription_history() -> None:
    connection = _CleanupConnection(
        stripe_customer_rows=[
            {
                "workspace_id": "ws_synth123",
                "stripe_subscription_id": "sub_123",
                "subscription_status": "canceled",
            }
        ]
    )

    with pytest.raises(AccountCleanupError) as exc:
        delete_synthetic_workspace(connection, workspace_id="ws_synth123")

    assert exc.value.code == "workspace_not_synthetic"
    assert exc.value.status_code == 409
    assert not any(sql.startswith("DELETE FROM") for sql, _params in connection.calls)
