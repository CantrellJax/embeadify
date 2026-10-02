"""`scribe tune`: replay the executor offline over a grid of thresholds, using only what the log kept.

No model call, no `embead`, no tracker. The logged recommendation and neighbors are re-judged by the real
executor with each setting. It prints a suggestion and a policy snippet; it never edits a policy file.

What a labeled row says about the right action ("truth"):
  suppress: verdict should_have_been_dup, or `correct` on a logged dup/fold/drop.
  create:   verdict wrongly_dup, bad_placement, or `correct` on a logged create.
  (unclear rows are left out.)
A setting's FALSE SUPPRESSIONS are rows whose truth is create but the setting suppresses; its MISSED
DUPLICATES are rows whose truth is suppress but the setting creates.
"""

from __future__ import annotations

import itertools
from dataclasses import replace

from .. import snapshot
from . import candidate as cand
from . import executor
from . import labels as lb
from .match import Neighbor
from .policy import Policy
from .recommend import Recommendation, fallback

GRID = {
    "min_similarity": (0.75, 0.80, 0.85, 0.90, 0.95),
    "min_confidence": (0.60, 0.70, 0.80, 0.90, 0.95),
    "llm_min_similarity": (0.65, 0.75, 0.80, 0.85),
    "placement_min_similarity": (0.50, 0.60, 0.70, 0.80),
}
DEFAULT_MIN_LABELS = 30


def truth(row: dict, verdict: str) -> str | None:
    if verdict == "unclear":
        return None
    suppressed = lb.final_action(row) in lb.SUPPRESS
    if verdict == "should_have_been_dup":
        return "suppress"
    if verdict in ("wrongly_dup", "bad_placement"):
        return "create"
    return "suppress" if suppressed else "create"


def _neighbors(row: dict) -> list[Neighbor]:
    return [
        Neighbor(
            **{k: v for k, v in n.items() if k in Neighbor.__dataclass_fields__},
        )
        for n in row.get("neighbors") or []
    ]


def _snapshot(neighbors: list[Neighbor]) -> snapshot.Snapshot:
    """The tracker as far as the log shows it: each neighbor, and each neighbor's parent when live/closed."""
    snap: snapshot.Snapshot = {}

    def put(ident, status, parent=None):
        snap.setdefault(ident, snapshot.State(ident, status, parent, None, set()))

    for n in neighbors:
        put(n.issue_id, "closed" if n.is_closed else (n.status or "open"), n.parent_id)
    for n in neighbors:
        if n.parent_id and n.parent_status in ("live", "closed"):
            put(n.parent_id, "open" if n.parent_status == "live" else "closed")
    return snap


def decide(row: dict, policy: Policy) -> tuple[executor.Plan, bool]:
    """(plan, unknowable): re-judge one logged decision. `unknowable` marks a lowered LLM floor that would
    now call a model that never ran on this row; its logged answer is kept (a create)."""
    c = cand.validate({**row["candidate"], "body": row["candidate"].get("body", "")})
    neighbors = _neighbors(row)
    rec = Recommendation(
        **{**row["recommendation"], "evidence": tuple(row["recommendation"].get("evidence", []))}
    )
    unknowable = False
    kind = (row.get("recommender") or {}).get("kind")
    if kind == "external":
        top = max((n.similarity for n in neighbors), default=0.0)
        if not neighbors or top < policy.llm_min_similarity:
            rec = fallback(c["candidate_id"], policy)  # the pre-filter would skip the model
        elif not row["recommender"].get("model_called"):
            unknowable = True
    return executor.plan(c, rec, neighbors, _snapshot(neighbors), policy), unknowable


def usable(rows: dict, labels: dict) -> list[tuple[dict, str, str]]:
    out = []
    for k, label in labels.items():
        row = rows.get(k)
        if row and row.get("candidate") and row.get("recommendation") is not None:
            t = truth(row, label["verdict"])
            if t:
                out.append((row, label, t))
    return out


