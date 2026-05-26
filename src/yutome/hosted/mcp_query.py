from __future__ import annotations

import json
import math
import re
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import ValidationError

from yutome import contract
from yutome.hosted.allocation_policy import (
    default_search_store_allocation,
    estimate_search_store_query,
    estimate_voyage_embeddings,
)
from yutome.hosted.errors import classify_provider_http_error
from yutome.hosted.events import denied_usage_event, usage_event_from_normalization
from yutome.hosted.gate import Allocation, UsageGate
from yutome.hosted.ids import idempotency_key, input_hash
from yutome.hosted.migrations import HOSTED_DEFAULT_EMBEDDING_DIMENSION, HOSTED_DEFAULT_EMBEDDING_MODEL
from yutome.hosted.models import EntitlementPolicy, ProviderAllocation, UsageEvent, UsageReservation, WorkspaceBalance
from yutome.hosted.normalizers import normalize_search_store_usage
from yutome.hosted.provider_wrappers import ProviderCallContext, UsageReservationDenied
from yutome.hosted.resources import HostedResourceNotFound
from yutome.hosted.search_store import SearchStore
from yutome.query import QueryResult


FORBIDDEN_TOOL_ARGUMENT_KEYS = frozenset(
    {
        "workspace",
        "workspace_id",
        "tenant",
        "tenant_id",
        "organization",
        "organization_id",
        "org_id",
        "owner_user_id",
        "user_id",
    }
)
SUPPORTED_TOOLS: frozenset[str] = frozenset({"find", "show"})
SUPPORTED_TOOL = "find"
SUPPORTED_RESOURCE_HOSTS: frozenset[str] = frozenset({"channel", "chunk", "transcript", "video"})


class UsageGateLike(Protocol):
    def reserve(
        self,
        *,
        workspace_id: str,
        subject: str,
        operation: str,
        estimated_units: dict[str, float],
        allocation: Allocation | None,
        policy: EntitlementPolicy,
        balance: WorkspaceBalance,
        idempotency_key: str,
    ) -> UsageReservation:
        ...


class UsageLedgerWriter(Protocol):
    def append(self, event: UsageEvent) -> Any:
        ...


@dataclass(frozen=True)
class HostedMcpAuthContext:
    workspace_id: str
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({contract.AUTH_SCOPE}))
    user_id: str | None = None
    grant_id: str | None = None
    client_id: str | None = None
    session_id: str | None = None

    def validated(self) -> HostedMcpAuthContext:
        workspace_id = self.workspace_id.strip()
        if not workspace_id:
            raise HostedMcpError(
                code="workspace_required",
                message="Hosted MCP requests require an authenticated workspace.",
                status_code=401,
            )
        if not _valid_identity(workspace_id):
            raise HostedMcpError(
                code="workspace_invalid",
                message="Authenticated workspace identity is invalid.",
                status_code=401,
            )
        if contract.AUTH_SCOPE not in self.scopes:
            raise HostedMcpError(
                code="insufficient_scope",
                message=f"Hosted MCP requests require the {contract.AUTH_SCOPE!r} scope.",
                status_code=403,
            )
        if workspace_id == self.workspace_id:
            return self
        return HostedMcpAuthContext(
            workspace_id=workspace_id,
            scopes=self.scopes,
            user_id=self.user_id,
            grant_id=self.grant_id,
            client_id=self.client_id,
            session_id=self.session_id,
        )


@dataclass(frozen=True)
class HostedMcpUsageContext:
    allocation: Allocation | None
    policy: EntitlementPolicy
    balance: WorkspaceBalance


UsageContextProvider = Callable[[HostedMcpAuthContext, str, Mapping[str, float]], HostedMcpUsageContext]
QueryEmbeddingCallable = Callable[[str, ProviderCallContext], list[float]]


class HostedMcpError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int = 400,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.data = dict(data or {})
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data:
            error["data"] = self.data
        return {"ok": False, "error": error}


