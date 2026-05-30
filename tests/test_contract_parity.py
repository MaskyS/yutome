"""Parity tests between the Python contract registry and downstream surfaces.

These fail when:

- ``SKILL.md`` stops mentioning a tool name or resource URI template.
- The OAuth scope diverges between the Python and TS sides.

The point of the registry refactor is to remove these drift opportunities, so
the tests here are the safety net that prevents reintroducing them.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from yutome import contract, search_presets
from yutome.cli import search as search_cli
from yutome.hosted.mcp_query import HostedShowRequest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_JSON = REPO_ROOT / "cloudflare" / "yutome-capsule" / "src" / "contract.json"
SKILL_MD = REPO_ROOT / ".claude" / "skills" / "yutome-retrieval" / "SKILL.md"


def test_auth_scope_is_canonical() -> None:
    """The OAuth scope must be the same name in Python, the emitted JSON, and
    (by extension) the TS Worker that reads contract.json."""
    payload = json.loads(CONTRACT_JSON.read_text(encoding="utf-8"))
    assert contract.AUTH_SCOPE == "yutome.search.read"
    assert payload["auth_scope"] == "yutome.search.read"


@pytest.mark.parametrize("tool", [t.name for t in contract.TOOLS])
def test_skill_md_mentions_each_tool(tool: str) -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert tool in text, f"SKILL.md must mention tool name {tool!r}."


@pytest.mark.parametrize(
    "uri_template",
    [r.uri_template for r in contract.RESOURCES],
)
def test_skill_md_mentions_each_resource_template(uri_template: str) -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    # SKILL.md uses the placeholder-stripped form (e.g. yutome://chunk/{id}),
    # but contract uses the parameter name (e.g. {chunk_id}). Check for the
    # host segment instead, which is invariant.
    host = uri_template.removeprefix("yutome://").split("/", 1)[0]
    expected = f"yutome://{host}/"
    assert expected in text, f"SKILL.md must mention {expected}"


def test_resource_uri_template_host_matches_spec_host() -> None:
    """ResourceSpec.host is the dispatch key; ensure it matches the URI."""
    for spec in contract.RESOURCES:
        derived_host = spec.uri_template.removeprefix("yutome://").split("/", 1)[0]
        assert spec.host == derived_host, (
            f"ResourceSpec host {spec.host!r} does not match URI template "
            f"{spec.uri_template!r}"
        )


def test_search_presets_drive_contract_defaults() -> None:
    assert inspect.signature(contract.tool_find).parameters["limit"].default == search_presets.FIND_LIMIT_DEFAULT
    assert inspect.signature(contract.tool_list).parameters["limit"].default == search_presets.LIST_LIMIT_DEFAULT
    assert (
        inspect.signature(contract.tool_show).parameters["token_budget"].default
        == search_presets.TOKEN_BUDGET_DEFAULT
    )


def test_search_presets_drive_cli_options() -> None:
    find_limit = inspect.signature(search_cli.find_command).parameters["limit"].default
    list_limit = inspect.signature(search_cli.list_command).parameters["limit"].default
    show_token_budget = inspect.signature(search_cli.show_command).parameters["token_budget"].default
    show_transcript_limit = inspect.signature(search_cli.show_command).parameters["transcript_limit"].default

    assert find_limit.default == search_presets.FIND_LIMIT_DEFAULT
    assert find_limit.min == search_presets.LIMIT_MIN
    assert find_limit.max == search_presets.LIMIT_MAX
    assert list_limit.default == search_presets.LIST_LIMIT_DEFAULT
    assert list_limit.min == search_presets.LIMIT_MIN
    assert list_limit.max == search_presets.LIMIT_MAX
    assert show_token_budget.default == search_presets.TOKEN_BUDGET_DEFAULT
    assert show_token_budget.min == search_presets.TOKEN_BUDGET_MIN
    assert show_token_budget.max == search_presets.TOKEN_BUDGET_MAX
    assert show_transcript_limit.default is None
    assert show_transcript_limit.min == search_presets.TRANSCRIPT_LIMIT_MIN
    assert show_transcript_limit.max == search_presets.TRANSCRIPT_LIMIT_MAX


def test_clamp_helpers_match_legacy_behavior() -> None:
    assert search_presets.clamp_limit(0) == 1
    assert search_presets.clamp_limit(10_000) == search_presets.LIMIT_MAX
    assert search_presets.clamp_token_budget(0) == search_presets.TOKEN_BUDGET_MIN
    assert search_presets.clamp_token_budget(99_999) == search_presets.TOKEN_BUDGET_MAX
    assert search_presets.clamp_per_group_limit(0) == 1
    assert search_presets.clamp_per_group_limit(50) == search_presets.PER_GROUP_LIMIT_MAX
    assert search_presets.grouped_candidate_limit(50, 3) == search_presets.LIMIT_MAX
    assert search_presets.grouped_candidate_limit(2, 3) == 48


def test_enumerations_match_existing_validation() -> None:
    assert search_presets.SEARCH_MODES == ("lexical", "semantic", "hybrid", "none")
    assert search_presets.GROUP_BY_KEYS == ("video", "channel", "transcript_source")
    assert search_presets.LIST_ENTITIES == ("video", "videos", "channel", "channels", "status")
    assert search_presets.SHOW_KINDS == ("chunk", "video", "channel", "transcript", "context", "source")
    assert set(search_presets.SHOW_KINDS) == set(HostedShowRequest.SUPPORTED_KINDS)
