from __future__ import annotations

from typing import Any

from yutome import contract
from yutome.hosted.entitlements import PostgresUsageContextProvider
from yutome.hosted.gate import UsageGate
from yutome.hosted.mcp_query import HostedMcpAuthContext


_MISSING = object()


class EntitlementConnection:
    def __init__(
        self,
        *,
        policy: bool = True,
        balance: bool = True,
        service_allocation: bool = True,
        provider_allocation: bool = True,
        subscription_status: str = "trialing",
        trial_ends_at: Any = "2999-01-01T00:00:00+00:00",
        workspace_row: bool = True,
        requests_per_minute: Any = _MISSING,
    ) -> None:
        self.policy = policy
        self.balance = balance
        self.service_allocation = service_allocation
        self.provider_allocation = provider_allocation
        self.subscription_status = subscription_status
        self.trial_ends_at = trial_ends_at
        self.workspace_row = workspace_row
        self.requests_per_minute = requests_per_minute
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params = dict(params or {})
        self.calls.append((statement, params))
        if "FROM workspaces" in statement:
            if not self.workspace_row:
                return []
            return [
                {
                    "subscription_status": self.subscription_status,
                    "trial_ends_at": self.trial_ends_at,
                }
            ]
        if "FROM service_allocations" in statement:
            if not self.service_allocation:
                return []
            return [
                {
                    "id": "svc_ws_prod_search_store",
                    "workspace_id": params["workspace_id"],
                    "service": "search_store",
                    "operation": params["operation"],
                    "credential_mode": "service_internal",
                    "status": "active",
                    "backend": "postgres_vectorchord",
                    "index_profile_ref": "sip_default",
                    "metadata_json": {},
                }
            ]
        if "FROM provider_allocations" in statement:
            if not self.provider_allocation:
                return []
            return [
                {
                    "id": "alloc_ws_prod_voyage",
                    "workspace_id": params["workspace_id"],
                    "provider": params["provider"],
                    "operation": params["operation"],
                    "credential_mode": "hosted",
                    "status": "active",
                    "model_or_plan": "voyage-4-lite",
                    "external_allocation_id": None,
                    "metadata_json": {},
                }
            ]
        if "FROM entitlement_policies" in statement:
            if not self.policy:
                return []
            row = {
                "id": "policy_ws_prod_pro",
                "workspace_id": params["workspace_id"],
                "allowed_operations": ["search_store.*", "voyage.embed_query"],
                "hard_limits_jsonb": {"search_store.lexical_query": {"candidate_limit": 100}},
                "soft_limits_jsonb": {"voyage.embed_query": {"total_tokens": 1000}},
            }
            if self.requests_per_minute is not _MISSING:
                row["requests_per_minute"] = self.requests_per_minute
            return [row]
        if "FROM workspace_balances" in statement:
            if not self.balance:
                return []
            return [
                {
                    "workspace_id": params["workspace_id"],
                    "entitlement_policy_id": params["entitlement_policy_id"],
                    "remaining_units_jsonb": {"queries": 10, "candidate_limit": 100, "total_tokens": 2000, "vectors": 10},
                    "unlimited_units": [],
                }
            ]
        return []


def _auth(workspace_id: str = "ws_prod", *, dashboard_read: bool = False) -> HostedMcpAuthContext:
    return HostedMcpAuthContext(
        workspace_id=workspace_id,
        scopes=frozenset({contract.AUTH_SCOPE}),
        dashboard_read=dashboard_read,
    )


def test_postgres_usage_context_provider_loads_search_store_entitlement_inputs() -> None:
    connection = EntitlementConnection()
    provider = PostgresUsageContextProvider(connection)

    context = provider(_auth(), "lexical_query", {"queries": 1, "candidate_limit": 5})
    reservation = UsageGate().reserve(
        workspace_id="ws_prod",
        subject="search_store",
        operation="lexical_query",
        estimated_units={"queries": 1, "candidate_limit": 5},
        allocation=context.allocation,
        policy=context.policy,
        balance=context.balance,
        idempotency_key="idem_search",
    )

    assert reservation.status == "reserved"
    assert context.allocation is not None
    assert context.allocation.id == "svc_ws_prod_search_store"
    assert context.policy.operation_allowed("search_store.lexical_query")
    assert context.balance.has_units({"queries": 1}) == (True, None)


def test_active_policy_carries_requests_per_minute_when_present() -> None:
    connection = EntitlementConnection(requests_per_minute="37")
    provider = PostgresUsageContextProvider(connection)

    policy = provider._active_policy(workspace_id="ws_prod")

    assert policy is not None
    assert policy.requests_per_minute == 37


def test_active_policy_defaults_requests_per_minute_to_none_when_absent() -> None:
    connection = EntitlementConnection()
    provider = PostgresUsageContextProvider(connection)

    policy = provider._active_policy(workspace_id="ws_prod")

    assert policy is not None
    assert policy.requests_per_minute is None


def test_postgres_usage_context_provider_loads_voyage_allocation() -> None:
    connection = EntitlementConnection()
    provider = PostgresUsageContextProvider(connection)

    context = provider.voyage(_auth(), "embed_query", {"total_tokens": 10, "vectors": 1})
    reservation = UsageGate().reserve(
        workspace_id="ws_prod",
        subject="voyage",
        operation="embed_query",
        estimated_units={"total_tokens": 10, "vectors": 1},
        allocation=context.allocation,
        policy=context.policy,
        balance=context.balance,
        idempotency_key="idem_voyage",
    )

    assert reservation.status == "reserved"
    assert context.allocation is not None
    assert context.allocation.id == "alloc_ws_prod_voyage"


