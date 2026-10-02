"""One pass of the scribe: for each queued candidate, match, recommend, validate, (maybe) write, log."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from .. import bd, engine, snapshot
from . import candidate as cand
from . import executor, match, recommend
from . import store as st
from .policy import Policy

LOG_BODY = 4000
DEFAULT_JOBS = 4
MAX_JOBS = 8


def clamp_jobs(jobs: int | None) -> int:
    return max(1, min(MAX_JOBS, DEFAULT_JOBS if jobs is None else int(jobs)))


@dataclass
class Timing:
    """Where a pass spent its time. ``recommender_seconds`` is summed over calls, so it can exceed wall."""

    match_seconds: float = 0.0
    recommender_seconds: float = 0.0
    wall_seconds: float = 0.0
    match_calls: int = 0
    llm_calls: int = 0
    llm_skipped: int = 0
    budget_skipped: int = 0  # would have reached the model, but the budget was spent: built-in path instead
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_ms: int = 0
    llm_usage_calls: int = 0  # calls whose backend reported tokens (the denominator for averages)
    recommender_jobs: int = 1

    def to_dict(self) -> dict:
        d = {k: (round(v, 3) if isinstance(v, float) else v) for k, v in self.__dict__.items()}
        return d

    def line(self) -> str:
        text = (
            f"timing: wall {self.wall_seconds:.1f}s, match {self.match_seconds:.1f}s"
            f" ({self.match_calls} call{'s' if self.match_calls != 1 else ''}),"
            f" recommender {self.recommender_seconds:.1f}s summed over {self.recommender_jobs} job(s),"
            f" LLM calls made {self.llm_calls}, skipped {self.llm_skipped}"
        )
        if self.llm_usage_calls:
            n = self.llm_usage_calls
            text += (
                f"; tokens in {self.llm_input_tokens} out {self.llm_output_tokens}"
                f" (avg {self.llm_input_tokens // n} in, {self.llm_output_tokens // n} out,"
                f" {self.llm_ms // n} ms per call over {n} reporting)"
            )
        elif self.llm_calls:
            text += "; the backend reported no token usage"
        return text

    def note_decision(self, fields: dict) -> None:
        kind = fields["recommender"]
        if kind.get("budget_skipped"):
            self.budget_skipped += 1
        elif kind["kind"] == "external":
            if kind["model_called"]:
                self.llm_calls += 1
                self.llm_ms += fields.get("llm_ms") or 0
                if fields.get("llm_input_tokens") is not None or fields.get("llm_output_tokens") is not None:
                    self.llm_usage_calls += 1
                    self.llm_input_tokens += fields.get("llm_input_tokens") or 0
                    self.llm_output_tokens += fields.get("llm_output_tokens") or 0
            else:
                self.llm_skipped += 1


class Budget:
    """A cap on model calls and tokens for one pass, shared by every worker.

    Never silent: exhaustion is counted.

    Calls are reserved before a call starts (exact). Tokens are only known afterwards, so a worker checks the
    tokens already spent: with N calls in flight the cap can be passed by at most those N calls.
    A candidate whose best neighbor is under `llm_min_similarity` is below the model's floor: it reserves
    nothing up front (so it cannot crowd out a real call) and is counted afterwards only if the recommender
    called the model anyway. A call the pre-filter skipped is refunded.
    """

    def __init__(self, max_calls: int, max_tokens: int):
        self.max_calls, self.max_tokens = max_calls, max_tokens
        self.calls = self.tokens = 0
        self._lock = threading.Lock()

    def reserve(self) -> bool:
        with self._lock:
            if self.calls >= self.max_calls or self.tokens >= self.max_tokens:
                return False
            self.calls += 1
            return True

    def settle(self, rec: recommend.Recommendation, reserved: bool = True) -> None:
        with self._lock:
            spared = any("model not called" in e for e in rec.evidence)
            if reserved and spared:
                self.calls -= 1  # the pre-filter spared the model: nothing was spent
            elif not reserved and not spared:
                self.calls += 1  # an unreserved call that did reach the model still counts
            m = rec.metadata or {}
            self.tokens += (m.get("llm_input_tokens") or 0) + (m.get("llm_output_tokens") or 0)


def budget_for(policy: Policy, recommender: tuple[str, ...]) -> Budget | None:
    return Budget(policy.max_llm_calls, policy.max_llm_tokens) if recommender else None


def budget_line(timing: Timing, policy: Policy) -> str | None:
    """Printed whenever the budget cut anything, with or without --timing: never silent."""
    if not timing.budget_skipped:
        return None
    return (
        f"budget: {timing.budget_skipped} candidate(s) skipped the model and used the built-in recommender"
        f" (limits per pass: {policy.max_llm_calls} calls, {policy.max_llm_tokens} tokens;"
        f" raise with --max-llm-calls / --max-llm-tokens or the policy file)"
    )


@dataclass
class Proposal:
    recommendation: recommend.Recommendation
    notes: list[str]
    seconds: float = 0.0
    kind: str = "builtin"
    budget_skipped: bool = False


def propose(
    c, neighbors, policy: Policy, recommender: tuple[str, ...], timeout: float, budget: Budget | None = None
) -> Proposal:
    """The recommender's side of a decision. Pure of tracker state, so it may run on a worker thread."""
    started = time.monotonic()
    kind, skipped = "builtin", False
    try:
        top = max((n.similarity for n in neighbors), default=0.0)
        in_band = top >= policy.llm_min_similarity
        if recommender and budget is not None and in_band and not budget.reserve():
            rec = recommend.builtin(c, neighbors, policy)
            notes = ["llm_budget_exhausted"]
            skipped = True  # in the band: it would have reached the model
        elif recommender:
            kind = "external"
            outcome = recommend.external(recommender, c, neighbors, policy, timeout)
            rec, notes = outcome.recommendation, list(outcome.notes)
            if budget is not None:
                budget.settle(rec, reserved=in_band)
        else:
            rec, notes = recommend.builtin(c, neighbors, policy), []
    except Exception as error:  # one candidate's failure is a create for that candidate only
        rec = recommend.fallback(c["candidate_id"], policy)
        notes = [f"recommender_failed: {type(error).__name__}"]
    return Proposal(rec, notes, time.monotonic() - started, kind, skipped)


