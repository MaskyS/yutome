"""Error-surface ergonomics tests.

Cover the user-facing failure paths that previously surfaced silently:

* Corrupt remote connector state must surface a clean ValueError
  pointing at ``yutome connect --deploy``, not a JSONDecodeError.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from yutome.config import default_config
from yutome.paths import ProjectPaths
from yutome.remote_connection import load_remote_state, remote_state_path


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
