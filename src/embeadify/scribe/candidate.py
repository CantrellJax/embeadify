"""The candidate schema: validation, canonical form, content hash, receipt id."""

from __future__ import annotations

import hashlib
import json
import re

from ..decisions import CREATE_TYPES
from ..sanitize import is_id

MAX_FILE_BYTES = 256 * 1024
MAX_TITLE = 500
MAX_BODY = 100_000
MAX_REFS = 50
MAX_REF = 1000
MAX_LABELS = 10
MAX_LABEL = 64
KEYS = {
    "candidate_id",
    "title",
    "body",
    "type",
    "priority",
    "source",
    "evidence_refs",
    "parent",
    "labels",
    "supersedes",
    "kind",
}
KINDS = ("task", "bug", "question_chief", "question_owner", "decision")
QUESTION_KINDS = ("question_chief", "question_owner", "decision")
CHIEF_LABELS = ("questions",)
OWNER_LABELS = ("ask:owner",)
REPLAY_MARKER = re.compile(r"(?im)^\s*embeadify-replay:")
REPLAY_ID_PREFIX = "replay-"
REQUIRED = ("candidate_id", "title", "body", "type", "priority", "source")


class CandidateError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _priority(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 4 else None
    if isinstance(value, str) and re.fullmatch(r"[Pp]?[0-4]", value):
        return int(value[-1])
    return None


def validate(obj) -> dict:
    """Return the normalised candidate or raise CandidateError listing every problem."""
    if not isinstance(obj, dict):
        raise CandidateError(["candidate must be a JSON object"])
    errors: list[str] = []
    errors.extend(f"unknown field {k!r}" for k in sorted(set(obj) - KEYS))
    errors.extend(f"missing field {k!r}" for k in REQUIRED if k not in obj)
    out: dict = {}

    def text(key: str, limit: int, required: bool = True) -> str | None:
        value = obj.get(key)
        if key not in obj or value is None:
            return None
        if not isinstance(value, str):
            errors.append(f"{key} must be a string")
            return None
        if len(value) > limit:
            errors.append(f"{key} is longer than {limit} characters")
            return None
        if required and not value.strip():
            errors.append(f"{key} must not be empty")
            return None
        return value

    cid = obj.get("candidate_id")
    if "candidate_id" in obj:
        if is_id(cid):
            out["candidate_id"] = cid
        else:
            errors.append("candidate_id must be 1-96 characters: letters, digits, . _ : - (not first)")
    if (title := text("title", MAX_TITLE)) is not None:
        out["title"] = title
    if (body := text("body", MAX_BODY, required=False)) is not None:
        out["body"] = body
    if "type" in obj:
        if obj["type"] in CREATE_TYPES:
            out["type"] = obj["type"]
        else:
            errors.append(f"type must be one of {', '.join(CREATE_TYPES)}")
    if "priority" in obj:
        prio = _priority(obj["priority"])
        if prio is None:
            errors.append("priority must be 0-4 or P0-P4")
        else:
            out["priority"] = prio
    source = obj.get("source")
    if "source" in obj:
        if not isinstance(source, dict) or set(source) - {"agent", "pr", "bead"}:
            errors.append("source must be an object with only agent, pr, bead")
        elif not isinstance(source.get("agent"), str) or not source["agent"].strip():
            errors.append("source.agent is required")
        elif any(v is not None and (not isinstance(v, str) or len(v) > 200) for v in source.values()):
            errors.append("source values must be strings of at most 200 characters")
        else:
            out["source"] = {k: v for k, v in sorted(source.items()) if v}
    for key in ("evidence_refs", "labels"):
        if key not in obj:
            continue
        limit, each = (MAX_REFS, MAX_REF) if key == "evidence_refs" else (MAX_LABELS, MAX_LABEL)
        value = obj[key]
        if not isinstance(value, list) or len(value) > limit:
            errors.append(f"{key} must be a list of at most {limit} strings")
        elif any(not isinstance(v, str) or len(v) > each for v in value):
            errors.append(f"every {key} entry must be a string of at most {each} characters")
        else:
            out[key] = list(value)
    if obj.get("kind") is not None:
        if obj["kind"] in KINDS:
            out["kind"] = obj["kind"]
        else:
            errors.append(f"kind must be one of {', '.join(KINDS)}")
    for key in ("parent", "supersedes"):
        if obj.get(key) is not None:
            if is_id(obj[key]):
                out[key] = obj[key]
            else:
                errors.append(f"{key} must be an id")
    if errors:
        raise CandidateError(errors)
    out.setdefault("body", "")
    out.setdefault("evidence_refs", [])
    return out


def canonical(candidate: dict) -> bytes:
    return json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def digest(candidate: dict) -> str:
    return hashlib.sha256(canonical(candidate)).hexdigest()


def receipt_id(candidate_id: str, content_hash: str) -> str:
    return "rcpt-" + hashlib.sha256(f"{candidate_id}\0{content_hash}".encode()).hexdigest()[:16]


def is_urgent(candidate: dict) -> bool:
    """Self-declared urgency. It buys no privilege: urgent candidates are only ever created."""
    labels = [label.lower() for label in candidate.get("labels", [])]
    return candidate["priority"] <= 1 or "security" in labels or "security" in candidate["title"].lower()


def kind_of(candidate: dict) -> str:
    """The candidate's kind: the producer's `kind`, else derived (the label or title says it is a question).

    `ask:owner` => question_owner; `questions` => question_chief; a title starting with QUESTION and no
    label to say whose question it is => question_owner (the safer door: a bead plus an owner route, never a
    silent note); otherwise the type decides (`bug` => bug, anything else => task).
    """
    if candidate.get("kind"):
        return candidate["kind"]
    labels = {label.lower() for label in candidate.get("labels", [])}
    if labels & set(OWNER_LABELS):
        return "question_owner"
    if labels & set(CHIEF_LABELS):
        return "question_chief"
    if candidate["title"].lstrip().upper().startswith("QUESTION"):
        return "question_owner"
    return "bug" if candidate.get("type") == "bug" else "task"


def self_trigger_reason(candidate: dict, self_names: tuple[str, ...]) -> str | None:
    """Why a candidate would make the scribe react to its own output, or None. Checked at submit."""
    agent = candidate["source"]["agent"].strip().lower()
    if agent in {name.lower() for name in self_names}:
        return f"source.agent {candidate['source']['agent']!r} is the scribe itself"
    if REPLAY_MARKER.search(candidate.get("body", "")) or candidate["candidate_id"].startswith(
        REPLAY_ID_PREFIX
    ):
        return "the candidate carries a replay marker (replay never queues)"
    return None