def test_postgres_usage_context_provider_denies_missing_policy_closed() -> None:
    connection = EntitlementConnection(policy=False)
    provider = PostgresUsageContextProvider(connection)

    context = provider(_auth(), "lexical_query", {"queries": 1})
    reservation = UsageGate().reserve(
        workspace_id="ws_prod",
        subject="search_store",
        operation="lexical_query",
        estimated_units={"queries": 1},
        allocation=context.allocation,
        policy=context.policy,
        balance=context.balance,
        idempotency_key="idem_denied",
    )

    assert reservation.status == "denied"
    assert reservation.decision.reason == "operation_not_allowed"
    assert reservation.decision.denial_effect == "hard"
    assert context.balance.unlimited_units == set()


def test_postgres_usage_context_provider_missing_balance_is_hard_denial() -> None:
    connection = EntitlementConnection(balance=False)
    provider = PostgresUsageContextProvider(connection)

    context = provider(_auth(), "lexical_query", {"queries": 1})
    reservation = UsageGate().reserve(
        workspace_id="ws_prod",
        subject="search_store",
        operation="lexical_query",
        estimated_units={"queries": 1},
        allocation=context.allocation,
        policy=context.policy,
        balance=context.balance,
        idempotency_key="idem_soft_denied",
    )

    assert reservation.status == "denied"
    assert reservation.decision.reason == "insufficient_balance"
    assert reservation.decision.denial_effect == "hard"
    assert context.balance.unlimited_units == set()


def _reserve(provider: PostgresUsageContextProvider, subject: str, operation: str):
    method = provider.voyage if subject == "voyage" else provider
    context = (
        method(_auth(), operation, {"queries": 1})
        if subject == "search_store"
        else method(_auth(), operation, {"total_tokens": 10, "vectors": 1})
    )
    return UsageGate().reserve(
        workspace_id="ws_prod",
        subject=subject,  # type: ignore[arg-type]
        operation=operation,
        estimated_units={"queries": 1} if subject == "search_store" else {"total_tokens": 10, "vectors": 1},
        allocation=context.allocation,
        policy=context.policy,
        balance=context.balance,
        idempotency_key=f"idem_{subject}_{operation}",
    )


def test_trialing_subscription_is_entitled_like_active() -> None:
    # A still-running trial grants ingest/tool calls exactly like an active subscription.
    connection = EntitlementConnection(subscription_status="trialing")
    provider = PostgresUsageContextProvider(connection)

    reservation = _reserve(provider, "search_store", "lexical_query")

    assert reservation.status == "reserved"
    assert any("FROM workspaces" in statement for statement, _params in connection.calls)


def test_active_subscription_is_entitled_regardless_of_trial_window() -> None:
    # An active paid subscription stays entitled even after the original trial window passed.
    connection = EntitlementConnection(
        subscription_status="active", trial_ends_at="2000-01-01T00:00:00+00:00"
    )
    provider = PostgresUsageContextProvider(connection)

    reservation = _reserve(provider, "voyage", "embed_query")

    assert reservation.status == "reserved"


def test_expired_trial_without_subscription_hard_denies_ingest_tool_calls() -> None:
    # Trial ended (past trial_ends_at) and no active/trialing subscription -> read-only:
    # ingest/tool calls hard-deny, and the gate short-circuits before loading policy/balance.
    connection = EntitlementConnection(
        subscription_status="canceled", trial_ends_at="2000-01-01T00:00:00+00:00"
    )
    provider = PostgresUsageContextProvider(connection)

    reservation = _reserve(provider, "search_store", "lexical_query")

    assert reservation.status == "denied"
    # The trial gate returns no allocation + a deny policy, so the gate fails closed before any
    # operation/limit/balance check (allocation_missing is a hard deny).
    assert reservation.decision.reason == "allocation_missing"
    assert reservation.decision.denial_effect == "hard"
    # Read-only deny is decided at the trial gate; policy/balance are not even consulted.
    assert all("FROM entitlement_policies" not in statement for statement, _params in connection.calls)


def test_missing_workspace_row_fails_closed_as_expired() -> None:
    connection = EntitlementConnection(workspace_row=False)
    provider = PostgresUsageContextProvider(connection)

    reservation = _reserve(provider, "search_store", "lexical_query")

    assert reservation.status == "denied"
    assert reservation.decision.reason == "allocation_missing"


def test_expired_trial_dashboard_read_stays_allowed() -> None:
    # The dashboard BFF read path (dashboard_read=True) is exempt from the trial-expiry deny,
    # so the existing corpus remains searchable from the dashboard after a trial ends.
    connection = EntitlementConnection(
        subscription_status="canceled", trial_ends_at="2000-01-01T00:00:00+00:00"
    )
    provider = PostgresUsageContextProvider(connection)

    context = provider.for_subject(
        auth=_auth(dashboard_read=True),
        subject="search_store",
        operation="lexical_query",
        estimated_units={"queries": 1},
    )
    reservation = UsageGate().reserve(
        workspace_id="ws_prod",
        subject="search_store",
        operation="lexical_query",
        estimated_units={"queries": 1},
        allocation=context.allocation,
        policy=context.policy,
        balance=context.balance,
        idempotency_key="idem_dashboard_read",
    )

    assert reservation.status == "reserved"
    # The real policy/balance are loaded (not the deny policy) because the read is exempt.
    assert context.policy.operation_allowed("search_store.lexical_query")