class HostedMcpQueryAdapter:
    def __init__(
        self,
        *,
        search_store: SearchStore,
        gate: UsageGateLike | None = None,
        ledger: UsageLedgerWriter | None = None,
        usage_context_provider: UsageContextProvider | None = None,
        voyage_usage_context_provider: UsageContextProvider | None = None,
        query_embedder: QueryEmbeddingCallable | None = None,
        embedding_model: str = HOSTED_DEFAULT_EMBEDDING_MODEL,
        embedding_dimension: int = HOSTED_DEFAULT_EMBEDDING_DIMENSION,
    ) -> None:
        self.search_store = search_store
        self.gate = gate or UsageGate()
        self.ledger = ledger or NoopUsageLedger()
        self.usage_context_provider = usage_context_provider or default_usage_context
        self.voyage_usage_context_provider = voyage_usage_context_provider or default_voyage_usage_context
        self.query_embedder = query_embedder or self._embed_query_with_voyage
        self.embedding_model = embedding_model
        self.embedding_dimension = embedding_dimension

    def call_tool(
        self,
        *,
        auth: HostedMcpAuthContext,
        name: str,
        arguments: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        auth = auth.validated()
        normalized_name = str(name or "")
        if normalized_name not in SUPPORTED_TOOLS:
            _raise_unsupported_tool(normalized_name)
        normalized_arguments = _normalize_tool_arguments(arguments or {})
        if normalized_name == "show":
            return self._show(auth=auth, arguments=normalized_arguments)
        return self._find(auth=auth, arguments=normalized_arguments).model_dump()

    def read_resource(self, *, auth: HostedMcpAuthContext, uri: str) -> dict[str, Any]:
        auth = auth.validated()
        host, params = parse_yutome_resource_uri(uri)
        if host not in SUPPORTED_RESOURCE_HOSTS:
            _raise_unsupported_resource(host, uri)
        try:
            return self._read_resource(auth=auth, host=host, params=params)
        except HostedResourceNotFound as exc:
            raise _resource_not_found(kind=exc.kind, id_=exc.id) from exc

    def _find(self, *, auth: HostedMcpAuthContext, arguments: dict[str, Any]) -> QueryResult:
        _reject_workspace_argument_injection(arguments)
        request = HostedFindRequest.from_arguments(arguments)
        if request.mode == "lexical":
            return self._find_lexical(auth=auth, request=request)
        return self._find_vector(auth=auth, request=request)

    def _find_lexical(self, *, auth: HostedMcpAuthContext, request: HostedFindRequest) -> QueryResult:
        estimate = estimate_search_store_query(
            operation="lexical_query",
            candidate_limit=request.limit,
        )
        reservation = self._reserve_search_store(auth=auth, request=request, estimate=estimate)
        rows, usage = self.search_store.lexical_search(
            workspace_id=auth.workspace_id,
            query=request.text,
            limit=request.limit,
        )
        self._record_search_store_success(auth=auth, reservation=reservation, rows=rows, usage=usage)
        return QueryResult(
            rows=[_contract_find_row(row, project=request.project) for row in rows],
            notes=request.notes,
            total=None,
        )

    def _find_vector(self, *, auth: HostedMcpAuthContext, request: HostedFindRequest) -> QueryResult:
        search_operation = "semantic_query" if request.mode == "semantic" else "hybrid_query"
        search_estimate = estimate_search_store_query(
            operation=search_operation,
            candidate_limit=request.limit,
            query_vector_dimensions=self.embedding_dimension,
        )
        search_reservation = self._reserve_search_store(auth=auth, request=request, estimate=search_estimate)
        vector_context = self._voyage_query_context(auth=auth, request=request)
        try:
            query_vector = self.query_embedder(request.text, vector_context)
        except UsageReservationDenied as exc:
            raise HostedMcpError(
                code="usage_denied",
                message=exc.reservation.decision.message or exc.reservation.decision.reason,
                status_code=403,
                data={
                    "reason": exc.reservation.decision.reason,
                    "operation": exc.reservation.operation_key,
                    "reservation_id": exc.reservation.id,
                },
            ) from exc
        except Exception as exc:
            failure = classify_provider_http_error(provider="voyage", status_code=_status_code_from_exception(exc), message=str(exc))
            raise HostedMcpError(
                code="provider_call_failed",
                message="Hosted MCP semantic query embedding failed.",
                status_code=502,
                data={
                    "provider": failure.provider,
                    "operation": "voyage.embed_query",
                    "failure_kind": failure.kind,
                    "retryable": failure.retryable,
                    "error_code": failure.code,
                },
            ) from exc

        if request.mode == "semantic":
            rows, usage = self.search_store.semantic_search(
                workspace_id=auth.workspace_id,
                query_vector=query_vector,
                limit=request.limit,
            )
        else:
            rows, usage = self.search_store.hybrid_search(
                workspace_id=auth.workspace_id,
                query=request.text,
                query_vector=query_vector,
                limit=request.limit,
            )
        self._record_search_store_success(auth=auth, reservation=search_reservation, rows=rows, usage=usage)
        return QueryResult(
            rows=[_contract_find_row(row, project=request.project) for row in rows],
            notes=request.notes,
            total=None,
        )

    def _reserve_search_store(
        self,
        *,
        auth: HostedMcpAuthContext,
        request: HostedFindRequest,
        estimate: Any,
    ) -> UsageReservation:
        usage_context = self.usage_context_provider(
            auth,
            estimate.operation,
            estimate.estimated_units,
        )
        reservation = self.gate.reserve(
            workspace_id=auth.workspace_id,
            subject=estimate.subject,
            operation=estimate.operation,
            estimated_units=estimate.estimated_units,
            allocation=usage_context.allocation,
            policy=usage_context.policy,
            balance=usage_context.balance,
            idempotency_key=_query_idempotency_key(auth=auth, request=request),
        )
        if not reservation.decision.allowed:
            event = denied_usage_event(reservation)
            self.ledger.append(_with_mcp_metadata(event, auth=auth, reservation=reservation))
            raise HostedMcpError(
                code="usage_denied",
                message=reservation.decision.message or reservation.decision.reason,
                status_code=403,
                data={
                    "reason": reservation.decision.reason,
                    "operation": reservation.operation_key,
                    "reservation_id": reservation.id,
                },
            )
        return reservation

    def _record_search_store_success(
        self,
        *,
        auth: HostedMcpAuthContext,
        reservation: UsageReservation,
        rows: list[dict[str, Any]],
        usage: Any,
    ) -> None:
        actual_units = {
            **usage.units,
            "result_count": usage.units.get("result_count", len(rows)),
        }
        normalization = normalize_search_store_usage(
            operation=usage.operation,
            backend=usage.backend,
            index_profile_ref=usage.index_profile_ref,
            units=actual_units,
            metadata=usage.metadata,
        )
        event = usage_event_from_normalization(
            normalization,
            reservation=reservation,
            event_type="service_operation_succeeded",
        )
        self.ledger.append(_with_mcp_metadata(event, auth=auth, reservation=reservation))

    def _voyage_query_context(self, *, auth: HostedMcpAuthContext, request: HostedFindRequest) -> ProviderCallContext:
        estimate = estimate_voyage_embeddings(
            operation="embed_query",
            total_tokens_estimate=_estimate_query_tokens(request.text),
            vectors=1,
        )
        usage_context = self.voyage_usage_context_provider(auth, estimate.operation, estimate.estimated_units)
        return ProviderCallContext(
            gate=self.gate,  # type: ignore[arg-type]
            ledger=self.ledger,
            workspace_id=auth.workspace_id,
            subject=estimate.subject,
            operation=estimate.operation,
            estimated_units=estimate.estimated_units,
            allocation=usage_context.allocation,
            policy=usage_context.policy,
            balance=usage_context.balance,
            idempotency_key=_provider_idempotency_key(auth=auth, request=request, operation=estimate.operation_key),
            metadata={
                "mcp_client_id": auth.client_id,
                "mcp_grant_id": auth.grant_id,
                "mcp_session_id": auth.session_id,
                "mcp_tool": SUPPORTED_TOOL,
                "mcp_search_mode": request.mode,
                "estimate_method": estimate.method,
            },
        )

    def _embed_query_with_voyage(self, query: str, context: ProviderCallContext) -> list[float]:
        from yutome.embeddings import _embed_voyage_query

        return _embed_voyage_query(
            query=query,
            model=self.embedding_model,
            dimension=self.embedding_dimension,
            hosted_context=context,
        )

    def _show(self, *, auth: HostedMcpAuthContext, arguments: dict[str, Any]) -> dict[str, Any]:
        request = HostedShowRequest.from_arguments(arguments)
        try:
            if request.kind == "chunk":
                return self.search_store.resource_chunk(workspace_id=auth.workspace_id, chunk_id=request.id_)
            if request.kind == "video":
                return self.search_store.resource_video(workspace_id=auth.workspace_id, video_id=request.id_)
            if request.kind == "channel":
                return self.search_store.resource_channel(workspace_id=auth.workspace_id, channel_id=request.id_)
            if request.kind == "transcript":
                return self.search_store.resource_transcript(
                    workspace_id=auth.workspace_id,
                    transcript_version_id=request.id_,
                    offset=request.transcript_offset,
                    limit=request.transcript_limit,
                )
            if request.kind == "source":
                return self.search_store.resource_source(workspace_id=auth.workspace_id, source_id=request.id_)
        except HostedResourceNotFound as exc:
            raise _resource_not_found(kind=exc.kind, id_=exc.id) from exc
        raise HostedMcpError(
            code="unsupported_show_kind",
            message="Hosted MCP show currently supports chunk, video, channel, transcript, and source.",
            status_code=501,
            data={"kind": request.kind, "supported": sorted(HostedShowRequest.SUPPORTED_KINDS)},
        )

    def _read_resource(
        self,
        *,
        auth: HostedMcpAuthContext,
        host: str,
        params: Mapping[str, str],
    ) -> dict[str, Any]:
        if host == "chunk":
            return self.search_store.resource_chunk(workspace_id=auth.workspace_id, chunk_id=params["chunk_id"])
        if host == "video":
            return self.search_store.resource_video(workspace_id=auth.workspace_id, video_id=params["video_id"])
        if host == "channel":
            return self.search_store.resource_channel(workspace_id=auth.workspace_id, channel_id=params["channel_id"])
        if host == "transcript":
            return self.search_store.resource_transcript(
                workspace_id=auth.workspace_id,
                transcript_version_id=params["transcript_version_id"],
            )
        _raise_unsupported_resource(host, f"yutome://{host}")


@dataclass(frozen=True)
class HostedFindRequest:
    text: str
    limit: int
    mode: str = "lexical"
    project: str | None = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, Any]) -> HostedFindRequest:
        text = str(arguments.get("text") or "").strip()
        if not text:
            raise HostedMcpError(
                code="invalid_arguments",
                message="find requires a non-empty text argument.",
                status_code=400,
            )

        mode = arguments.get("mode")
        notes: list[str] = []
        if mode in {None, "", "lexical"}:
            normalized_mode = "lexical"
            if mode in {None, ""}:
                notes.append("hosted_find_defaulted_to_lexical")
        elif mode in {"semantic", "hybrid"}:
            normalized_mode = str(mode)
        else:
            raise HostedMcpError(
                code="unsupported_search_mode",
                message="Hosted MCP find supports lexical, semantic, and hybrid modes.",
                status_code=400,
                data={"mode": mode},
            )

        search_in = arguments.get("in_", arguments.get("in", "chunks"))
        if search_in != "chunks":
            raise HostedMcpError(
                code="unsupported_search_target",
                message="Hosted MCP currently supports find over chunks only.",
                status_code=400,
                data={"in": search_in},
            )

        unsupported_filters = [
            key
            for key in ("channel", "since", "until", "source", "language", "group_by")
            if arguments.get(key) is not None
        ]
        if unsupported_filters:
            raise HostedMcpError(
                code="unsupported_find_filter",
                message="Hosted MCP lexical find filters are not implemented yet.",
                status_code=400,
                data={"filters": unsupported_filters},
            )

        offset = _coerce_int(arguments.get("offset", 0), name="offset")
        if offset != 0:
            raise HostedMcpError(
                code="unsupported_find_offset",
                message="Hosted MCP lexical find does not support offset yet.",
                status_code=400,
                data={"offset": offset},
            )

        project = arguments.get("project")
        if project not in {None, "thin", "chunk", "metadata"}:
            raise HostedMcpError(
                code="unsupported_find_project",
                message="Hosted MCP lexical find supports thin, chunk, and metadata projects only.",
                status_code=400,
                data={"project": project},
            )

        return cls(
            text=text,
            limit=_coerce_limit(arguments.get("limit", 10)),
            mode=normalized_mode,
            project=None if project == "thin" else project,
            notes=notes,
        )


