"""Shared runtime singleton: cached config + paths reused by every adapter.

Each adapter (local stdio MCP, local HTTP, bridge dispatcher) calls
``configure(config_path)`` once at startup. Handler functions in
``yutome.contract`` then read the active runtime via ``current()`` without
re-parsing TOML on every call.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from yutome.config import DEFAULT_CONFIG_FILENAME, AppConfig, load_config
from yutome.env import apply_env_to_config, load_dotenv
from yutome.paths import ProjectPaths


@dataclass(frozen=True)
class Runtime:
    config_path: Path
    config: AppConfig
    paths: ProjectPaths


_CURRENT: Runtime | None = None


def configure(config_path: Path) -> Runtime:
    """Build a runtime from the given config path and install it as current."""
    global _CURRENT
    project_root = (
        config_path.parent if config_path.is_absolute() else (Path.cwd() / config_path).parent
    )
    load_dotenv(project_root / ".env")
    config = apply_env_to_config(load_config(config_path))
    paths = ProjectPaths.from_config(config, project_root=project_root)
    _CURRENT = Runtime(config_path=config_path, config=config, paths=paths)
    return _CURRENT


def set_current(runtime: Runtime) -> None:
    """Install a pre-built Runtime (used by callers that already loaded config)."""
    global _CURRENT
    _CURRENT = runtime


def current() -> Runtime:
    """Return the active runtime, auto-configuring from the default path if unset."""
    if _CURRENT is not None:
        return _CURRENT
    env_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_root:
        candidate = Path(env_root) / DEFAULT_CONFIG_FILENAME
        if candidate.exists():
            return configure(candidate)
    return configure(Path(DEFAULT_CONFIG_FILENAME))