def evaluate(items, base: Policy, setting: dict) -> dict:
    policy = replace(base, **setting)
    flips = fs = missed = unknown = placement_fixed = placement_changed = 0
    for row, label, t in items:
        plan, unknowable = decide(row, policy)
        unknown += unknowable
        was = lb.final_action(row)
        flips += plan.action != was
        suppressed = plan.action in lb.SUPPRESS
        fs += t == "create" and suppressed
        missed += t == "suppress" and not suppressed
        if (
            was == "create"
            and plan.action == "create"
            and plan.parent != (row["executor_plan"].get("parent"))
        ):
            placement_changed += 1
            placement_fixed += label["verdict"] == "bad_placement" and plan.parent == label.get("better")
    return {
        "setting": setting,
        "flips": flips,
        "false_suppressions": fs,
        "missed_duplicates": missed,
        "placement_changed": placement_changed,
        "placement_fixed": placement_fixed,
        "unknowable_rows": unknown,
    }


def grid_settings(base: Policy) -> list[dict]:
    names = list(GRID)
    out = []
    for values in itertools.product(*(GRID[n] for n in names)):
        out.append(dict(zip(names, values, strict=True)))
    return out


def current(base: Policy) -> dict:
    return {n: getattr(base, n) for n in GRID}


def run(rows: dict, labels: dict, base: Policy, min_labels: int = DEFAULT_MIN_LABELS) -> dict:
    items = usable(rows, labels)
    baseline = evaluate(items, base, current(base))
    faithful = sum(1 for row, _, _ in items if decide(row, base)[0].action == lb.final_action(row))
    results = [evaluate(items, base, s) for s in grid_settings(base)]
    results.sort(
        key=lambda r: (
            r["false_suppressions"],
            r["missed_duplicates"],
            r["flips"],
            round(sum(abs(v - getattr(base, k)) for k, v in r["setting"].items()), 9),
            tuple(r["setting"].values()),
        )
    )
    out = {
        "labeled_rows": len(items),
        "min_labels": min_labels,
        "baseline_setting": current(base),
        "baseline": baseline,
        "baseline_reproduces_logged_action": {"n": faithful, "of": len(items)},
        "results": results,
        "recommendation": None,
        "verdict": "",
    }
    if len(items) < min_labels:
        out["verdict"] = f"not enough labels ({len(items)} usable, need {min_labels})"
        return out
    best = results[0]
    if best["false_suppressions"] > 0:
        out["verdict"] = (
            "no setting in the grid has zero false suppression on the labeled set; change the recommender"
            " or the prompt, not the thresholds"
        )
    elif best["setting"] == current(base):
        out["verdict"] = "keep the current settings: no grid setting does better on the labeled set"
    else:
        out["verdict"] = "recommended"
        out["recommendation"] = best
    return out


def snippet(setting: dict) -> str:
    lines = ["[scribe]", "# bump policy_version when you adopt this: labels are bound to a policy_version"]
    lines += [f"{k} = {v}" for k, v in setting.items()]
    return "\n".join(lines)


def render(data: dict, top: int = 8) -> str:
    n, need = data["labeled_rows"], data["min_labels"]
    rep = data["baseline_reproduces_logged_action"]
    lines = [
        f"labeled rows usable for tuning: {n} (minimum {need})",
        f"baseline settings {data['baseline_setting']}: the executor reproduces the logged action on"
        f" {rep['n']}/{rep['of']} rows; false suppression {data['baseline']['false_suppressions']},"
        f" missed duplicates {data['baseline']['missed_duplicates']}",
        "",
        "flips = labeled decisions whose suppress/create outcome changes vs the log; unknowable = rows where",
        "a lowered llm_min_similarity would now call a model that never ran (logged answer kept).",
        "best settings (fewest false suppressions, then missed duplicates, then flips):",
    ]
    for r in data["results"][:top]:
        s = " ".join(f"{k}={v}" for k, v in r["setting"].items())
        lines.append(
            f"  {s}\n    flips {r['flips']}, false suppression {r['false_suppressions']},"
            f" missed duplicates {r['missed_duplicates']}, placement changed {r['placement_changed']}"
            f" (fixed {r['placement_fixed']}), unknowable {r['unknowable_rows']}"
        )
    lines.append("")
    lines.append(data["verdict"].upper() if data["verdict"].startswith("not enough") else data["verdict"])
    if data["recommendation"]:
        lines += [
            "",
            "policy snippet (NOT applied; open a PR with the metrics attached):",
            snippet(data["recommendation"]["setting"]),
        ]
    return "\n".join(lines)
