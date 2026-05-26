from __future__ import annotations

import re

from alembic.config import Config
from alembic.script import ScriptDirectory

from yutome.hosted.postgres import hosted_schema_statements
from yutome.hosted.schema import hosted_metadata


def test_hosted_alembic_head_is_fresh_baseline() -> None:
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)

    assert script.get_current_head() == "20260526_0001"


def test_hosted_core_metadata_tracks_hosted_schema_tables() -> None:
    ddl = "\n".join(hosted_schema_statements())
    ddl_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS ([a-z_]+)", ddl))

    assert set(hosted_metadata.tables) == ddl_tables
    assert "bm25_document" in hosted_metadata.tables["chunks"].c
    assert "embedding" in hosted_metadata.tables["chunk_embeddings"].c
