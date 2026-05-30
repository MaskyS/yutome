from __future__ import annotations

from typing import Literal, TypeAlias

FIND_LIMIT_DEFAULT = 10
LIST_LIMIT_DEFAULT = 20
LIMIT_MIN = 1
LIMIT_MAX = 200
OFFSET_MIN = 0

TOKEN_BUDGET_DEFAULT = 3000
TOKEN_BUDGET_MIN = 200
TOKEN_BUDGET_MAX = 8000

TRANSCRIPT_LIMIT_MIN = 1
TRANSCRIPT_LIMIT_MAX = 5000

PER_GROUP_LIMIT_DEFAULT = 3
PER_GROUP_LIMIT_MIN = 1
PER_GROUP_LIMIT_MAX = 20

GROUP_FANOUT_FACTOR = 8

SEARCH_MODES = ("lexical", "semantic", "hybrid", "none")
GROUP_BY_KEYS = ("video", "channel", "transcript_source")
LIST_ENTITIES = ("video", "videos", "channel", "channels", "status")
SHOW_KINDS = ("chunk", "video", "channel", "transcript", "context", "source")

SearchMode: TypeAlias = Literal[*SEARCH_MODES]
GroupByKey: TypeAlias = Literal[*GROUP_BY_KEYS]
ListEntity: TypeAlias = Literal[*LIST_ENTITIES]
ShowKind: TypeAlias = Literal[*SHOW_KINDS]


def clamp_limit(value: int) -> int:
    return max(LIMIT_MIN, min(value, LIMIT_MAX))


def clamp_offset(value: int) -> int:
    return max(OFFSET_MIN, value)


def clamp_token_budget(value: int) -> int:
    return max(TOKEN_BUDGET_MIN, min(value, TOKEN_BUDGET_MAX))


def clamp_transcript_limit(value: int) -> int:
    return max(TRANSCRIPT_LIMIT_MIN, min(value, TRANSCRIPT_LIMIT_MAX))


def clamp_per_group_limit(value: int) -> int:
    return max(PER_GROUP_LIMIT_MIN, min(value, PER_GROUP_LIMIT_MAX))


def grouped_candidate_limit(limit: int, per_group_limit: int) -> int:
    return min(LIMIT_MAX, limit * max(PER_GROUP_LIMIT_MIN, per_group_limit) * GROUP_FANOUT_FACTOR)
