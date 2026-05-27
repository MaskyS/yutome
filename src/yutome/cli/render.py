from __future__ import annotations

import json
from typing import Any

import typer


def jsonable(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")  # type: ignore[no-any-return, attr-defined]
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def echo_json(value: object) -> None:
    typer.echo(json.dumps(jsonable(value), ensure_ascii=False, indent=2, default=str))


def render_query_result(result: object, *, json_output: bool) -> None:
    payload: Any = result.model_dump() if hasattr(result, "model_dump") else result
    if json_output:
        echo_json(payload)
        return
    if not isinstance(payload, dict):
        typer.echo(str(payload))
        return
    for note in payload.get("notes", []):
        typer.echo(f"note: {note}")
    rows = payload.get("rows", [])
    if len(rows) == 1 and isinstance(rows[0], dict) and payload.get("total") == 1:
        echo_json(rows[0])
    else:
        echo_json(rows)


def render_hosted_result(value: object, *, json_output: bool, message: str | None = None) -> None:
    if json_output:
        echo_json(value)
        return
    if message is not None:
        typer.echo(message)
        return
    typer.echo(str(value))