class _Lazy:
    def __init__(self, fn):
        self._fn, self._done, self._value = fn, False, None

    def result(self):
        if not self._done:
            self._value, self._done = self._fn(), True
        return self._value

    def cancel(self) -> None:
        pass


class Proposer:
    """Recommend in parallel (bounded), hand results back in submission order. It never touches the tracker.

    Only an external recommender is a subprocess worth overlapping; the built-in and ``jobs == 1`` run
    lazily on the calling thread, exactly as before.
    """

    def __init__(
        self,
        policy: Policy,
        recommender: tuple[str, ...],
        timeout: float,
        jobs: int,
        budget: Budget | None = None,
    ):
        self.policy, self.recommender, self.timeout, self.budget = policy, recommender, timeout, budget
        self.jobs = clamp_jobs(jobs)
        self._pool = ThreadPoolExecutor(self.jobs) if recommender and self.jobs > 1 else None

    def submit(self, c, neighbors):
        args = (c, neighbors, self.policy, self.recommender, self.timeout, self.budget)
        if self._pool is None:
            return _Lazy(lambda: propose(*args))
        return self._pool.submit(propose, *args)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


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
    timing: Timing = field(default_factory=Timing)


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


def candidate_view(c: dict) -> dict:
    """The candidate as the log keeps it: enough to re-judge the decision offline (body capped)."""
    view = {k: v for k, v in c.items() if k != "body"}
    view["body"] = c["body"][:LOG_BODY]
    return view


def decide(
    c: dict,
    neighbors: list[match.Neighbor],
    degraded: str,
    snap,
    policy: Policy,
    recommender: tuple[str, ...],
    timeout: float,
    proposal: Proposal | None = None,
) -> tuple[recommend.Recommendation, executor.Plan, dict]:
    """Recommend, then let the executor decide. Returns (recommendation, plan, the log fields).

    Shared by the queue runner and `scribe replay`. It never writes anything. ``proposal`` is a recommendation
    already made (in parallel); without it the recommender runs here.
    """
    notes = [degraded] if degraded else []
    proposal = proposal or propose(c, neighbors, policy, recommender, timeout)
    rec, notes = proposal.recommendation, notes + proposal.notes
    plan = executor.plan(c, rec, neighbors, snap, policy, notes)
    skipped = any("model not called" in e for e in rec.evidence)
    meta = rec.metadata or {}
    fields = {
        "candidate": candidate_view(c),
        "recommendation": rec.to_dict(),
        "recommender": {
            "kind": proposal.kind,
            "model_called": proposal.kind == "external" and not skipped,
            **({"budget_skipped": True} if proposal.budget_skipped else {}),
        },
        "llm_input_tokens": meta.get("llm_input_tokens"),
        "llm_output_tokens": meta.get("llm_output_tokens"),
        "llm_ms": meta.get("llm_ms"),
        "thresholds": {
            "min_similarity": policy.min_similarity,
            "min_confidence": policy.min_confidence,
            "llm_min_similarity": policy.llm_min_similarity,
            "placement_min_similarity": policy.placement_min_similarity,
        },
        "neighbors": [n.to_dict() for n in neighbors],
        "degraded": degraded or None,
        "guards": plan.guards,
        "target_facts": target_facts(snap, rec.target_id),
        "executor_plan": {
            "kind": plan.kind,
            "guards": plan.guards,
            "flags": plan.flags,
            "route": plan.route,
            "action": plan.action,
            "target_id": plan.target_id,
            "parent": plan.parent,
            "placement_rule": plan.placement_rule,
            "unplaced": plan.unplaced,
            "downgraded": plan.downgraded,
            "reasons": plan.reasons,
            "adjustments": plan.adjustments,
            "steps": plan.steps(),
        },
    }
    return rec, plan, fields