@dataclass(frozen=True)
class HostedShowRequest:
    SUPPORTED_KINDS = frozenset({"channel", "chunk", "context", "source", "transcript", "video"})

    kind: str
    id_: str
    transcript_offset: int = 0
    transcript_limit: int | None = None

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, Any]) -> HostedShowRequest:
        _reject_workspace_argument_injection(arguments)
        kind = str(arguments.get("kind") or "").strip()
        if kind not in cls.SUPPORTED_KINDS:
            raise HostedMcpError(
                code="unsupported_show_kind",
                message="Hosted MCP show currently supports chunk, video, channel, transcript, source, and context.",
                status_code=400,
                data={"kind": kind, "supported": sorted(cls.SUPPORTED_KINDS)},
            )
        if kind == "context":
            raise HostedMcpError(
                code="unsupported_show_context",
                message="Hosted MCP show context expansion is not implemented in this Python adapter slice.",
                status_code=501,
                data={"kind": kind},
            )
        raw_id = arguments.get("id_", arguments.get("id"))
        id_ = str(raw_id or "").strip()
        if not id_:
            raise HostedMcpError(
                code="invalid_arguments",
                message=f"show(kind={kind!r}) requires id_.",
                status_code=400,
                data={"kind": kind},
            )
        transcript_limit = arguments.get("transcript_limit")
        return cls(
            kind=kind,
            id_=id_,
            transcript_offset=max(0, _coerce_int(arguments.get("transcript_offset", 0), name="transcript_offset")),
            transcript_limit=None
            if transcript_limit is None
            else max(1, min(_coerce_int(transcript_limit, name="transcript_limit"), 5000)),
        )


