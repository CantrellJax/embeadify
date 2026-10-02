"""The deterministic executor: validate a typed recommendation, then build the `bd` argument arrays.

Nothing here executes, splits, or follows text from a candidate. Every `bd` call is assembled from
validated fields: a title and description that were cleaned and length-capped, ids that passed the id
pattern AND exist in the fresh snapshot, and integers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import engine, sanitize, snapshot
from ..decisions import Op
from . import candidate as cand
from .match import Neighbor
from .policy import Policy
from .recommend import Recommendation

MAX_NOTE = 3000
MAX_FOLD_EXCERPT = 1500


@dataclass
class Plan:
    action: str
    recommended: str
    target_id: str | None = None
    parent: str | None = None
    placement_rule: str | None = None  # which rule chose the parent of a create (None: not a create)
    unplaced: bool = False  # a create that no rule could place: `scribe report` lists it
    reasons: list[str] = field(default_factory=list)  # why the action changed or the fallback was used
    adjustments: list[str] = field(default_factory=list)  # edits that did not change the action
    items: list[engine.Item] = field(default_factory=list)

    @property
    def downgraded(self) -> bool:
        return self.action != self.recommended

    def steps(self, limit: int = 1000) -> list[dict]:
        return [
            {"bd": [a if len(a) <= limit else a[:limit] + "...[cut]" for a in i.argv]} for i in self.items
        ]


def _live(snap: snapshot.Snapshot, ident: str | None) -> bool:
    return ident is not None and ident in snap and snap[ident].status != "closed"


def _placeable(snap: snapshot.Snapshot, ident: str | None, policy: Policy) -> bool:
    """A parent for placement must exist in the fresh snapshot and be live (never closed)."""
    if not _live(snap, ident):
        return False
    return policy.allow_deferred_parent or snap[ident].status != "deferred"


def _placement_choices(c: dict, rec: Recommendation, fold_origin, neighbors, snap, policy: Policy):
    """(rule, parent id or None) in precedence order. Lazily evaluated; the first placeable one wins."""
    yield "recommender_parent", rec.parent
    yield "fold_target_parent", snap[fold_origin].parent if fold_origin else None
    yield "candidate_hint", c.get("parent")  # (a)
    source = c["source"].get("bead")  # (b)
    yield "source_bead_parent", snap[source].parent if source in snap else None
    for n in sorted(neighbors, key=lambda n: (-n.similarity, n.issue_id)):  # (c)
        if n.similarity < policy.placement_min_similarity:
            break
        if _live(snap, n.issue_id) and _placeable(snap, snap[n.issue_id].parent, policy):
            yield "neighbor_parent", snap[n.issue_id].parent
            break
    yield "type_parent", policy.type_parent.get(c["type"])
    yield "default_parent", policy.default_parent  # (d)


def _place(c, rec, fold_origin, neighbors, snap, policy: Policy, out: Plan) -> None:
    for rule, choice in _placement_choices(c, rec, fold_origin, neighbors, snap, policy):
        if choice is None:
            continue
        if _placeable(snap, choice, policy):
            out.parent, out.placement_rule = choice, rule
            return
        out.adjustments.append(f"parent_not_live_or_unknown:{choice}")
    out.placement_rule, out.unplaced = "unplaced", True  # (e)


def _source_line(c: dict) -> str:
    src = c["source"]
    return "Source: " + " ".join(f"{k}={sanitize.clean_line(v, 200)}" for k, v in src.items())


def _refs(c: dict) -> list[str]:
    return [sanitize.clean_line(r, 300) for r in c["evidence_refs"]][:20]


def _create_items(c: dict, plan: Plan, policy: Policy, related: str | None, note_priority: str) -> None:
    cid = c["candidate_id"]
    title = sanitize.clean_line(c["title"], policy.max_title) or f"(untitled candidate {cid})"
    body, truncated = sanitize.clean_block(c["body"], policy.max_body)
    parts = [body] if body else []
    if truncated:
        parts.append(
            f"[body truncated at {policy.max_body} characters; the full text stays in the scribe queue]"
        )
    meta = [_source_line(c)]
    if note_priority:
        meta.append(note_priority)
    if related:
        meta.append(f"Related: {related} (a fold that was not confirmed, so this is a linked bead)")
    refs = _refs(c)
    if refs:
        meta.append("Evidence refs (plain text, never fetched):\n" + "\n".join(f"- {r}" for r in refs))
    description = engine.description_with_marker("\n\n".join(parts), cid, ["\n".join(meta)])
    priority = max(c["priority"], policy.min_priority)
    argv = engine.create_argv(title, c["type"], priority, plan.parent, description)
    op = Op("create", cid, f"type={c['type']} priority={priority} title={title!r}")
    plan.items.append(
        engine.Item(op=op, argv=argv, marker=f"candidate:{cid}", watch=[plan.parent] if plan.parent else [])
    )


def _note_items(c: dict, plan: Plan, rec: Recommendation, neighbor: Neighbor, content_hash: str) -> None:
    cid, target = c["candidate_id"], plan.target_id
    lines = [
        sanitize.marker_line("provenance", cid),
        f"{plan.action}: candidate {cid} (hash {content_hash[:12]}) matched this bead"
        f" at similarity {neighbor.similarity:.2f}",
        _source_line(c),
        "Title: " + sanitize.clean_line(c["title"], 200),
    ]
    refs = _refs(c)
    if refs:
        lines.append("Evidence refs (plain text, never fetched): " + "; ".join(refs))
    lines.extend("Recommender evidence: " + sanitize.clean_line(e, 300) for e in rec.evidence[:5])
    if plan.action == "fold":
        excerpt, _ = sanitize.clean_block(c["body"], MAX_FOLD_EXCERPT)
        if excerpt:
            lines.append("Finding:\n" + excerpt)
    text = "\n".join(lines)[:MAX_NOTE]
    op = Op("note", target, text)
    plan.items.append(
        engine.Item(
            op=op, argv=engine._argv(op), marker=f"provenance:{cid}", marker_on=target, watch=[target]
        )
    )


def _target_problem(rec: Recommendation, neighbor: Neighbor | None, snap, policy: Policy) -> str:
    """Empty string when the target is acceptable for dup/fold/drop, otherwise the reason it is not."""
    target = rec.target_id
    if target is None:
        return "missing_target"
    if target not in snap:
        return "target_not_in_snapshot"
    if neighbor is None:
        return "target_not_a_retrieved_neighbor"
    if neighbor.similarity < policy.min_similarity:
        return "similarity_below_floor"
    if rec.confidence < policy.min_confidence:
        return "confidence_below_floor"
    if not rec.evidence or not any(e.strip() for e in rec.evidence):
        return "no_evidence"
    if snap[target].status == "closed":
        if rec.action == "fold":
            return "fold_into_closed_target"
        if not neighbor.resolution_evidence.strip():
            return "closed_target_without_resolution_evidence"
    if rec.action == "drop" and not any(target in e and len(e.strip()) >= 12 for e in rec.evidence):
        return "drop_evidence_not_specific"
    if rec.action == "fold" and not rec.acceptance_covered:
        return "acceptance_not_confirmed"
    return ""


def plan(
    c: dict,
    rec: Recommendation,
    neighbors: list[Neighbor],
    snap: snapshot.Snapshot,
    policy: Policy,
    notes: list[str] | None = None,
) -> Plan:
    cid, content_hash = c["candidate_id"], cand.digest(c)
    out = Plan(action=rec.action, recommended=rec.action, reasons=list(notes or []))
    if rec.candidate_id != cid:  # a recommendation for another candidate is never applied
        out.action, out.target_id = "create", None
        out.reasons.append("recommendation_for_another_candidate")
        rec = Recommendation(cid, "create")
    by_id = {n.issue_id: n for n in neighbors}
    fold_origin = rec.target_id if rec.action == "fold" and rec.target_id in snap else None

    def downgrade(reason: str) -> None:
        out.action = "create"
        out.reasons.append(reason)

    if out.action != "create" and out.action not in policy.allowed_actions:
        downgrade("action_not_allowed")
    if out.action != "create" and cand.is_urgent(c):
        downgrade("urgent_always_create")
    if out.action != "create" and c.get("supersedes"):
        downgrade("challenge_always_create")
    if out.action != "create":
        problem = _target_problem(rec, by_id.get(rec.target_id or ""), snap, policy)
        if problem:
            downgrade(problem)
    if out.action == "create":
        out.target_id = None
        _place(c, rec, fold_origin, neighbors, snap, policy, out)
        priority_note = ""
        if c["priority"] < policy.min_priority:
            out.adjustments.append("priority_clamped")
            priority_note = (
                f"Producer-declared priority P{c['priority']} is unverified;"
                f" created as P{policy.min_priority}."
            )
        _create_items(c, out, policy, fold_origin, priority_note)
    elif out.action in ("dup", "fold"):
        out.target_id = rec.target_id
        _note_items(c, out, rec, by_id[rec.target_id], content_hash)
    else:  # drop: nothing is written to the tracker; the receipt keeps the candidate searchable
        out.target_id = rec.target_id
    return out
