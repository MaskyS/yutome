from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yutome.api import find as api_find
from yutome.config import AppConfig
from yutome.paths import ProjectPaths


class EvalCase(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    query: str
    mode: Literal["lexical", "semantic", "hybrid", "none"] | None = None
    channel: str | None = None
    source: str | None = None
    language: str | None = None
    group_by: Literal["video", "channel", "transcript_source"] | None = None
    limit: int = Field(default=10, ge=1, le=200)
    expected_video_ids: list[str] = Field(default_factory=list)
    expected_chunk_ids: list[str] = Field(default_factory=list)
    expected_terms: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _has_expectation(self) -> "EvalCase":
        if not (self.expected_video_ids or self.expected_chunk_ids or self.expected_terms):
            raise ValueError("eval case needs at least one expected_video_ids, expected_chunk_ids, or expected_terms entry")
        return self


class EvalSuite(BaseModel):
    cases: list[EvalCase]


def load_eval_suite(path: Path) -> EvalSuite:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return EvalSuite.model_validate(payload)


def run_eval_suite(*, config: AppConfig, paths: ProjectPaths, suite: EvalSuite) -> dict[str, Any]:
    case_results = [_run_case(config=config, paths=paths, case=case) for case in suite.cases]
    passed = sum(1 for result in case_results if result["passed"])
    return {
        "total": len(case_results),
        "passed": passed,
        "failed": len(case_results) - passed,
        "cases": case_results,
    }


def _run_case(*, config: AppConfig, paths: ProjectPaths, case: EvalCase) -> dict[str, Any]:
    result = api_find(
        config=config,
        paths=paths,
        text=case.query,
        mode=case.mode,
        channel=case.channel,
        source=case.source,
        language=case.language,
        group_by=case.group_by,
        limit=case.limit,
    ).model_dump()
    rows = result.get("rows", [])
    video_ids = _collect_values(rows, "video_id")
    chunk_ids = _collect_values(rows, "chunk_id")
    haystack = _text_haystack(rows)

    missing_video_ids = [video_id for video_id in case.expected_video_ids if video_id not in video_ids]
    missing_chunk_ids = [chunk_id for chunk_id in case.expected_chunk_ids if chunk_id not in chunk_ids]
    missing_terms = [term for term in case.expected_terms if term.lower() not in haystack]
    passed = not (missing_video_ids or missing_chunk_ids or missing_terms)

    return {
        "name": case.name,
        "query": case.query,
        "passed": passed,
        "returned": len(rows),
        "video_ids": sorted(video_ids),
        "chunk_ids": sorted(chunk_ids),
        "missing_video_ids": missing_video_ids,
        "missing_chunk_ids": missing_chunk_ids,
        "missing_terms": missing_terms,
        "notes": result.get("notes", []),
    }


def _collect_values(rows: list[dict[str, Any]], key: str) -> set[str]:
    values: set[str] = set()
    for row in rows:
        value = row.get(key)
        if isinstance(value, str):
            values.add(value)
        hits = row.get("hits")
        if isinstance(hits, list):
            values.update(_collect_values([hit for hit in hits if isinstance(hit, dict)], key))
    return values


def _text_haystack(rows: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    fields = ("title", "snippet", "text", "description", "channel_title")
    for row in rows:
        for field in fields:
            value = row.get(field)
            if isinstance(value, str):
                parts.append(value)
        hits = row.get("hits")
        if isinstance(hits, list):
            parts.append(_text_haystack([hit for hit in hits if isinstance(hit, dict)]))
    return "\n".join(parts).lower()