class NoopUsageLedger:
    def append(self, event: UsageEvent) -> None:
        return None


def default_usage_context(
    auth: HostedMcpAuthContext,
    operation: str,
    estimated_units: Mapping[str, float],
) -> HostedMcpUsageContext:
    return HostedMcpUsageContext(
        allocation=default_search_store_allocation(
            workspace_id=auth.workspace_id,
            operation=operation,
        ),
        policy=EntitlementPolicy(
            id=f"policy_{auth.workspace_id}_hosted_mcp",
            workspace_id=auth.workspace_id,
            allowed_operations={f"search_store.{operation}"},
        ),
        balance=WorkspaceBalance(
            workspace_id=auth.workspace_id,
            unlimited_units=set(estimated_units),
        ),
    )


def default_voyage_usage_context(
    auth: HostedMcpAuthContext,
    operation: str,
    estimated_units: Mapping[str, float],
) -> HostedMcpUsageContext:
    return HostedMcpUsageContext(
        allocation=ProviderAllocation(
            id=f"alloc_{auth.workspace_id}_voyage",
            workspace_id=auth.workspace_id,
            provider="voyage",
            operation=operation,
            model_or_plan=HOSTED_DEFAULT_EMBEDDING_MODEL,
        ),
        policy=EntitlementPolicy(
            id=f"policy_{auth.workspace_id}_hosted_mcp_voyage",
            workspace_id=auth.workspace_id,
            allowed_operations={f"voyage.{operation}"},
        ),
        balance=WorkspaceBalance(
            workspace_id=auth.workspace_id,
            unlimited_units=set(estimated_units),
        ),
    )


