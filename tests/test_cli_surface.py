from __future__ import annotations

import json
from typing import Any

import pytest
from typer.testing import CliRunner

from yutome.cli import app
from yutome.cli import search as search_cli


def _help(args: list[str]) -> str:
    result = CliRunner().invoke(app, [*args, "--help"])
    assert result.exit_code == 0, result.output
    return result.output


def test_command_tree_matches_composable_surface() -> None:
    root = _help([])
    for command in ("setup", "connect", "disconnect", "status", "search", "corpus", "serve", "hosted", "doctor", "export"):
        assert command in root

    search = _help(["search"])
    for command in ("find", "list", "show", "q"):
        assert command in search

    corpus = _help(["corpus"])
    for command in ("add", "import", "import-youtube", "select", "sync", "rebuild", "quality"):
        assert command in corpus

    serve = _help(["serve"])
    for command in ("mcp", "http", "bridge", "remote"):
        assert command in serve

    remote = _help(["serve", "remote"])
    for command in ("prepare", "sync", "http", "mcp"):
        assert command in remote
    assert "check" not in remote

    hosted = _help(["hosted"])
    for command in ("api", "migrate", "login", "jobs", "usage", "source", "run"):
        assert command in hosted
    for removed in ("db-check", "search-smoke", "billing-status", "worker", "maintenance-tick"):
        assert removed not in hosted

    doctor = _help(["doctor"])
    for command in ("local", "proxy", "gemini", "eval", "contract", "remote", "hosted-db"):
        assert command in doctor

    export = _help(["export"])
    assert "markdown" in export
    assert "obsidian" in export
    assert "portable-md" not in export


@pytest.mark.parametrize(
    "args",
    [
        ["find", "topic"],
        ["q", "{}"],
        ["sync"],
        ["add", "@channel"],
        ["import", "sources.csv"],
        ["import-youtube"],
        ["select", "@channel"],
        ["unselect", "@channel"],
        ["rebuild-vectors"],
        ["rebuild-chunks"],
        ["proxy-info"],
        ["proxy-test"],
        ["gemini-test"],
        ["eval", "run", "evals/smoke.json"],
        ["mcp", "serve"],
        ["http", "serve"],
        ["remote", "prepare"],
        ["bridge", "start"],
        ["quality", "upgrade"],
        ["contract", "emit"],
        ["usage"],
    ],
)
def test_removed_top_level_paths_fail(args: list[str]) -> None:
    result = CliRunner().invoke(app, args)
    assert result.exit_code != 0
    assert "No such command" in result.output


def test_search_presets_route_to_transport_neutral_api(monkeypatch) -> None:  # noqa: ANN001
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeRuntime:
        config = None
        paths = None

    class FakeContext:
        def runtime(self) -> FakeRuntime:
            return FakeRuntime()

    def fake_find(**kwargs: Any) -> dict[str, Any]:
        calls.append(("find", kwargs))
        return {"rows": []}

    def fake_list(**kwargs: Any) -> dict[str, Any]:
        calls.append(("list", kwargs))
        return {"rows": []}

    def fake_show(**kwargs: Any) -> dict[str, Any]:
        calls.append(("show", kwargs))
        return {"ok": True}

    def fake_q(request: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(("q", {"request": request, **kwargs}))
        return {"rows": []}

    monkeypatch.setattr(search_cli, "api_find", fake_find)
    monkeypatch.setattr(search_cli, "api_list", fake_list)
    monkeypatch.setattr(search_cli, "api_show", fake_show)
    monkeypatch.setattr(search_cli, "api_q", fake_q)
    monkeypatch.setattr(search_cli, "get_context", lambda _ctx: FakeContext())

    runner = CliRunner()
    assert runner.invoke(app, ["search", "find", "Crohn", "--mode", "lexical", "--limit", "3", "--json"]).exit_code == 0
    assert runner.invoke(app, ["search", "list", "videos", "--limit", "5", "--json"]).exit_code == 0
    assert runner.invoke(app, ["search", "show", "context", "chunk-1", "--token-budget", "1000"]).exit_code == 0
    assert (
        runner.invoke(
            app,
            ["search", "q", json.dumps({"entity": "video", "limit": 1})],
        ).exit_code
        == 0
    )

    assert calls[0][0] == "find"
    assert calls[0][1]["text"] == "Crohn"
    assert calls[0][1]["mode"] == "lexical"
    assert calls[0][1]["limit"] == 3
    assert calls[1][0] == "list"
    assert calls[1][1]["config"] is None
    assert calls[1][1]["paths"] is None
    assert calls[1][1]["entity"] == "videos"
    assert calls[1][1]["limit"] == 5
    assert calls[1][1]["offset"] == 0
    assert calls[2][0] == "show"
    assert calls[2][1]["kind"] == "context"
    assert calls[2][1]["id_"] == "chunk-1"
    assert calls[2][1]["token_budget"] == 1000
    assert calls[3][0] == "q"
    assert calls[3][1]["request"].entity == "video"
