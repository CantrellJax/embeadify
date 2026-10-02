"""Typed recommendations. A recommender only proposes; the executor decides what is allowed to run."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field

from .. import bd, sanitize
from .match import Neighbor
from .policy import ACTIONS, Policy

MAX_OUTPUT = 64 * 1024
MAX_EVIDENCE = 20
MAX_EVIDENCE_LEN = 500


class BadRecommendation(ValueError):
    pass


@dataclass(frozen=True)
class Recommendation:
    candidate_id: str
    action: str
    target_id: str | None = None
    parent: str | None = None
    evidence: tuple[str, ...] = ()
    confidence: float = 0.0
    policy_version: str = ""
    acceptance_covered: bool = False  # fold only: the target's existing acceptance already covers it

    def to_dict(self) -> dict:
        d = asdict(self)
        d["evidence"] = list(self.evidence)
        return d


def from_obj(obj, candidate_id: str) -> Recommendation:
    """Strictly validate a recommender's JSON. Raises BadRecommendation."""
    if not isinstance(obj, dict):
        raise BadRecommendation("recommendation must be a JSON object")
    allowed = {f for f in Recommendation.__dataclass_fields__}
    if extra := sorted(set(obj) - allowed):
        raise BadRecommendation(f"unknown fields: {', '.join(extra)}")
    if obj.get("candidate_id") != candidate_id:
        raise BadRecommendation("candidate_id does not match the candidate")
    if obj.get("action") not in ACTIONS:
        raise BadRecommendation(f"action must be one of {', '.join(ACTIONS)}")
    for key in ("target_id", "parent"):
        if obj.get(key) is not None and not sanitize.is_id(obj[key]):
            raise BadRecommendation(f"{key} must be an id or null")
    evidence = obj.get("evidence", [])
    if (
        not isinstance(evidence, list)
        or len(evidence) > MAX_EVIDENCE
        or any(not isinstance(e, str) or len(e) > MAX_EVIDENCE_LEN for e in evidence)
    ):
        raise BadRecommendation("evidence must be a short list of short strings")
    conf = obj.get("confidence")
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1:
        raise BadRecommendation("confidence must be a number from 0 to 1")
    version = obj.get("policy_version", "")
    if not isinstance(version, str) or len(version) > 64:
        raise BadRecommendation("policy_version must be a short string")
    covered = obj.get("acceptance_covered", False)
    if not isinstance(covered, bool):
        raise BadRecommendation("acceptance_covered must be true or false")
    return Recommendation(
        candidate_id=candidate_id,
        action=obj["action"],
        target_id=obj.get("target_id"),
        parent=obj.get("parent"),
        evidence=tuple(sanitize.clean_line(e, MAX_EVIDENCE_LEN) for e in evidence),
        confidence=float(conf),
        policy_version=version,
        acceptance_covered=covered,
    )


def fallback(candidate_id: str, policy: Policy) -> Recommendation:
    return Recommendation(candidate_id, "create", policy_version=policy.policy_version)


def builtin(candidate: dict, neighbors: list[Neighbor], policy: Policy) -> Recommendation:
    """Create, unless a LIVE neighbor is at least `dup_threshold` similar: then `dup` onto it.

    Never `fold` (it cannot know a target's acceptance) and never `drop`. A closed neighbor is never a basis.
    """
    cid = candidate["candidate_id"]
    live = [
        n
        for n in neighbors
        if not n.is_closed and n.status != "closed" and n.similarity >= policy.dup_threshold
    ]
    if not live:
        return fallback(cid, policy)
    best = sorted(live, key=lambda n: (-n.similarity, n.issue_id))[0]
    return Recommendation(
        candidate_id=cid,
        action="dup",
        target_id=best.issue_id,
        evidence=(
            f"{best.issue_id} is open and {best.similarity:.2f} similar"
            f" (threshold {policy.dup_threshold:.2f})",
        ),
        confidence=best.similarity,
        policy_version=policy.policy_version,
    )


@dataclass
class Outcome:
    recommendation: Recommendation
    notes: list[str] = field(default_factory=list)


def external(
    command: tuple[str, ...],
    candidate: dict,
    neighbors: list[Neighbor],
    policy: Policy,
    timeout: float = 120.0,
) -> Outcome:
    """Run the plug-in recommender (JSON in, one typed recommendation out). Any problem => create."""
    cid = candidate["candidate_id"]
    payload = {
        "schema_version": 1,
        "policy_version": policy.policy_version,
        "untrusted_fields": ["candidate", "neighbors"],
        "candidate": {
            **{k: v for k, v in candidate.items() if k != "body"},
            "body": sanitize.clean_block(candidate["body"], policy.max_body)[0],
        },
        "neighbors": [n.to_dict() for n in neighbors],
        "constraints": {
            "allowed_actions": list(policy.allowed_actions),
            "min_similarity": policy.min_similarity,
            "min_confidence": policy.min_confidence,
            "llm_min_similarity": policy.llm_min_similarity,
        },
    }

    def bad(note: str) -> Outcome:
        return Outcome(fallback(cid, policy), [note])

    try:
        done = subprocess.run(
            list(command),
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError:
        return bad("recommender_missing")
    except (OSError, subprocess.TimeoutExpired) as error:
        return bad(f"recommender_failed: {type(error).__name__}")
    if done.returncode != 0:
        first = (
            bd.redact(done.stderr.strip().splitlines()[0])
            if done.stderr.strip()
            else f"exit {done.returncode}"
        )
        return bad(f"recommender_failed: {first[:200]}")
    if len(done.stdout) > MAX_OUTPUT:
        return bad("recommender_invalid_output: too large")
    try:
        return Outcome(from_obj(json.loads(done.stdout), cid))
    except (json.JSONDecodeError, BadRecommendation) as error:
        return bad(f"recommender_invalid_output: {error}"[:300])
