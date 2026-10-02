"""`embeadify plan`: turn an emBEADings report into a COMMENTED-OUT decisions template."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from . import snapshot

KINDS = ("orphans", "mentions", "triage")
DUP_KINDS = ("absorbed-by", "duplicate-of", "superseded-by")
HEADER = [
    "# embeadify decisions template (generated; nothing here is applied).",
    "# Every proposal is commented out. Uncomment ONLY the lines a human approves,",
    "# fill in every <placeholder>, then run: embeadify apply THIS_FILE   (dry run)",
    "#                                       embeadify apply THIS_FILE --apply",
    "# Titles below come from the report and may be private: do not publish this file.",
    "",
]


class PlanError(Exception):
    pass


def _one_line(text: object, limit: int = 100) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def load_rules(path: Path) -> list[dict]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise PlanError(f"cannot read rules file {path}: {error}") from error
    rules = data.get("filter", [])
    if not isinstance(rules, list):
        raise PlanError("rules file: `filter` must be an array of tables ([[filter]])")
    for rule in rules:
        if "name" not in rule:
            raise PlanError("rules file: every [[filter]] needs a `name`")
        if "title_regex" in rule:
            try:
                re.compile(rule["title_regex"])
            except re.error as error:
                raise PlanError(f"rules file: bad title_regex in {rule['name']!r}: {error}") from error
    return rules


def _orphans(report: dict) -> list[str]:
    out = [
        "# --- orphans: live issues whose parent is closed or missing ---",
        "# A parent is never guessed. Fill in the new parent (or `-` to clear it).",
    ]
    for e in report.get("dangling_parent", []):
        out.append(
            f"# parent {e['issue_id']} <fill-in>   # evidence: parent {e.get('parent_id')} is "
            f'{e.get("parent_status")}; {e.get("status")}; "{_one_line(e.get("title"))}"'
        )
    if len(out) == 2:
        out.append("# (no dangling parents in this report)")
    return out


def _mentions(report: dict) -> list[str]:
    out = [
        "# --- mentions: text claims with no typed link ---",
        "# For each claim pick ONE alternative (or none). Verify the claim first.",
    ]
    skipped = 0
    for c in report.get("claims", []):
        if c.get("typed_link"):
            skipped += 1
            continue
        a, b, kind = c["issue_id"], c["related_issue_id"], c["kind"]
        fields = ",".join(c.get("source_fields", []))
        t1, t2 = _one_line(c.get("issue_title")), _one_line(c.get("related_title"))
        ev = f'{a} says {kind} {b} (in {fields}); "{t1}" -> "{t2}"'
        if kind in DUP_KINDS:
            out.append(f"# evidence: {ev}")
            out.append(f"# dup {a} {b}")
            out.append(f"# parent {a} {b}")
        else:
            out.append(f"# no proposal for kind {kind!r}, review by hand: {ev}")
    if skipped:
        out.append(f"# ({skipped} claim(s) already have a typed link and were left out)")
    return out


def _triage(report: dict) -> list[str]:
    out = [
        "# --- triage: possible completed-work echoes ---",
        "# `close` ends an ACTIVE issue. Only uncomment if the completed work really covers it.",
    ]
    for c in report.get("candidates", []):
        if c.get("kind") != "completed-work-echo":
            continue
        out.append(
            f"# close {c['issue_id']} <reason: covered by completed {c['related_issue_id']}>"
            f"   # evidence: similarity {c.get('similarity')}; {_one_line(c.get('what_to_verify'))}"
        )
    if len(out) == 2:
        out.append("# (no completed-work-echo candidates in this report)")
    return out


def _rules(rules: list[dict], snap: snapshot.Snapshot) -> list[str]:
    out: list[str] = []
    for rule in rules:
        name = rule["name"]
        regex = re.compile(rule["title_regex"]) if "title_regex" in rule else None
        statuses = set(rule.get("statuses", ["open"]))
        reason = rule.get("reason", f"closed by embeadify rule {name}")
        out.append(f"# --- rule {name}: proposals only, review every line ---")
        n = 0
        for ident in sorted(snap):
            s = snap[ident]
            if s.status not in statuses:
                continue
            if regex and not regex.search(s.title):
                continue
            if "max_priority" in rule and (s.priority is None or s.priority < rule["max_priority"]):
                continue
            if rule.get("require_no_dependents") and snapshot.any_dependents(snap, ident):
                continue
            if rule.get("require_no_comments") and s.comment_count != 0:
                continue  # unknown (None) never matches
            out.append(f'# close {ident} {reason}   # evidence: rule {name}; "{_one_line(s.title)}"')
            n += 1
        if n == 0:
            out.append("# (no matches)")
    return out


def build(report: dict, kind: str | None, rules: list[dict] | None = None, snap=None) -> str:
    if report.get("schema_version") != 1:
        raise PlanError(
            f"unsupported schema_version {report.get('schema_version')!r}; embeadify reads version 1"
        )
    rtype = report.get("report_type")
    if rtype not in KINDS:
        raise PlanError(f"unsupported report_type {rtype!r}; expected one of {', '.join(KINDS)}")
    if kind is not None and kind != rtype:
        raise PlanError(f"--kind {kind} does not match the report's report_type {rtype!r}")
    body = {"orphans": _orphans, "mentions": _mentions, "triage": _triage}[rtype](report)
    lines = HEADER + body
    if rules:
        lines += ["", *_rules(rules, snap or {})]
    return "\n".join(lines) + "\n"
