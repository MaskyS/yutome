from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from yutome import api
from yutome.config import AppConfig, HostedConfig
from yutome.hosted.search_store import SearchFilters, SearchStoreUsage
from yutome.paths import ProjectPaths


class FakeSearchStore:
    instances: list[FakeSearchStore] = []

    def __init__(self, connection: object) -> None:
        self.connection = connection
        self.calls: list[dict[str, Any]] = []
        self.instances.append(self)

    def lexical_search(
        self,
        *,
        workspace_id: str,
        query: str,
        limit: int,
        offset: int = 0,
        filters: SearchFilters | None = None,
    ) -> tuple[list[dict[str, Any]], SearchStoreUsage]:
        self.calls.append(
            {
                "mode": "lexical",
                "workspace_id": workspace_id,
                "query": query,
                "limit": limit,
                "offset": offset,
                "filters": filters,
            }
        )
        return [], _usage(operation="lexical_query", backend="vectorchord_bm25", limit=limit)

    def semantic_search(
        self,
        *,
        workspace_id: str,
        query_vector: list[float],
        limit: int,
        offset: int = 0,
        filters: SearchFilters | None = None,
    ) -> tuple[list[dict[str, Any]], SearchStoreUsage]:
        self.calls.append(
            {
                "mode": "semantic",
                "workspace_id": workspace_id,
                "query_vector": query_vector,
                "limit": limit,
                "offset": offset,
                "filters": filters,
            }
        )
        return [], _usage(operation="semantic_query", backend="postgres_vectorchord", limit=limit)

    def hybrid_search(
        self,
        *,
        workspace_id: str,
        query: str,
        query_vector: list[float],
        limit: int,
        offset: int = 0,
        filters: SearchFilters | None = None,
    ) -> tuple[list[dict[str, Any]], SearchStoreUsage]:
        self.calls.append(
            {
                "mode": "hybrid",
                "workspace_id": workspace_id,
                "query": query,
                "query_vector": query_vector,
                "limit": limit,
                "offset": offset,
                "filters": filters,
            }
        )
        return [], _usage(operation="hybrid_query", backend="vectorchord_bm25_pgvector", limit=limit)


def test_local_default_hybrid_falls_back_to_lexical_with_loud_notice(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _install_fake_store(monkeypatch)

    def raises(**_kwargs: object) -> list[float]:
        raise RuntimeError("missing voyage key")

    monkeypatch.setattr(api, "_embed_voyage_query", raises)

    with caplog.at_level(logging.WARNING, logger="yutome.api"):
        result = api.find(
            config=_config(),
            paths=_paths(tmp_path),
            text="Crohn probiotics",
            mode=None,
        )

    store = FakeSearchStore.instances[-1]
    assert [call["mode"] for call in store.calls] == ["lexical"]
    assert result.notes[0].startswith("embeddings_unavailable_lexical_only")
    assert "lexical-only" in result.notes[0]
    assert "Voyage embedding unavailable for hybrid query" in caplog.text


def test_local_explicit_semantic_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_store(monkeypatch)

    def raises(**_kwargs: object) -> list[float]:
        raise RuntimeError("missing voyage key")

    monkeypatch.setattr(api, "_embed_voyage_query", raises)

    with pytest.raises(RuntimeError, match="missing voyage key"):
        api.find(
            config=_config(),
            paths=_paths(tmp_path),
            text="Crohn probiotics",
            mode="semantic",
        )

    assert FakeSearchStore.instances[-1].calls == []


def test_local_explicit_hybrid_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_store(monkeypatch)

    def raises(**_kwargs: object) -> list[float]:
        raise RuntimeError("missing voyage key")

    monkeypatch.setattr(api, "_embed_voyage_query", raises)

    with pytest.raises(RuntimeError, match="missing voyage key"):
        api.find(
            config=_config(),
            paths=_paths(tmp_path),
            text="Crohn probiotics",
            mode="hybrid",
        )

    assert FakeSearchStore.instances[-1].calls == []


def test_local_default_hybrid_success_has_no_degraded_notice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_fake_store(monkeypatch)
    monkeypatch.setattr(api, "_embed_voyage_query", lambda **_kwargs: [0.1, 0.2])

    result = api.find(
        config=_config(),
        paths=_paths(tmp_path),
        text="Crohn probiotics",
        mode=None,
    )

    store = FakeSearchStore.instances[-1]
    assert [call["mode"] for call in store.calls] == ["hybrid"]
    assert not any(note.startswith("embeddings_unavailable_lexical_only") for note in result.notes)


def _install_fake_store(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSearchStore.instances = []
    monkeypatch.setattr(api, "_connect", lambda _config: object())
    monkeypatch.setattr(api, "PostgresVectorChordSearchStore", FakeSearchStore)


def _config() -> AppConfig:
    return AppConfig(hosted=HostedConfig(local_workspace_id="ws_local"))


def _paths(tmp_path: Path) -> ProjectPaths:
    return ProjectPaths.from_config(_config(), project_root=tmp_path)


def _usage(*, operation: str, backend: str, limit: int) -> SearchStoreUsage:
    return SearchStoreUsage(
        operation=operation,
        backend=backend,
        index_profile_ref="sip_default",
        units={"queries": 1, "candidate_limit": limit, "result_count": 0},
        metadata={"storage_backend": "postgres_vectorchord"},
    )
