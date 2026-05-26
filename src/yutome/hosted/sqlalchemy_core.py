from __future__ import annotations

from typing import Any

from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import ClauseElement


_POSTGRES_DIALECT = postgresql.dialect(paramstyle="pyformat")


def compile_postgres_statement(statement: ClauseElement) -> tuple[str, dict[str, Any]]:
    """Compile SQLAlchemy Core into the psycopg named-param shape Yutome uses."""

    compiled = statement.compile(
        dialect=_POSTGRES_DIALECT,
        compile_kwargs={"render_postcompile": True},
    )
    return str(compiled).strip(), dict(compiled.params)


__all__ = ["compile_postgres_statement"]