def parse_yutome_resource_uri(uri: str) -> tuple[str, dict[str, str]]:
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme != "yutome":
        raise HostedMcpError(
            code="invalid_resource_uri",
            message="Hosted MCP resources must use the yutome:// scheme.",
            status_code=400,
            data={"uri": uri},
        )
    host = parsed.hostname or ""
    spec = contract.resource_by_host(host)
    if spec is None:
        raise HostedMcpError(
            code="unsupported_resource",
            message=f"Unsupported hosted MCP resource host: {host!r}.",
            status_code=404,
            data={"uri": uri},
        )
    placeholder_match = re.search(r"\{([^}]+)\}", spec.uri_template)
    if placeholder_match is None:
        return host, {}
    raw_path = parsed.path.lstrip("/")
    if not raw_path:
        placeholder = placeholder_match.group(1)
        raise HostedMcpError(
            code="invalid_resource_uri",
            message=f"Resource URI is missing the {placeholder} segment.",
            status_code=400,
            data={"uri": uri},
        )
    return host, {placeholder_match.group(1): urllib.parse.unquote(raw_path)}


def _normalize_tool_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(arguments)
    if "in" in normalized and "in_" not in normalized:
        normalized["in_"] = normalized.pop("in")
    return normalized


def _reject_workspace_argument_injection(arguments: Mapping[str, Any]) -> None:
    forbidden = sorted(FORBIDDEN_TOOL_ARGUMENT_KEYS.intersection(arguments))
    if forbidden:
        raise HostedMcpError(
            code="workspace_argument_not_allowed",
            message="Hosted MCP workspace identity comes from auth context, not tool arguments.",
            status_code=400,
            data={"arguments": forbidden},
        )


