"""Owner-routing and never-drop guards: pure functions over the candidate, recommendation and snapshot.

Every guard only ever FORCES A CREATE (or a route): it can never suppress anything, so a wider match is
always the safe direction and a guard that misfires costs one extra bead, never lost work. They run in the
deterministic executor; the recommender (a model) can only recommend.

A. never fold, drop or dup when the candidate or the target
   - touches prod data, published schedules, money, privacy or security (keywords and labels, policy lists);
   - targets a bead that is in progress with an assignee;
   - targets an owner-held bead: an owner/human label or a decision-typed bead;
   - targets a bead whose notes or description say it closes only on evidence ("close only on prod
     evidence", "awaiting schema_migrations");
   - is relevant to the target only because it ANSWERS it (an owner adds an answer as a note on that bead).
B. a closed neighbor is a dup/drop basis only with an owner-attributed, dated, verbatim quote.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .. import snapshot
from .policy import Policy
from .recommend import Recommendation

DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
OWNER = re.compile(r"\b(?:Jackson|Clay)\b")
QUOTE = re.compile(r'"([^"]{12,})"|“([^”]{12,})”')
SUPERSEDED = re.compile(r"supersed", re.I)
ANSWERS = re.compile(
    r"\b(?:answers?|answered|answering|ruling|ruled|resolves? (?:the |this )?question"
    r"|owner (?:said|decided|confirmed))\b",
    re.I,
)
QUESTION_TITLE = re.compile(r"^\s*question\b", re.I)


@lru_cache(maxsize=64)
def _words(words: tuple[str, ...]):
    if not words:
        return None
    alt = "|".join(re.escape(" ".join(w.lower().split())) for w in words if w.strip())
    return re.compile(rf"(?<!\w)(?:{alt})(?!\w)") if alt else None


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _mentions(text: str, words: tuple[str, ...]) -> bool:
    rx = _words(tuple(words))
    return bool(rx and rx.search(_norm(text)))


def _label_set(labels) -> set[str]:
    return {str(x).lower() for x in labels}


def candidate_sensitive(c: dict, policy: Policy) -> bool:
    """Prod data, published schedules, money, privacy or security in the candidate's title, body or labels."""
    text = " ".join([c["title"], c.get("body", ""), *c.get("labels", [])])
    return bool(
        _label_set(c.get("labels", [])) & _label_set(policy.sensitive_labels)
        or _mentions(text, policy.sensitive_keywords)
    )


def question_like(state: snapshot.State) -> bool:
    return bool(
        QUESTION_TITLE.match(state.title)
        or state.issue_type == "decision"
        or _label_set(state.labels) & {"questions", "ask:owner"}
    )


def target_hits(c: dict, rec: Recommendation, state: snapshot.State, policy: Policy) -> list[str]:
    """Guard names (A) that fire for this target, in a fixed order."""
    hits = []
    if _label_set(state.labels) & _label_set(policy.sensitive_labels) or _mentions(
        state.title, policy.sensitive_keywords
    ):
        hits.append("sensitive_target")
    if state.status == "in_progress" and state.assignee:
        hits.append("claimed_target")
    if _label_set(state.labels) & _label_set(policy.owner_labels) or state.issue_type in policy.owner_types:
        hits.append("owner_held_target")
    if _mentions(state.notes + " " + state.description, policy.hold_phrases):
        hits.append("close_on_evidence_target")
    said = " ".join([c["title"], *rec.evidence])
    if ANSWERS.search(said) or QUESTION_TITLE.match(state.title):
        hits.append("answer_only")
    return hits


def hits(c: dict, rec: Recommendation, snap, policy: Policy) -> list[str]:
    """Every A guard that forces a create for a non-create recommendation."""
    out = ["sensitive_candidate"] if candidate_sensitive(c, policy) else []
    if rec.target_id and rec.target_id in snap:
        out += target_hits(c, rec, snap[rec.target_id], policy)
    return out


def _squash(text: str) -> str:
    return " ".join(text.split())


def closed_quote_problem(rec: Recommendation, resolution: str, state: snapshot.State) -> str:
    """B: empty when a closed neighbor may be a dup/drop basis, else the reason it may not.

    Needs ONE evidence string with a date (YYYY-MM-DD), a named owner (Jackson or Clay) and a double-quoted
    span of at least 12 characters that literally appears in the neighbor's close reason (the match report's
    `resolution_evidence`), and no `Superseded` marker in the target's notes or description or after the
    quote in the close reason. "Merged PR names the bead" or "absorbed by X" is not evidence. Nothing here
    classifies WHO wrote the close reason: a coordinator note or a ledger entry is not an owner ruling, and
    only the quote regex tells them apart.
    """
    if not resolution.strip():
        return "closed_target_without_resolution_evidence"
    source = _squash(resolution)
    for e in rec.evidence:
        if not (DATE.search(e) and OWNER.search(e)):
            continue
        for m in QUOTE.finditer(e):
            quote = _squash(m.group(1) or m.group(2))
            at = source.find(quote)
            if at < 0:
                continue
            after = source[at + len(quote) :]
            if (
                SUPERSEDED.search(after)
                or SUPERSEDED.search(state.notes)
                or SUPERSEDED.search(state.description)
            ):
                return "closed_target_superseded"
            return ""
    return "closed_target_without_owner_quote"
