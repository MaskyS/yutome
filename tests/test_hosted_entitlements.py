from __future__ import annotations

from typing import Any

from yutome import contract
from yutome.hosted.entitlements import PostgresUsageContextProvider
from yutome.hosted.gate import UsageGate
from yutome.hosted.mcp_query import HostedMcpAuthContext


class EntitlementConnection:
    def __init__(
        self,
        *,
        policy: bool = True,
        balance: bool = True,
        service_allocation: bool = True,
        provider_allocation: bool = True,
    ) -> None:
        self.policy = policy
        self.balance = balance
        self.service_allocation = service_allocation
        self.provider_allocation = provider_allocation
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        params = dict(params or {})
        self.calls.append((statement, params))
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
            return [
                {
                    "id": "policy_ws_prod_pro",
                    "workspace_id": params["workspace_id"],
                    "allowed_operations": ["search_store.*", "voyage.embed_query"],
                    "hard_limits_jsonb": {"search_store.lexical_query": {"candidate_limit": 100}},
                    "soft_limits_jsonb": {"voyage.embed_query": {"total_tokens": 1000}},
                }
            ]
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


def _auth(workspace_id: str = "ws_prod") -> HostedMcpAuthContext:
    return HostedMcpAuthContext(workspace_id=workspace_id, scopes=frozenset({contract.AUTH_SCOPE}))


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
