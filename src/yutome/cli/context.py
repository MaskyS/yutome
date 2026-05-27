from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer

from yutome import runtime
from yutome.config import DEFAULT_CONFIG_FILENAME
from yutome.db import bootstrap_catalog


@dataclass
class InvocationContext:
    config_path: Path = Path(DEFAULT_CONFIG_FILENAME)
    _runtime: runtime.Runtime | None = None

    def runtime(self) -> runtime.Runtime:
        if self._runtime is None:
            self._runtime = runtime.configure(self.config_path)
            bootstrap_catalog(self._runtime.paths.catalog_db)
        return self._runtime


def install_context(ctx: typer.Context, *, config_path: Path) -> None:
    ctx.obj = InvocationContext(config_path=config_path)


def get_context(ctx: typer.Context) -> InvocationContext:
    obj = ctx.obj
    if isinstance(obj, InvocationContext):
        return obj
    fallback = InvocationContext()
    ctx.obj = fallback
    return fallback


def config_path(ctx: typer.Context) -> Path:
    return get_context(ctx).config_path
