from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from yutome.hosted.billing import billing_schema_statements
from yutome.hosted.migrations import (
    POSTGRES_HOSTED_SCHEMA_SQL,
    POSTGRES_PHASE1_SCHEMA_SQL,
    POSTGRES_PHASE4_SCHEMA_SQL,
)


class SqlConnection(Protocol):
    def execute(self, statement: str) -> object:
        ...


def schema_statements(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    for raw_line in sql.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        current.append(raw_line)
        if line.endswith(";"):
            statements.append("\n".join(current).strip())
            current = []
    if current:
        statements.append("\n".join(current).strip())
    return statements


def phase1_schema_statements(sql: str = POSTGRES_PHASE1_SCHEMA_SQL) -> list[str]:
    return schema_statements(sql)


def phase4_schema_statements(sql: str = POSTGRES_PHASE4_SCHEMA_SQL) -> list[str]:
    return schema_statements(sql)


def hosted_schema_statements(sql: str = POSTGRES_HOSTED_SCHEMA_SQL) -> list[str]:
    statements = schema_statements(sql)
    if sql == POSTGRES_HOSTED_SCHEMA_SQL:
        statements.extend(billing_schema_statements())
    return statements


def apply_phase1_schema(connection: SqlConnection, *, statements: Iterable[str] | None = None) -> int:
    return apply_schema(connection, statements=statements or phase1_schema_statements())


def apply_phase4_schema(connection: SqlConnection, *, statements: Iterable[str] | None = None) -> int:
    return apply_schema(connection, statements=statements or phase4_schema_statements())


def apply_hosted_schema(connection: SqlConnection, *, statements: Iterable[str] | None = None) -> int:
    return apply_schema(connection, statements=statements or hosted_schema_statements())


def apply_schema(connection: SqlConnection, *, statements: Iterable[str]) -> int:
    applied = 0
    for statement in statements:
        connection.execute(statement)
        applied += 1
    return applied


__all__ = [
    "SqlConnection",
    "apply_hosted_schema",
    "apply_phase1_schema",
    "apply_phase4_schema",
    "apply_schema",
    "hosted_schema_statements",
    "phase1_schema_statements",
    "phase4_schema_statements",
    "schema_statements",
]
