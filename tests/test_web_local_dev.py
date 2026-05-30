from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import psycopg
import pytest

from yutome.hosted.schema import hosted_metadata


ROOT = Path(__file__).parents[1]


class _RecordingConnection:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def __enter__(self) -> _RecordingConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, statement: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.statements.append(statement)
        return []


def test_dev_hosted_api_bootstraps_current_full_hosted_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    statements: list[str] = []

    def connect(*_args: object, **_kwargs: object) -> _RecordingConnection:
        return _RecordingConnection(statements)

    monkeypatch.setattr(psycopg, "connect", connect)
    module_name = "_yutome_dev_hosted_api_bootstrap_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "web" / "scripts" / "dev_hosted_api.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)

    sql = "\n".join(statements)
    created_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS ([a-z_]+)", sql))

    assert set(hosted_metadata.tables) <= created_tables
    assert "email_login_tokens" in created_tables
    assert "api_keys" in created_tables
    assert {"search_index_profiles", "chunks", "chunk_embeddings"} <= created_tables
    assert "CREATE EXTENSION IF NOT EXISTS vchord;" in sql


def test_local_dev_script_enables_local_only_magic_link_flow() -> None:
    script = (ROOT / "web" / "scripts" / "local-dev.sh").read_text()

    assert 'export YUTOME_APP_URL="http://127.0.0.1:5273"' in script
    assert "export YUTOME_AUTH_DEV_RETURN_LINK=1" in script
    assert "tensorchord/vchord-suite:pg17-latest" in script