def target_facts(snap, target_id: str | None) -> dict | None:
    """What the guards saw on the recommended target, so `scribe tune` re-judges the row the same way."""
    if not target_id or target_id not in snap:
        return None
    s = snap[target_id]
    return {
        "id": s.id,
        "status": s.status,
        "assignee": s.assignee,
        "issue_type": s.issue_type,
        "labels": sorted(s.labels),
        "title": s.title,
        "notes": s.notes[:2000],
        "description": s.description[:2000],
    }


@dataclass
class Prepared:
    """A candidate's neighbors (one batched match) and its recommendation (maybe still running)."""

    neighbors: list[match.Neighbor]
    degraded: str
    proposal: object  # a Future / _Lazy yielding a Proposal


def _reconciled_route(c: dict, marker_kind: str, bead: str, policy: Policy) -> dict | None:
    """A create that landed before its receipt did still owes its owner route (the ref is the bead)."""
    if marker_kind == "candidate" and cand.kind_of(c) in ("question_owner", "decision"):
        return executor.route_for(c, bead, policy)
    return None


def process(
    queue: st.Queue,
    sub: st.Submission,
    policy: Policy,
    *,
    live: bool,
    recommender: tuple[str, ...],
    timeout: float,
    summary: Summary,
    prepared: Prepared | None = None,
) -> bool:
    """Returns True when this call may have created a bead (later candidates' neighbors may be stale)."""
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
                    route=_reconciled_route(c, kind, bead, policy),
                ),
            )
            queue.release_lease(cid)
            summary.reconciled += 1
        else:
            summary.shadowed += 1
        queue.log(entry)
        return False

    if prepared is None:
        neighbors, degraded = match.fetch(c, policy.match_command, timeout)
        proposal = None
    else:
        neighbors, degraded, proposal = prepared.neighbors, prepared.degraded, prepared.proposal.result()
    rec, plan, fields = decide(c, neighbors, degraded, snap, policy, recommender, timeout, proposal)
    summary.timing.note_decision(fields)
    if proposal is not None:
        summary.timing.recommender_seconds += proposal.seconds
    entry.update(fields)
    if not live:
        entry["outcome"] = "shadow"
        summary.shadowed += 1
        queue.log(entry)
        return False

    if any(executor.writes_metadata(i.argv) for i in plan.items):  # defence in depth: never written
        entry.update(outcome="failed", error="refused: a plan would write bead metadata")
        summary.failed += 1
        queue.log(entry)
        return False
    attempts = queue.take_lease(cid)
    for item in plan.items:
        engine.run_item(item, timeout)
        if item.outcome != "ok":
            entry.update(outcome="failed", error=item.message[:300], attempts=attempts)
            summary.failed += 1
            summary.details.append(f"{cid}: {item.message[:200]}")
            queue.log(entry)
            return (
                plan.action == "create"
            )  # the lease stays: the candidate remains queued and the next run reconciles first
    item = plan.items[0] if plan.items else None
    bead = (item.created_id or item.marker_on) if item else (plan.target_id or "")
    reconciled = bool(item and item.message.startswith("reconciled"))
    result = "dropped" if plan.action == "drop" else ("reconciled" if reconciled else "applied")
    entry.update(outcome=result, bead_id=bead or None, attempts=attempts)
    undo = f"close {bead} embeadify undo: scribe create {cid}" if plan.action == "create" and bead else None
    route = None
    if plan.route and plan.action == "create" and bead:
        route = {**plan.route, "hub_brief": {**plan.route["hub_brief"], "ref": bead}}
        entry["route"] = route
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
            parent=plan.parent,
            placement_rule=plan.placement_rule,
            unplaced=plan.unplaced or None,
            guards=plan.guards or None,
            flags=plan.flags or None,
            route=route,
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
    return plan.action == "create"