def _raise_unsupported_tool(name: str) -> None:
    if contract.tool_by_name(name) is None:
        message = f"Unsupported hosted MCP tool: {name!r}."
    else:
        message = f"Hosted MCP tool {name!r} is not implemented in this Python adapter slice."
    raise HostedMcpError(
        code="unsupported_tool",
        message=message,
        status_code=404,
        data={"tool": name, "supported": sorted(SUPPORTED_TOOLS)},
    )


def _raise_unsupported_resource(host: str, uri: str) -> None:
    if host in SUPPORTED_RESOURCE_HOSTS:
        raise AssertionError("resource host marked supported but no handler is registered")
    raise HostedMcpError(
        code="unsupported_resource",
        message=f"Hosted MCP resource {host!r} is not implemented in this Python adapter slice.",
        status_code=501,
        data={"uri": uri, "supported": sorted(SUPPORTED_RESOURCE_HOSTS)},
    )


def _resource_not_found(*, kind: str, id_: str) -> HostedMcpError:
    return HostedMcpError(
        code="resource_not_found",
        message=f"Hosted MCP {kind} resource was not found.",
        status_code=404,
        data={"kind": kind, "id": id_},
    )


def _query_idempotency_key(*, auth: HostedMcpAuthContext, request: HostedFindRequest) -> str:
    operation = f"search_store.{request.mode}_query"
    extras = [value for value in (auth.grant_id, auth.client_id, auth.session_id) if value]
    return idempotency_key(
        workspace_id=auth.workspace_id,
        subject_id=auth.client_id or "hosted_mcp",
        operation=operation,
        input_hash_value=input_hash(
            {
                "tool": SUPPORTED_TOOL,
                "mode": request.mode,
                "text": request.text,
                "limit": request.limit,
                "project": request.project,
            }
        ),
        extras=extras,
    )


def _provider_idempotency_key(*, auth: HostedMcpAuthContext, request: HostedFindRequest, operation: str) -> str:
    extras = [value for value in (auth.grant_id, auth.client_id, auth.session_id) if value]
    return idempotency_key(
        workspace_id=auth.workspace_id,
        subject_id=auth.client_id or "hosted_mcp",
        operation=operation,
        input_hash_value=input_hash(
            {
                "tool": SUPPORTED_TOOL,
                "mode": request.mode,
                "text": request.text,
                "limit": request.limit,
                "project": request.project,
            }
        ),
        extras=extras,
    )


