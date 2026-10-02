"""`scribe metrics`: how the shadow scribe is doing, from the log and the labels. Pure functions, no I/O.

Every rate is a (numerator, denominator) pair so a number is never shown without its sample size.
"""

from __future__ import annotations

from collections import Counter

from . import labels as lb

ACTIONS = ("create", "dup", "fold", "drop")


def rate(num: int, den: int) -> dict:
    return {"n": num, "of": den, "rate": (num / den) if den else None}


def fmt(r: dict, what: str = "") -> str:
    if not r["of"]:
        return f"n/a (0 {what or 'rows'})"
    return f"{r['rate'] * 100:.1f}% ({r['n']}/{r['of']})"


def block(rows: list[dict], labels: dict) -> dict:
    """Metrics for one group of log rows (one row per candidate and policy version)."""
    mix = Counter(lb.final_action(r) for r in rows)
    creates = [r for r in rows if lb.final_action(r) == "create"]
    unplaced = [r for r in creates if (r.get("executor_plan") or {}).get("unplaced")]
    external = [r for r in rows if (r.get("recommender") or {}).get("kind") == "external"]
    called = [r for r in external if r["recommender"].get("model_called")]
    with_actual = [r for r in rows if r.get("actual")]
    agree = [r for r in with_actual if _agrees(r)]
    placeable = [
        r for r in with_actual if lb.final_action(r) == "create" and r["actual"].get("parent_source")
    ]
    place_agree = [r for r in placeable if (r["executor_plan"].get("parent") == r["actual"].get("parent"))]

    judged = [(r, labels[lb.key(r)]) for r in rows if lb.key(r) in labels]
    scored = [(r, ln) for r, ln in judged if ln["verdict"] != "unclear"]
    proposals = [(r, ln) for r, ln in scored if lb.final_action(r) in lb.SUPPRESS]
    wrong = [r for r, ln in proposals if ln["verdict"] == "wrongly_dup"]
    labeled_creates = [(r, ln) for r, ln in scored if lb.final_action(r) == "create"]
    missed = [r for r, ln in labeled_creates if ln["verdict"] == "should_have_been_dup"]
    placement_judged = [ln for r, ln in labeled_creates if ln["verdict"] in ("correct", "bad_placement")]
    placement_ok = [ln for ln in placement_judged if ln["verdict"] == "correct"]
    forced = Counter(g for r in rows for g in (r.get("guards") or []))
    return {
        "guard_forced_create": dict(sorted(forced.items())),
        "llm_usage": usage(rows),
        "candidates": len(rows),
        "action_mix": {a: mix.get(a, 0) for a in ACTIONS if mix.get(a, 0)}
        | {k: v for k, v in mix.items() if k not in ACTIONS},
        "unplaced_rate": rate(len(unplaced), len(creates)),
        "llm_call_rate": rate(len(called), len(external)),
        "agreement_with_actual": rate(len(agree), len(with_actual)),
        "placement_agreement_with_actual": rate(len(place_agree), len(placeable)),
        "label_coverage": rate(len(judged), len(rows)),
        "labeled_scored": len(scored),
        "unclear": len(judged) - len(scored),
        "suppress_precision": rate(len(proposals) - len(wrong), len(proposals)),
        "false_suppressions": {
            "count": len(wrong),
            "of_labeled_proposals": len(proposals),
            "candidates": sorted(r["candidate_id"] for r in wrong),
        },
        "missed_duplicate_rate": rate(len(missed), len(labeled_creates)),
        "placement_accuracy": rate(len(placement_ok), len(placement_judged)),
    }


def usage(rows: list[dict]) -> dict:
    """Model spend from the log: totals and per-call averages over the calls that reported tokens."""
    called = [r for r in rows if (r.get("recommender") or {}).get("model_called")]
    seen = [
        r for r in called if r.get("llm_input_tokens") is not None or r.get("llm_output_tokens") is not None
    ]
    tin = sum(r.get("llm_input_tokens") or 0 for r in seen)
    tout = sum(r.get("llm_output_tokens") or 0 for r in seen)
    ms = sum(r.get("llm_ms") or 0 for r in called)
    n = len(seen)
    return {
        "calls": len(called),
        "calls_reporting_tokens": n,
        "input_tokens": tin,
        "output_tokens": tout,
        "avg_input_tokens": (tin / n) if n else None,
        "avg_output_tokens": (tout / n) if n else None,
        "avg_ms": (ms / len(called)) if called else None,
        "budget_skipped": sum(1 for r in rows if (r.get("recommender") or {}).get("budget_skipped")),
    }


