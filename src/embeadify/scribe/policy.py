"""The scribe policy file (TOML). Live writes need `--live` AND `live = true` here."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..decisions import CREATE_TYPES
from ..sanitize import is_id

ACTIONS = ("create", "fold", "dup", "drop")


class PolicyError(Exception):
    pass


@dataclass(frozen=True)
class Policy:
    live: bool = False
    policy_version: str = "builtin-1"
    allowed_actions: tuple[str, ...] = ACTIONS
    dup_threshold: float = 0.95  # built-in recommender: similarity needed to say `dup`
    min_similarity: float = 0.85  # executor floor for any dup/fold/drop target
    min_confidence: float = 0.8  # executor floor on the recommendation's confidence
    max_title: int = 200
    max_body: int = 8000
    min_priority: int = 1  # a producer's P0 is created as P1: urgency is a guess, never a privilege
    match_command: tuple[str, ...] = ("embead", "match")
    recommender_command: tuple[str, ...] = ()
    placement_min_similarity: float = 0.6  # a neighbor's parent places a create only at this similarity
    default_parent: str | None = None  # placement rule (d): a container for otherwise unplaced creates
    type_parent: dict[str, str] = field(default_factory=dict)  # placement: issue type -> parent id
    allow_deferred_parent: bool = True  # a deferred parent is live for placement; closed never is
    llm_min_similarity: float = (
        0.80  # the reference LLM recommender skips the model below this (just under min_similarity)
    )
    max_llm_calls: int = 40  # per pass: past this, candidates skip the model and take the built-in path
    max_llm_tokens: int = 250_000  # per pass, input + output as the backend reports them


def llm_floor_note(policy: Policy) -> str | None:
    """One stderr line when the model pre-filter sits far below the executor floor."""
    if policy.min_similarity - policy.llm_min_similarity > 0.2 + 1e-9:
        return (
            f"note: llm_min_similarity {policy.llm_min_similarity} is more than 0.2 below the executor"
            f" similarity floor {policy.min_similarity}; those model calls rarely change decisions"
        )
    return None


def _check(key: str, value, kind) -> object:
    if kind == "strs":
        ok = isinstance(value, list) and all(isinstance(v, str) and v for v in value)
    elif kind is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 1
    elif kind == "id":
        ok = is_id(value)
    elif kind == "idmap":
        ok = isinstance(value, dict) and all(k in CREATE_TYPES and is_id(v) for k, v in value.items())
    elif kind is int:
        ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    else:
        ok = isinstance(value, kind)
    if not ok:
        raise PolicyError(f"policy key {key!r} has the wrong type or range")
    return value


def load(path: Path | None) -> Policy:
    if path is None:
        return Policy()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PolicyError(f"cannot read policy {path}: {error}") from error
    table = data.get("scribe", {})
    if not isinstance(table, dict) or set(data) - {"scribe"}:
        raise PolicyError("policy must contain only a [scribe] table")
    known = {
        "live": bool,
        "policy_version": str,
        "allowed_actions": "strs",
        "dup_threshold": float,
        "min_similarity": float,
        "min_confidence": float,
        "max_title": int,
        "max_body": int,
        "min_priority": int,
        "match_command": "strs",
        "recommender_command": "strs",
        "placement_min_similarity": float,
        "default_parent": "id",
        "type_parent": "idmap",
        "allow_deferred_parent": bool,
        "llm_min_similarity": float,
        "max_llm_calls": int,
        "max_llm_tokens": int,
    }
    if unknown := sorted(set(table) - set(known)):
        raise PolicyError(f"unknown policy keys: {', '.join(unknown)}")
    values = {k: _check(k, v, known[k]) for k, v in table.items()}
    if "allowed_actions" in values:
        bad = set(values["allowed_actions"]) - set(ACTIONS)
        if bad:
            raise PolicyError(f"unknown actions in allowed_actions: {', '.join(sorted(bad))}")
        values["allowed_actions"] = tuple(dict.fromkeys(["create", *values["allowed_actions"]]))
    for key in ("match_command", "recommender_command"):
        if key in values:
            values[key] = tuple(values[key])
    if values.get("min_priority", 1) > 4 or not 1 <= values.get("max_title", 200) <= 500:
        raise PolicyError("min_priority must be 0-4 and max_title 1-500")
    if not 1 <= values.get("max_body", 8000) <= 50_000:
        raise PolicyError("max_body must be 1-50000")
    return Policy(**values)
