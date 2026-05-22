"""Catch contract-vs-implementation drift by exercising the exact patterns
that yutome's tool descriptions and SKILL.md teach the model to use.

The motivating bug: the tool description for ``list`` says ``order_by=newest``,
but ``api._order`` never translated ``"newest"`` to a real OrderBy field, so
every call from Claude failed with a Pydantic validation error. Structural
parity tests didn't catch it because they verify shape, not behavior.

Each test here picks an instruction that appears in `contract.py` or
SKILL.md and turns it into an actual function call. They run against the
``_order`` helper directly (no DB needed), so they're fast and don't depend
on a populated catalog. Heavier end-to-end usage is covered by the eval
suite (`yutome eval`).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from yutome.api import _ORDER_BY_ALIASES, _order
from yutome.query import OrderBy

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_MD = REPO_ROOT / ".claude" / "skills" / "yutome-retrieval" / "SKILL.md"
CONTRACT_PY = REPO_ROOT / "src" / "yutome" / "contract.py"


# ---------- order_by aliases ----------


@pytest.mark.parametrize("alias", sorted(_ORDER_BY_ALIASES))
def test_every_order_by_alias_produces_a_valid_orderby(alias: str) -> None:
    """Every alias the description/SKILL can suggest must resolve to a valid
    OrderBy without raising. Pydantic would reject any unknown field."""
    orders = _order(alias)
    assert orders, f"_order({alias!r}) returned an empty list"
    assert isinstance(orders[0], OrderBy)
    # Asserts the Pydantic Literal accepted the field — no exception means OK.


def test_order_by_newest_resolves_to_published_at_desc() -> None:
    """The single biggest documented case: 'newest videos'."""
    orders = _order("newest")
    assert len(orders) == 1
    assert orders[0].field == "published_at"
    assert orders[0].direction == "desc"


def test_order_by_oldest_resolves_to_published_at_asc() -> None:
    orders = _order("oldest")
    assert len(orders) == 1
    assert orders[0].field == "published_at"
    assert orders[0].direction == "asc"


# ---------- Any string the descriptions mention as order_by must be an alias ----------


_ORDER_QUOTE_RE = re.compile(r"order_by\s*=\s*[\"'`]([a-z_]+)[\"'`]")


def _order_by_values_in(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return {m.group(1) for m in _ORDER_QUOTE_RE.finditer(text)}


def test_order_by_values_in_contract_and_skill_resolve() -> None:
    """If a description or SKILL.md says ``order_by=foo``, ``_order('foo')``
    must return at least one valid OrderBy (no Pydantic error)."""
    mentioned = _order_by_values_in(CONTRACT_PY) | _order_by_values_in(SKILL_MD)
    # Filter out the noise — the regex picks up Python keyword names too.
    candidates = {v for v in mentioned if v.isidentifier() and v != "order_by"}
    assert candidates, "regex should match at least one documented order_by usage"
    for value in candidates:
        orders = _order(value)
        assert orders, (
            f"Documentation mentions order_by={value!r} but _order({value!r}) "
            f"returned []; either alias it in api._ORDER_BY_ALIASES or stop "
            f"telling the model to use it."
        )
