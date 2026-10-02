"""One pass of the scribe: for each queued candidate, match, recommend, validate, (maybe) write, log."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .. import engine, snapshot
from . import executor, match, recommend
from . import store as st
from .policy import Policy


@dataclass
class Summary:
    seen: int = 0
    applied: int = 0
    reconciled: int = 0
    shadowed: int = 0
    failed: int = 0
    invalid: int = 0
    skipped: int = 0
    details: list[str] = field(default_factory=list)


def _entry(sub: st.Submission, mode: str, policy: Policy) -> dict:
    return {
        "candidate_id": sub.candidate["candidate_id"],
        "hash": sub.hash,
        "receipt_id": sub.receipt_id,
        "policy_version": policy.policy_version,
        "mode": mode,
        "ts": st.now(),
    }


def _receipt(entry: dict, **extra) -> dict:
    keep = ("candidate_id", "hash", "receipt_id", "policy_version", "mode")
    return {"schema_version": 1, "decided_at": st.now(), **{k: entry[k] for k in keep}, **extra}


def process(
    queue: st.Queue,
    sub: st.Submission,
    policy: Policy,
    *,
    live: bool,
    recommender: tuple[str, ...],
    timeout: float,
    summary: Summary,
) -> None:
    c = sub.candidate
    cid = c["candidate_id"]
    mode = "live" if live else "shadow"
    entry = _entry(sub, mode, policy)
    snap = snapshot.take()

    # Exactly once, ever: any marker for this candidate anywhere means it already landed.
    hit = snapshot.find_marker(snap, cid)
    if hit:
        kind, bead = hit
        entry.update(
            recommendation=None,
            executor_plan={"action": "already_applied", "steps": []},
            outcome="reconciled",
            bead_id=bead,
            marker_kind=kind,
        )
        if live:
            attempts = queue.lease_attempts(cid)
            queue.write_decision(
                sub.receipt_id,
                _receipt(
                    entry,
                    final_action="create" if kind == "candidate" else "dup",
                    outcome="reconciled",
                    bead_id=bead,
                    redelivered=attempts > 0,
                ),
            )
            queue.release_lease(cid)
            summary.reconciled += 1
        else:
            summary.shadowed += 1
        queue.log(entry)
        return

    neighbors, degraded = match.fetch(c, policy.match_command, timeout)
    notes = [degraded] if degraded else []
    if recommender:
        outcome = recommend.external(recommender, c, neighbors, policy, timeout)
        rec, notes = outcome.recommendation, notes + outcome.notes
    else:
        rec = recommend.builtin(c, neighbors, policy)
    plan = executor.plan(c, rec, neighbors, snap, policy, notes)
    entry.update(
        recommendation=rec.to_dict(),
        neighbors=[n.to_dict() for n in neighbors],
        degraded=degraded or None,
        executor_plan={
            "action": plan.action,
            "target_id": plan.target_id,
            "parent": plan.parent,
            "downgraded": plan.downgraded,
            "reasons": plan.reasons,
            "adjustments": plan.adjustments,
            "steps": plan.steps(),
        },
    )
    if not live:
        entry["outcome"] = "shadow"
        summary.shadowed += 1
        queue.log(entry)
        return

    attempts = queue.take_lease(cid)
    for item in plan.items:
        engine.run_item(item, timeout)
        if item.outcome != "ok":
            entry.update(outcome="failed", error=item.message[:300], attempts=attempts)
            summary.failed += 1
            summary.details.append(f"{cid}: {item.message[:200]}")
            queue.log(entry)
            return  # the lease stays: the candidate remains queued and the next run reconciles first
    item = plan.items[0] if plan.items else None
    bead = (item.created_id or item.marker_on) if item else (plan.target_id or "")
    reconciled = bool(item and item.message.startswith("reconciled"))
    result = "dropped" if plan.action == "drop" else ("reconciled" if reconciled else "applied")
    entry.update(outcome=result, bead_id=bead or None, attempts=attempts)
    undo = f"close {bead} embeadify undo: scribe create {cid}" if plan.action == "create" and bead else None
    queue.write_decision(
        sub.receipt_id,
        _receipt(
            entry,
            final_action=plan.action,
            recommended_action=plan.recommended,
            reasons=plan.reasons,
            outcome=result,
            bead_id=bead or None,
            target_id=plan.target_id,
            undo=undo,
            redelivered=attempts > 1,
            recommendation=rec.to_dict(),
        ),
    )
    queue.release_lease(cid)
    summary.reconciled += result == "reconciled"
    summary.applied += result == "applied"
    summary.skipped += result == "dropped"
    queue.log(entry)


def run_once(
    queue: st.Queue,
    policy: Policy,
    *,
    live: bool,
    recommender: tuple[str, ...] = (),
    timeout: float = 120.0,
    limit: int | None = None,
) -> Summary:
    summary = Summary()
    handled = 0
    for sub in queue.submissions():
        summary.seen += 1
        if sub.error:
            summary.invalid += 1
            summary.details.append(f"{sub.path.name}: invalid submission ({sub.error})")
            continue
        if queue.decision(sub.receipt_id):
            continue
        if limit is not None and handled >= limit:
            break
        handled += 1
        process(queue, sub, policy, live=live, recommender=recommender, timeout=timeout, summary=summary)
    return summary


def run_loop(queue, policy, *, live, recommender, timeout, interval: float, limit=None) -> Summary:
    total = Summary()
    try:
        while True:
            total = run_once(queue, policy, live=live, recommender=recommender, timeout=timeout, limit=limit)
            time.sleep(interval)
    except KeyboardInterrupt:
        return total
