"""The ONLY module that knows the shape of `embead match`. Adjust parsing here and nowhere else.

Command (ONE call per batch: loading the model and the vector cache dominates, so every candidate of a
pass goes in one file and results are mapped back by ``candidate_id``)::

    embead match --candidates-file FILE.jsonl --json

``FILE.jsonl`` holds one object per line: ``{"candidate_id", "title", "body"}``.

Report (emBEADings schema v1, ``report_type`` = "match"); fields read here, everything else ignored::

    schema_version: 1
    report_type: "match"
    candidates[]:
      candidate_id: str
      status: "matches" | "no-match"
      neighbors[] (sorted by similarity desc, then issue_id):
        issue_id: str            status: str            issue_type: str      priority: int
        title: str               similarity: float 0..1 rank: int            is_closed: bool
        parent_id: str | null    parent_status: null | "live" | "closed" | "missing"
        resolution_evidence: null | {"kind": "close_reason", "text": str}

The report does not echo the candidate's title or body. Neighbor text is tracker content and therefore
untrusted: it is stored and shown, never executed. A missing, failing, or unparsable ``embead`` is not an
error for the scribe: the result is no neighbors plus a ``degraded`` reason, and the candidate is created.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .. import bd, sanitize

MAX_REPORT_BYTES = 4_000_000
MAX_NEIGHBORS = 25
PAYLOAD_TITLE = 500
PAYLOAD_BODY = 8000


@dataclass(frozen=True)
class Neighbor:
    issue_id: str
    status: str = ""
    issue_type: str = ""
    priority: int | None = None
    title: str = ""
    similarity: float = 0.0
    parent_id: str | None = None
    parent_status: str | None = None
    is_closed: bool = False
    resolution_evidence: str = ""  # the `text` of the close reason, empty when there is none
    owner_summary: str = ""  # bead metadata `owner_summary` (read-only context from the snapshot, 400 chars)

    def to_dict(self) -> dict:
        d = asdict(self)
        if not d["owner_summary"]:
            del d["owner_summary"]  # most neighbors have none: keep logs and payloads small
        return d


def _neighbor(raw) -> Neighbor | None:
    if not isinstance(raw, dict) or not sanitize.is_id(raw.get("issue_id")):
        return None
    sim = raw.get("similarity")
    if isinstance(sim, bool) or not isinstance(sim, (int, float)) or not 0 <= sim <= 1:
        return None
    evidence = raw.get("resolution_evidence")
    text = evidence.get("text") if isinstance(evidence, dict) else None
    status = str(raw.get("status") or "")
    prio = raw.get("priority")
    parent = raw.get("parent_id")
    return Neighbor(
        issue_id=raw["issue_id"],
        status=status[:40],
        issue_type=str(raw.get("issue_type") or "")[:40],
        priority=prio if isinstance(prio, int) and not isinstance(prio, bool) else None,
        title=sanitize.clean_line(str(raw.get("title") or ""), 200),
        similarity=float(sim),
        parent_id=parent if sanitize.is_id(parent) else None,
        parent_status=raw.get("parent_status")
        if raw.get("parent_status") in ("live", "closed", "missing")
        else None,
        is_closed=bool(raw.get("is_closed", status == "closed")),
        resolution_evidence=sanitize.clean_line(text, 500) if isinstance(text, str) else "",
    )


def _entries(report) -> list[dict]:
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != 1
        or report.get("report_type") != "match"
    ):
        raise ValueError("not a schema-v1 match report")
    return [c for c in report.get("candidates") or [] if isinstance(c, dict)]


def _neighbors_of(entry: dict, cap: int) -> list[Neighbor]:
    neighbors = [n for n in map(_neighbor, entry.get("neighbors") or []) if n is not None]
    neighbors.sort(key=lambda n: (-n.similarity, n.issue_id))
    return neighbors[:cap]


def parse_report(report, candidate_id: str, cap: int = MAX_NEIGHBORS) -> list[Neighbor]:
    """Neighbors for one candidate from a decoded report. Raises ValueError on a wrong shape."""
    mine = [c for c in _entries(report) if c.get("candidate_id") == candidate_id]
    if not mine:
        raise ValueError("report has no entry for this candidate")
    return _neighbors_of(mine[0], cap)


def _line(candidate: dict) -> dict:
    return {
        "candidate_id": candidate["candidate_id"],
        "title": sanitize.clean_line(candidate["title"], PAYLOAD_TITLE),
        "body": sanitize.clean_block(candidate["body"], PAYLOAD_BODY)[0],
    }


def _invoke(lines: list[dict], command: tuple[str, ...], timeout: float, limit: int | None):
    """One `embead match` call. Returns (decoded report, "") or (None, degraded reason)."""
    with tempfile.TemporaryDirectory(prefix="embeadify-scribe-") as tmp:
        path = Path(tmp) / "candidates.jsonl"
        path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
        argv = [*command, "--candidates-file", str(path), "--json"]
        if limit is not None:
            argv += ["--limit", str(limit)]
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return None, "embead_missing"
        except (OSError, subprocess.TimeoutExpired) as error:
            return None, f"embead_failed: {type(error).__name__}"
    if done.returncode != 0:
        return None, "embead_failed: " + bd.redact(
            done.stderr.strip().splitlines()[0] if done.stderr.strip() else f"exit {done.returncode}"
        )[:200]
    if len(done.stdout) > MAX_REPORT_BYTES:
        return None, "embead_bad_output: report too large"
    try:
        return json.loads(done.stdout), ""
    except ValueError as error:
        return None, f"embead_bad_output: {error}"


def fetch(
    candidate: dict,
    command: tuple[str, ...],
    timeout: float = 120.0,
    limit: int | None = None,
) -> tuple[list[Neighbor], str]:
    """(neighbors, degraded reason). The reason is empty when the matcher ran and its report parsed.

    ``limit`` (replay only) asks the matcher for that many neighbors with `--limit N` and keeps that many.
    """
    report, reason = _invoke([_line(candidate)], command, timeout, limit)
    if report is None:
        return [], reason
    try:
        return parse_report(report, candidate["candidate_id"], limit or MAX_NEIGHBORS), ""
    except ValueError as error:
        return [], f"embead_bad_output: {error}"


def fetch_many(
    candidates: list[dict],
    command: tuple[str, ...],
    timeout: float = 120.0,
    limit: int | None = None,
) -> tuple[dict[str, tuple[list[Neighbor], str]], int]:
    """ONE `embead match` call for the whole batch: ({candidate_id: (neighbors, degraded)}, calls made).

    Results are mapped back by ``candidate_id`` (order in the report is irrelevant). A candidate the report
    omits is degraded on its own. If the batch call itself fails (missing, non-zero, timeout, unparsable,
    wrong shape) every candidate falls back to the per-candidate path, so one bad batch costs speed, never
    correctness. ``limit`` applies per candidate.
    """
    unique: dict[str, dict] = {}
    for c in candidates:
        unique.setdefault(c["candidate_id"], c)
    if not unique:
        return {}, 0
    report, reason = _invoke([_line(c) for c in unique.values()], command, timeout, limit)
    entries = None
    if report is not None:
        try:
            entries = _entries(report)
        except ValueError as error:
            reason = f"embead_bad_output: {error}"
    calls = 1
    out: dict[str, tuple[list[Neighbor], str]] = {}
    if entries is None:
        if len(unique) == 1:
            return {cid: ([], reason) for cid in unique}, calls
        for cid, c in unique.items():  # degraded batch: the per-candidate path, as before
            out[cid] = fetch(c, command, timeout, limit)
            calls += 1
        return out, calls
    by_id = {}
    for e in entries:
        by_id.setdefault(e.get("candidate_id"), e)
    for cid in unique:
        if cid in by_id:
            out[cid] = (_neighbors_of(by_id[cid], limit or MAX_NEIGHBORS), "")
        else:
            out[cid] = ([], "embead_bad_output: report has no entry for this candidate")
    return out, calls