def _with_mcp_metadata(
    event: UsageEvent,
    *,
    auth: HostedMcpAuthContext,
    reservation: UsageReservation,
) -> UsageEvent:
    event.metadata = {
        **event.metadata,
        "idempotency_key": reservation.idempotency_key,
        "allocation_id": reservation.allocation_id,
        "mcp_client_id": auth.client_id,
        "mcp_grant_id": auth.grant_id,
        "mcp_session_id": auth.session_id,
    }
    return event


def _contract_find_row(row: Mapping[str, Any], *, project: str | None) -> dict[str, Any]:
    chunk_id = _required_str(row, "chunk_id")
    video_id = _required_str(row, "video_id")
    start_ms = _time_ms(row, "start")
    end_ms = _time_ms(row, "end")
    hit: dict[str, Any] = {
        "chunk_id": chunk_id,
        "resource_uri": f"yutome://chunk/{chunk_id}",
        "video_id": video_id,
        "youtube_url": _youtube_url(video_id, start_ms),
        "start_ms": start_ms,
        "end_ms": end_ms,
        "snippet": _snippet(str(row.get("snippet") or row.get("text") or "")),
        "transcript_version_id": row.get("transcript_version_id"),
        "match_type": row.get("match_type") or "lexical",
        "scores": _scores(row),
    }
    for key in (
        "title",
        "transcript_source",
        "language",
        "token_count",
        "channel_id",
        "channel_handle",
        "channel_title",
        "published_at",
        "duration_seconds",
        "thumbnail_url",
        "live_status",
        "ingest_status",
    ):
        if row.get(key) is not None:
            hit[key] = row.get(key)
    if row.get("is_generated") is not None:
        hit["is_generated"] = bool(row.get("is_generated"))
    if row.get("score") is not None:
        hit["score"] = _json_scalar(row.get("score"))
    if project in {"chunk", "metadata"}:
        hit["text"] = row.get("text", "")
    if project == "metadata":
        for key in ("sequence", "chunker_version", "text_hash", "metadata_hash", "description"):
            if row.get(key) is not None:
                hit[key] = row.get(key)
    return {key: value for key, value in hit.items() if value is not None and value != {}}


def _scores(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("lexical_score", "vector_score", "hybrid_score", "vector_distance")
    return {key: _json_scalar(row.get(key)) for key in keys if row.get(key) is not None}


def _required_str(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if value is None or str(value) == "":
        raise HostedMcpError(
            code="invalid_search_row",
            message=f"SearchStore lexical row is missing {key}.",
            status_code=500,
        )
    return str(value)


def _time_ms(row: Mapping[str, Any], prefix: str) -> int:
    ms_key = f"{prefix}_ms"
    seconds_key = f"{prefix}_seconds"
    if row.get(ms_key) is not None:
        return _coerce_int(row.get(ms_key), name=ms_key)
    seconds = row.get(seconds_key)
    if seconds is None:
        return 0
    try:
        return int(round(float(seconds) * 1000))
    except (TypeError, ValueError) as exc:
        raise HostedMcpError(
            code="invalid_search_row",
            message=f"SearchStore lexical row has invalid {seconds_key}.",
            status_code=500,
        ) from exc


def _coerce_limit(value: Any) -> int:
    return max(1, min(_coerce_int(value, name="limit"), 200))


def _coerce_int(value: Any, *, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HostedMcpError(
            code="invalid_arguments",
            message=f"{name} must be an integer.",
            status_code=400,
        ) from exc


def _estimate_query_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 5))


def _status_code_from_exception(exc: BaseException) -> int | None:
    for name in ("status_code", "status", "code"):
        value = getattr(exc, name, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        for name in ("status_code", "status"):
            value = getattr(response, name, None)
            if isinstance(value, int):
                return value
    return None


def _snippet(text: str, *, max_chars: int = 240) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def _youtube_url(video_id: str, start_ms: int) -> str:
    return f"https://youtube.com/watch?v={video_id}&t={int(start_ms // 1000)}s"


def _valid_identity(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value))


def _json_scalar(value: Any) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


def validation_error_to_mcp_error(exc: ValidationError) -> HostedMcpError:
    return HostedMcpError(
        code="invalid_arguments",
        message="Request body failed validation.",
        status_code=400,
        data={"errors": exc.errors()},
    )
