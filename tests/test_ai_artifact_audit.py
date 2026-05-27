from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_ai_artifacts.py"


def load_audit_module():
    spec = importlib.util.spec_from_file_location("audit_ai_artifacts", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_sample_project(root: Path) -> None:
    src = root / "src"
    src.mkdir()
    branches = "\n".join(
        f"    if value == {index}:\n        value += {index}"
        for index in range(6)
    )
    (src / "sample.py").write_text(
        "\n".join(
            [
                "from typing import Any",
                "",
                "def oversized(value: Any) -> int:  # noqa: ANN401",
                branches,
                "    try:",
                "        return value",
                "    except Exception:",
                "        return 0",
                "",
                "class LegacyCapsuleShim:",
                "    pass  # type: ignore[misc]",
            ]
        ),
        encoding="utf-8",
    )
    app = root / "web" / "app"
    app.mkdir(parents=True)
    (app / "routes.ts").write_text(
        "\n".join(
            [
                'app.get("/healthz", () => {',
                "  if (Math.random()) { return true; }",
                "});",
                'app.get("/healthz", () => {',
                "  return false;",
                "});",
            ]
        ),
        encoding="utf-8",
    )
    (root / "web" / "worker-configuration.d.ts").write_text("declare const generated: string;\n", encoding="utf-8")


def test_audit_project_detects_hotspots_and_markers(tmp_path: Path) -> None:
    write_sample_project(tmp_path)
    audit = load_audit_module()

    result = audit.audit_project(
        tmp_path,
        audit.Thresholds(module_lines=5, function_lines=5, class_lines=2, branch_count=3),
    )

    assert result["summary"]["source_files"] == 2
    assert result["summary"]["module_hotspots"] >= 1
    assert result["summary"]["function_hotspots"] >= 1
    assert result["summary"]["broad_exceptions"] == 1
    assert result["summary"]["duplicate_routes"] == 1
    assert result["summary"]["marker_totals"]["any_type"] >= 1
    assert result["summary"]["marker_totals"]["capsule"] >= 1
    assert result["generated_artifact_drift"]["present_generated_paths"] == ["web/worker-configuration.d.ts"]


def test_audit_cli_outputs_json(tmp_path: Path) -> None:
    write_sample_project(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(tmp_path),
            "--module-lines",
            "5",
            "--function-lines",
            "5",
            "--class-lines",
            "2",
            "--branch-count",
            "3",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["summary"]["duplicate_routes"] == 1
    assert payload["function_hotspots"][0]["path"] == "src/sample.py"
