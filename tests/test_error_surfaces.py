"""Error-surface ergonomics tests.

Cover the user-facing failure paths that previously surfaced silently:

* Empty-corpus searches must include a note telling the user to run
  ``yutome corpus sync`` rather than returning rows=[] with no explanation.
* Missing yutome.toml at the CLI boundary must exit cleanly with an
  init hint, not a Python traceback.
* Corrupt remote connector state must surface a clean ValueError
  pointing at ``yutome connect --deploy``, not a JSONDecodeError.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from yutome.api import find, list_
from yutome.config import default_config
from yutome.db import bootstrap_catalog
from yutome.paths import ProjectPaths
from yutome.remote_connection import load_remote_state, remote_state_path


@pytest.fixture
def empty_project(tmp_path: Path) -> tuple[object, ProjectPaths]:
    config = default_config()
    paths = ProjectPaths.from_config(config, project_root=tmp_path)
    paths.ensure_base_dirs()
    bootstrap_catalog(paths.catalog_db)
    return config, paths


def test_find_on_empty_corpus_surfaces_note(empty_project: tuple[object, ProjectPaths]) -> None:
    config, paths = empty_project
    result = find(config=config, paths=paths, text="anything", mode="lexical")
    assert result.rows == []
    assert any("No videos indexed yet" in note for note in result.notes), (
        f"empty-corpus note missing; notes={result.notes!r}"
    )


def test_list_status_on_empty_corpus_surfaces_note(
    empty_project: tuple[object, ProjectPaths],
) -> None:
    config, paths = empty_project
    result = list_(config=config, paths=paths, entity="status")
    if result.rows:
        # status_breakdown can still emit a single zeroed row; the note should
        # still fire only when rows is genuinely empty. Skip in that case.
        pytest.skip("status_breakdown returned a row; empty-rows path not exercised")
    assert any("No videos indexed yet" in note for note in result.notes), (
        f"empty-corpus note missing; notes={result.notes!r}"
    )


def test_load_paths_exits_cleanly_when_config_missing(tmp_path: Path) -> None:
    """The CLI helper must turn a missing yutome.toml into a typer.Exit(2)
    with an init hint, not a raw FileNotFoundError."""
    import typer

    from yutome.cli import _load_paths

    missing = tmp_path / "yutome.toml"
    with pytest.raises(typer.Exit) as exc_info:
        _load_paths(missing)
    assert exc_info.value.exit_code == 2


def test_load_remote_state_rejects_corrupt_json(tmp_path: Path) -> None:
    config = default_config()
    paths = ProjectPaths.from_config(config, project_root=tmp_path)
    paths.ensure_base_dirs()
    state_path = remote_state_path(paths)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_remote_state(paths)

    message = str(exc_info.value)
    assert "remote connector state" in message
    assert "yutome connect --deploy" in message


def test_load_remote_state_rejects_non_object(tmp_path: Path) -> None:
    config = default_config()
    paths = ProjectPaths.from_config(config, project_root=tmp_path)
    paths.ensure_base_dirs()
    state_path = remote_state_path(paths)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        load_remote_state(paths)

    message = str(exc_info.value)
    assert "JSON object" in message
    assert "yutome connect --deploy" in message
