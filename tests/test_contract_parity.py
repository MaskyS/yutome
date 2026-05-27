"""Parity tests between the Python contract registry and downstream surfaces.

These fail when:

- The TypeScript Worker's ``contract.json`` drifts from what
  the Python contract registry would produce now.
- ``SKILL.md`` stops mentioning a tool name or resource URI template.
- The OAuth scope diverges between the Python and TS sides.

The point of the registry refactor is to remove these drift opportunities, so
the tests here are the safety net that prevents reintroducing them.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from yutome import contract
from yutome.contract_export import build_contract_payload

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_JSON = REPO_ROOT / "cloudflare" / "yutome-capsule" / "src" / "contract.json"
SKILL_MD = REPO_ROOT / ".claude" / "skills" / "yutome-retrieval" / "SKILL.md"


def test_emitted_contract_json_matches_registry() -> None:
    """The committed contract.json must equal the Python registry output."""
    expected = build_contract_payload()
    on_disk = json.loads(CONTRACT_JSON.read_text(encoding="utf-8"))
    assert on_disk == expected, (
        "cloudflare/yutome-capsule/src/contract.json is stale. "
        "Refresh it through the internal contract export path and commit the change."
    )


def test_auth_scope_is_canonical() -> None:
    """The OAuth scope must be the same name in Python, the emitted JSON, and
    (by extension) the TS Worker that reads contract.json."""
    payload = json.loads(CONTRACT_JSON.read_text(encoding="utf-8"))
    assert contract.AUTH_SCOPE == "yutome.search.read"
    assert payload["auth_scope"] == "yutome.search.read"

    # mcp_server re-exports the same constant so existing imports keep working
    # but resolve to the canonical value.
    from yutome.mcp_server import REMOTE_READ_SCOPE

    assert REMOTE_READ_SCOPE == "yutome.search.read"


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