def _agrees(row: dict) -> bool:
    """Scribe vs filer: the filer either created the bead or (replayed with --include-duplicates) closed it
    as a duplicate. Any suppressing action agrees with a duplicate close; only create agrees with a create."""
    mine = lb.final_action(row)
    filer = row["actual"].get("filer_action", "create")
    return (mine in lb.SUPPRESS) if filer == "dup" else mine == "create"


def compute(rows: list[dict], labels: dict) -> dict:
    by_version: dict[str, list[dict]] = {}
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_version.setdefault(r.get("policy_version", "") or "(none)", []).append(r)
        by_day.setdefault(lb.day_of(r) or "(none)", []).append(r)
    return {
        "overall": block(rows, labels),
        "by_version": {k: block(v, labels) for k, v in sorted(by_version.items())},
        "by_day": {k: block(v, labels) for k, v in sorted(by_day.items())},
    }


def render_block(name: str, b: dict) -> list[str]:
    fs = b["false_suppressions"]
    lines = [f"== {name}: {b['candidates']} candidates =="]
    lines.append(
        "  action mix:        " + (", ".join(f"{k} {v}" for k, v in b["action_mix"].items()) or "none")
    )
    forced = b["guard_forced_create"]
    lines.append(
        "  guard_forced_create: "
        + (
            ", ".join(f"{k} {v}" for k, v in forced.items()) + f" (total {sum(forced.values())})"
            if forced
            else "none"
        )
    )
    lines.append(f"  unplaced creates:  {fmt(b['unplaced_rate'], 'creates')}")
    lines.append(f"  LLM called:        {fmt(b['llm_call_rate'], 'external-recommender rows')}")
    u = b["llm_usage"]
    if u["calls"] or u["budget_skipped"]:
        avg = (
            f"avg {u['avg_input_tokens']:.0f} in / {u['avg_output_tokens']:.0f} out per call"
            if u["calls_reporting_tokens"]
            else "no token usage reported"
        )
        ms = f", avg {u['avg_ms']:.0f} ms" if u["avg_ms"] is not None else ""
        lines.append(
            f"  LLM spend:         {u['calls']} calls, tokens in {u['input_tokens']} out {u['output_tokens']}"
            f" ({avg}{ms}); skipped for budget {u['budget_skipped']}"
        )
    lines.append(f"  agrees w/ filer:   {fmt(b['agreement_with_actual'], 'rows with actual')}")
    lines.append(f"  placement == filer {fmt(b['placement_agreement_with_actual'], 'creates with actual')}")
    lines.append(
        f"  LABELS:            {fmt(b['label_coverage'], 'rows')} labeled"
        f" ({b['labeled_scored']} scored, {b['unclear']} unclear)"
    )
    if fs["count"]:
        lines.append(
            f"  !!! FALSE SUPPRESSION: {fs['count']} of {fs['of_labeled_proposals']} labeled dup/fold/drop"
            f" proposals were wrongly_dup (real work would have been lost): {', '.join(fs['candidates'])}"
        )
    else:
        lines.append(
            f"  false suppression: 0 (of {fs['of_labeled_proposals']} labeled dup/fold/drop proposals)"
        )
    lines.append(f"  suppress precision {fmt(b['suppress_precision'], 'labeled proposals')}")
    lines.append(f"  missed duplicates: {fmt(b['missed_duplicate_rate'], 'labeled creates')}")
    lines.append(f"  placement accuracy {fmt(b['placement_accuracy'], 'labeled placements')}")
    return lines


def render(data: dict, by_version: bool) -> str:
    lines = render_block("ALL", data["overall"])
    groups = data["by_version"] if by_version else data["by_day"]
    for name, b in groups.items():
        lines += [""] + render_block(("policy " if by_version else "day ") + name, b)
    return "\n".join(lines)
