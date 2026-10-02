"""Adjudicator labels: an append-only `labels.jsonl` next to the log. Rows are immutable; a relabel is a new
row and the LAST row for a (candidate_id, policy_version) wins. Nothing here touches the tracker."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from .. import sanitize
from . import store as st

VERDICTS = {
    "correct": "the decision was right (a justified create, or a dup/fold/drop of a true duplicate)",
    "should_have_been_dup": "scribe CREATED, but a live bead already covers it (needs --of BEAD_ID)",
    "wrongly_dup": "scribe proposed dup/fold/drop, but the work is distinct; real work would have been lost",
    "bad_placement": "scribe created it under the wrong parent or unplaced (needs --better PARENT_ID)",
    "unclear": "the adjudicator cannot tell; excluded from every metric and from tune",
}
SUPPRESS = ("dup", "fold", "drop")
MAX_NOTE = 1000


class LabelError(ValueError):
    pass


def labels_path(queue: st.Queue) -> Path:
    return queue.root / "labels.jsonl"


def key(row: dict) -> tuple[str, str]:
    return (row.get("candidate_id", ""), row.get("policy_version", ""))


def day_of(row: dict) -> str:
    return str(row.get("ts", ""))[:10]


def read_labels(queue: st.Queue) -> list[dict]:
    try:
        lines = labels_path(queue).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("verdict") in VERDICTS and row.get("candidate_id"):
            out.append(row)
    return out


def current_labels(queue: st.Queue) -> dict[tuple[str, str], dict]:
    """The label in force for each (candidate_id, policy_version): the last row wins."""
    return {key(r): r for r in read_labels(queue)}


def latest_rows(queue: st.Queue) -> dict[tuple[str, str], dict]:
    """The newest shadow/live log row per (candidate_id, policy_version) that carries a decision."""
    out: dict[tuple[str, str], dict] = {}
    for row in queue.read_log():
        if row.get("executor_plan") and row.get("candidate_id") and row.get("outcome") != "reconciled":
            out[key(row)] = row
    return out


def final_action(row: dict) -> str:
    return (row.get("executor_plan") or {}).get("action", "")


def validate(row: dict, verdict: str, of: str | None, better: str | None) -> None:
    """Raise LabelError when the verdict does not fit the decision it judges."""
    if verdict not in VERDICTS:
        raise LabelError(f"verdict must be one of {', '.join(VERDICTS)}")
    action = final_action(row)
    if verdict == "should_have_been_dup":
        if not of:
            raise LabelError("should_have_been_dup needs --of BEAD_ID (the live bead that covers it)")
        if action != "create":
            raise LabelError(f"should_have_been_dup judges a create; this decision was {action!r}")
    if verdict == "bad_placement":
        if not better:
            raise LabelError("bad_placement needs --better PARENT_ID (where it belongs)")
        if action != "create":
            raise LabelError(f"bad_placement judges a create; this decision was {action!r}")
    if verdict == "wrongly_dup" and action not in SUPPRESS:
        raise LabelError(f"wrongly_dup judges a dup/fold/drop; this decision was {action!r}")
    if of and verdict != "should_have_been_dup":
        raise LabelError("--of is only for should_have_been_dup")
    if better and verdict != "bad_placement":
        raise LabelError("--better is only for bad_placement")
    for flag, value in (("--of", of), ("--better", better)):
        if value is not None and not sanitize.is_id(value):
            raise LabelError(f"{flag} must be a bead id")


def check_ids(row: dict, of: str | None, better: str | None, raw_items: list[dict]) -> None:
    """--of / --better must exist in the tracker (and, for a replay, predate the bead being judged)."""
    from .replay import parse_ts  # local: replay imports the runner, which imports this package

    by_id = {str(i["id"]): i for i in raw_items}
    cutoff = parse_ts((row.get("actual") or {}).get("created_at"))
    for flag, value in (("--of", of), ("--better", better)):
        if value is None:
            continue
        item = by_id.get(value)
        if item is None:
            raise LabelError(f"{flag} {value}: no such bead in the tracker")
        made = parse_ts(item.get("created_at"))
        if cutoff and made and made >= cutoff:
            raise LabelError(
                f"{flag} {value}: created after the bead being judged, so the scribe could not know it"
            )
        if flag == "--better" and item.get("status") == "closed":
            raise LabelError(f"--better {value}: a closed bead cannot be a parent")
    if of and of == (row.get("actual") or {}).get("bead_id"):
        raise LabelError("--of is the bead being judged itself")


def append(queue: st.Queue, row: dict, verdict: str, *, of=None, better=None, note="", by="") -> dict:
    entry = {
        "schema_version": 1,
        "candidate_id": row["candidate_id"],
        "policy_version": row.get("policy_version", ""),
        "decision_ts": row.get("ts"),
        "decision_action": final_action(row),
        "verdict": verdict,
        "of": of,
        "better": better,
        "note": sanitize.clean_line(note or "", MAX_NOTE),
        "by": sanitize.clean_line(
            by or os.environ.get("USER") or os.environ.get("USERNAME") or "unknown", 64
        ),
        "ts": st.now(),
    }
    queue.ensure()
    # One write call per row, append mode: rows are never rewritten or removed.
    with open(labels_path(queue), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def since_filter(rows, since: str | None):
    if not since:
        return rows
    try:
        datetime.strptime(since, "%Y-%m-%d")
    except ValueError:
        raise LabelError(f"--since must be YYYY-MM-DD, got {since!r}") from None
    return [r for r in rows if day_of(r) >= since]