def _prepare(subs, policy, proposer: Proposer, timeout: float, summary: Summary) -> dict[str, Prepared]:
    """ONE batched match for ``subs``, then start their recommendations (in parallel when allowed)."""
    found = _prepare_matches(subs, policy, timeout, summary)
    out = {}
    for s in subs:
        neighbors, degraded = found[s.candidate["candidate_id"]]
        out[s.candidate["candidate_id"]] = Prepared(
            neighbors, degraded, proposer.submit(s.candidate, neighbors)
        )
    return out


def _rematch(subs, policy, proposer: Proposer, timeout: float, summary: Summary, old) -> None:
    """Re-match the stale candidates in ONE call. A recommendation is only redone if its neighbors changed."""
    fresh = _prepare_matches(subs, policy, timeout, summary)
    for s in subs:
        cid = s.candidate["candidate_id"]
        neighbors, degraded = fresh[cid]
        before = old[cid]
        if degraded == before.degraded and [n.to_dict() for n in neighbors] == [
            n.to_dict() for n in before.neighbors
        ]:
            continue
        before.proposal.cancel()
        old[cid] = Prepared(neighbors, degraded, proposer.submit(s.candidate, neighbors))


def with_owner_summaries(neighbors: list[match.Neighbor], snap) -> list[match.Neighbor]:
    """Attach each neighbor's bead metadata `owner_summary` (read-only context for the recommender)."""
    out = []
    for n in neighbors:
        summary = snap[n.issue_id].owner_summary if snap is not None and n.issue_id in snap else ""
        out.append(replace(n, owner_summary=summary) if summary else n)
    return out


def _context_snapshot():
    try:
        return snapshot.take()
    except bd.BdError:
        return None  # context only: a decision never depends on it


def _prepare_matches(subs, policy, timeout, summary):
    started = time.monotonic()
    found, calls = match.fetch_many([s.candidate for s in subs], policy.match_command, timeout)
    summary.timing.match_seconds += time.monotonic() - started
    summary.timing.match_calls += calls
    snap = _context_snapshot()
    return {cid: (with_owner_summaries(ns, snap), degraded) for cid, (ns, degraded) in found.items()}


def run_once(
    queue: st.Queue,
    policy: Policy,
    *,
    live: bool,
    recommender: tuple[str, ...] = (),
    timeout: float = 120.0,
    limit: int | None = None,
    jobs: int = 1,
    budget: Budget | None = None,
) -> Summary:
    """One pass. Retrieval is batched (one `embead match`) and recommendations may overlap (``jobs``), but
    validation, execution and log appends stay strictly sequential in queue order: ONE writer.

    In live mode a create makes every later candidate's neighbors stale (the new bead could be one of
    them): they are re-matched together in one call before the next candidate is processed.
    """
    summary = Summary()
    summary.timing.recommender_jobs = clamp_jobs(jobs)
    began = time.monotonic()
    todo = []
    for sub in queue.submissions():
        summary.seen += 1
        if sub.error:
            summary.invalid += 1
            summary.details.append(f"{sub.path.name}: invalid submission ({sub.error})")
            continue
        if queue.decision(sub.receipt_id):
            continue
        if limit is not None and len(todo) >= limit:
            break
        todo.append(sub)
    if todo:
        with Proposer(
            policy, recommender, timeout, jobs, budget or budget_for(policy, recommender)
        ) as proposer:
            prepared = _prepare(todo, policy, proposer, timeout, summary)
            stale: set[str] = set()
            for i, sub in enumerate(todo):
                if stale:
                    later = [s for s in todo[i:] if s.candidate["candidate_id"] in stale]
                    if later:
                        _rematch(later, policy, proposer, timeout, summary, prepared)
                    stale.clear()
                created = process(
                    queue,
                    sub,
                    policy,
                    live=live,
                    recommender=recommender,
                    timeout=timeout,
                    summary=summary,
                    prepared=prepared[sub.candidate["candidate_id"]],
                )
                if live and created:
                    stale.update(s.candidate["candidate_id"] for s in todo[i + 1 :])
    summary.timing.wall_seconds = time.monotonic() - began
    return summary


def run_loop(queue, policy, *, live, recommender, timeout, interval: float, limit=None, jobs=1) -> Summary:
    total = Summary()
    try:
        while True:
            total = run_once(
                queue, policy, live=live, recommender=recommender, timeout=timeout, limit=limit, jobs=jobs
            )
            time.sleep(interval)
    except KeyboardInterrupt:
        return total
