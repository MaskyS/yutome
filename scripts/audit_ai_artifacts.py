#!/usr/bin/env python3
"""Audit likely AI-generated maintainability artifacts.

This is a non-public repo maintenance tool. It intentionally reports signals,
not verdicts: oversized files, long blocks, broad exceptions, type erasure,
compatibility/shim terminology, duplicate route declarations, duplicated
contract definitions, and generated artifacts that should not become a source
of truth.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


SCAN_ROOTS = (
    "src",
    "tests",
    "scripts",
    "web/app",
    "web/workers",
    "cloudflare/yutome-capsule/src",
    "cloudflare/yutome-capsule/test",
)

EXCLUDED_PARTS = {
    ".git",
    ".beads",
    ".dolt",
    ".mypy_cache",
    ".pytest_cache",
    ".react-router",
    ".ruff_cache",
    ".venv",
    ".wrangler",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}

SOURCE_SUFFIXES = {".py", ".ts", ".tsx"}

GENERATED_PATHS = (
    "web/build",
    "web/.react-router",
    "web/worker-configuration.d.ts",
    "cloudflare/yutome-capsule/node_modules",
    "cloudflare/yutome-capsule/.wrangler",
    "cloudflare/yutome-capsule/dist",
)

MARKERS = {
    "any_type": re.compile(r"\bAny\b|:\s*any\b|<any\b"),
    "type_ignore": re.compile(r"type:\s*ignore|@ts-ignore|@ts-expect-error"),
    "noqa": re.compile(r"#\s*noqa\b"),
    "legacy": re.compile(r"\b_?legacy\b", re.IGNORECASE),
    "compat": re.compile(r"\bcompat(?:ibility|ible)?\b|back-compat|backwards?-compat", re.IGNORECASE),
    "shim": re.compile(r"\bshim\b", re.IGNORECASE),
    "capsule": re.compile(r"capsule|yutome-capsule", re.IGNORECASE),
    "temporary": re.compile(r"\btemporary\b|\bfor now\b|\bTODO\b|\bFIXME\b|\bHACK\b", re.IGNORECASE),
}

PY_ROUTE_RE = re.compile(r"@(?P<object>app|router)\.(?P<method>get|post|put|patch|delete)\(\s*[\"'](?P<path>[^\"']+)")
TS_ROUTE_RE = re.compile(r"\b(?P<object>app|router)\.(?P<method>get|post|put|patch|delete)\(\s*[\"'](?P<path>[^\"']+)")
CONTRACT_RE = re.compile(r"\b(?P<symbol>AUTH_SCOPE|TOOLS|RESOURCES|SUPPORTED_TOOLS|SUPPORTED_RESOURCE_HOSTS)\b\s*[:=]")


@dataclass(frozen=True)
class Thresholds:
    module_lines: int = 600
    function_lines: int = 80
    class_lines: int = 120
    branch_count: int = 20


@dataclass(frozen=True)
class BlockMetric:
    path: str
    name: str
    kind: str
    start_line: int
    end_line: int
    lines: int
    branches: int


def relpath(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def has_excluded_part(path: Path) -> bool:
    return any(part in EXCLUDED_PARTS for part in path.parts)


def git_ls_files(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [root / line for line in result.stdout.splitlines() if line.strip()]


def discover_source_files(root: Path) -> list[Path]:
    tracked = git_ls_files(root)
    if tracked:
        candidates = tracked
    else:
        candidates = []
        for scan_root in SCAN_ROOTS:
            base = root / scan_root
            if base.exists():
                candidates.extend(path for path in base.rglob("*") if path.is_file())

    files = [
        path
        for path in candidates
        if path.suffix in SOURCE_SUFFIXES and path.exists() and not has_excluded_part(path.relative_to(root))
    ]
    return sorted(files, key=lambda path: relpath(path, root))


def branch_node_types() -> tuple[type[ast.AST], ...]:
    types: list[type[ast.AST]] = [ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.BoolOp]
    for name in ("Match", "match_case"):
        node_type = getattr(ast, name, None)
        if isinstance(node_type, type):
            types.append(node_type)
    return tuple(types)


def branch_count(node: ast.AST) -> int:
    return sum(isinstance(child, branch_node_types()) for child in ast.walk(node))


def is_broad_exception(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    if isinstance(handler.type, ast.Name):
        return handler.type.id in {"Exception", "BaseException"}
    if isinstance(handler.type, ast.Tuple):
        return any(isinstance(elt, ast.Name) and elt.id in {"Exception", "BaseException"} for elt in handler.type.elts)
    return False


def python_blocks(path: Path, root: Path, thresholds: Thresholds) -> tuple[list[BlockMetric], list[BlockMetric], list[dict[str, object]]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [], [], [{"path": relpath(path, root), "line": exc.lineno or 0, "kind": "syntax_error"}]

    functions: list[BlockMetric] = []
    classes: list[BlockMetric] = []
    broad_exceptions: list[dict[str, object]] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and getattr(node, "end_lineno", None):
            lines = int(node.end_lineno or node.lineno) - node.lineno + 1
            branches = branch_count(node)
            if lines >= thresholds.function_lines or branches >= thresholds.branch_count:
                functions.append(
                    BlockMetric(
                        path=relpath(path, root),
                        name=node.name,
                        kind="function",
                        start_line=node.lineno,
                        end_line=int(node.end_lineno or node.lineno),
                        lines=lines,
                        branches=branches,
                    )
                )
        elif isinstance(node, ast.ClassDef) and getattr(node, "end_lineno", None):
            lines = int(node.end_lineno or node.lineno) - node.lineno + 1
            branches = branch_count(node)
            if lines >= thresholds.class_lines or branches >= thresholds.branch_count:
                classes.append(
                    BlockMetric(
                        path=relpath(path, root),
                        name=node.name,
                        kind="class",
                        start_line=node.lineno,
                        end_line=int(node.end_lineno or node.lineno),
                        lines=lines,
                        branches=branches,
                    )
                )
        elif isinstance(node, ast.ExceptHandler) and is_broad_exception(node):
            broad_exceptions.append({"path": relpath(path, root), "line": node.lineno, "kind": "broad_exception"})

    return functions, classes, broad_exceptions


def find_matching_brace(text: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def ts_blocks(path: Path, root: Path, thresholds: Thresholds) -> tuple[list[BlockMetric], list[BlockMetric]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    block_res = (
        re.compile(r"\b(?P<kind>class)\s+(?P<name>[A-Za-z_$][\w$]*)[^{]*\{"),
        re.compile(r"\b(?P<kind>function)\s+(?P<name>[A-Za-z_$][\w$]*)[^{]*\{"),
        re.compile(r"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{"),
    )
    functions: list[BlockMetric] = []
    classes: list[BlockMetric] = []
    line_starts = [0]
    for match in re.finditer(r"\n", text):
        line_starts.append(match.end())

    def line_number(offset: int) -> int:
        return sum(start <= offset for start in line_starts)

    for pattern in block_res:
        for match in pattern.finditer(text):
            end = find_matching_brace(text, match.end() - 1)
            if end is None:
                continue
            start_line = line_number(match.start())
            end_line = line_number(end)
            lines = end_line - start_line + 1
            body = text[match.start() : end + 1]
            branches = len(re.findall(r"\b(if|for|while|case|catch)\b|&&|\|\||\?", body))
            kind = match.groupdict().get("kind") or "function"
            threshold = thresholds.class_lines if kind == "class" else thresholds.function_lines
            if lines >= threshold or branches >= thresholds.branch_count:
                metric = BlockMetric(
                    path=relpath(path, root),
                    name=match.group("name"),
                    kind=kind,
                    start_line=start_line,
                    end_line=end_line,
                    lines=lines,
                    branches=branches,
                )
                if kind == "class":
                    classes.append(metric)
                else:
                    functions.append(metric)
    return functions, classes


def marker_counts(text: str) -> dict[str, int]:
    counts = {name: len(pattern.findall(text)) for name, pattern in MARKERS.items()}
    return {name: count for name, count in counts.items() if count}


def find_routes(path: Path, root: Path, text: str) -> list[dict[str, object]]:
    route_re = PY_ROUTE_RE if path.suffix == ".py" else TS_ROUTE_RE
    routes: list[dict[str, object]] = []
    for match in route_re.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        routes.append(
            {
                "path": relpath(path, root),
                "line": line,
                "method": match.group("method").upper(),
                "route": match.group("path"),
            }
        )
    return routes


def find_contract_definitions(path: Path, root: Path, text: str) -> list[dict[str, object]]:
    definitions: list[dict[str, object]] = []
    if path.name == "contract.json":
        definitions.append({"path": relpath(path, root), "line": 1, "symbol": "contract.json"})
    for match in CONTRACT_RE.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        definitions.append({"path": relpath(path, root), "line": line, "symbol": match.group("symbol")})
    return definitions


def generated_artifact_drift(root: Path) -> dict[str, list[str]]:
    tracked = {relpath(path, root) for path in git_ls_files(root)}
    present = [path for path in GENERATED_PATHS if (root / path).exists()]
    return {
        "tracked_generated_paths": sorted(path for path in present if path in tracked or any(item.startswith(f"{path}/") for item in tracked)),
        "present_generated_paths": sorted(present),
    }


def duplicate_routes(routes: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for route in routes:
        grouped[(str(route["method"]), str(route["route"]))].append(route)
    duplicates = []
    for (method, route), entries in grouped.items():
        if len(entries) > 1:
            duplicates.append({"method": method, "route": route, "definitions": entries})
    return sorted(duplicates, key=lambda item: (str(item["route"]), str(item["method"])))


def audit_project(root: Path, thresholds: Thresholds = Thresholds()) -> dict[str, object]:
    root = root.resolve()
    files = discover_source_files(root)

    module_hotspots: list[dict[str, object]] = []
    marker_hotspots: list[dict[str, object]] = []
    function_hotspots: list[BlockMetric] = []
    class_hotspots: list[BlockMetric] = []
    broad_exceptions: list[dict[str, object]] = []
    routes: list[dict[str, object]] = []
    contract_definitions: list[dict[str, object]] = []
    marker_totals: Counter[str] = Counter()
    language_counts: Counter[str] = Counter()
    total_lines = 0

    for path in files:
        relative = relpath(path, root)
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        loc = len(lines)
        total_lines += loc
        language = "python" if path.suffix == ".py" else "typescript"
        language_counts[language] += 1
        counts = marker_counts(text)
        marker_totals.update(counts)
        if loc >= thresholds.module_lines:
            module_hotspots.append({"path": relative, "language": language, "lines": loc, "markers": counts})
        if counts:
            marker_hotspots.append({"path": relative, "language": language, "lines": loc, "markers": counts})
        routes.extend(find_routes(path, root, text))
        contract_definitions.extend(find_contract_definitions(path, root, text))

        if path.suffix == ".py":
            functions, classes, exceptions = python_blocks(path, root, thresholds)
        else:
            functions, classes = ts_blocks(path, root, thresholds)
            exceptions = []
        function_hotspots.extend(functions)
        class_hotspots.extend(classes)
        broad_exceptions.extend(exceptions)

    return {
        "root": str(root),
        "thresholds": asdict(thresholds),
        "summary": {
            "source_files": len(files),
            "total_lines": total_lines,
            "languages": dict(sorted(language_counts.items())),
            "marker_totals": dict(sorted(marker_totals.items())),
            "module_hotspots": len(module_hotspots),
            "function_hotspots": len(function_hotspots),
            "class_hotspots": len(class_hotspots),
            "broad_exceptions": len(broad_exceptions),
            "routes": len(routes),
            "duplicate_routes": len(duplicate_routes(routes)),
            "contract_definitions": len(contract_definitions),
        },
        "module_hotspots": sorted(module_hotspots, key=lambda item: (-int(item["lines"]), str(item["path"]))),
        "function_hotspots": [asdict(item) for item in sorted(function_hotspots, key=lambda item: (-item.lines, item.path, item.name))],
        "class_hotspots": [asdict(item) for item in sorted(class_hotspots, key=lambda item: (-item.lines, item.path, item.name))],
        "broad_exceptions": sorted(broad_exceptions, key=lambda item: (str(item["path"]), int(item["line"]))),
        "marker_hotspots": sorted(marker_hotspots, key=lambda item: (-sum(item["markers"].values()), str(item["path"]))),
        "duplicate_routes": duplicate_routes(routes),
        "contract_definitions": sorted(contract_definitions, key=lambda item: (str(item["path"]), int(item["line"]))),
        "generated_artifact_drift": generated_artifact_drift(root),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root to audit.")
    parser.add_argument("--module-lines", type=int, default=Thresholds.module_lines)
    parser.add_argument("--function-lines", type=int, default=Thresholds.function_lines)
    parser.add_argument("--class-lines", type=int, default=Thresholds.class_lines)
    parser.add_argument("--branch-count", type=int, default=Thresholds.branch_count)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    thresholds = Thresholds(
        module_lines=args.module_lines,
        function_lines=args.function_lines,
        class_lines=args.class_lines,
        branch_count=args.branch_count,
    )
    try:
        print(json.dumps(audit_project(args.root, thresholds), indent=2, sort_keys=True))
    except BrokenPipeError:
        sys.stdout.close()
        raise SystemExit(0) from None


if __name__ == "__main__":
    main()
