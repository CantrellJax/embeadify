"""Validate decisions against a snapshot, compute undo, and execute with bounded parallelism."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import bd, snapshot
from .decisions import Op

MAX_WORKERS = 8
TRANSIENT = re.compile(
    r"connection (refused|reset|closed)|i/o timeout|timed out|timeout|temporar|unavailable"
    r"|try again|broken pipe|\beof\b|too many connections|lock wait|deadlock",
    re.I,
)
GUARD = re.compile(r"guard|refusing|not allowed|read-?only|permission denied", re.I)
OPEN_STATUSES = {"open", "in_progress", "blocked", "deferred", "hooked", "pinned"}


@dataclass
class Item:
    op: Op
    verdict: str = "apply"  # apply | noop | refused
    old: str = ""
    new: str = ""
    detail: str = ""
    undo: list[Op] = field(default_factory=list)
    irreversible: bool = False
    argv: list[str] = field(default_factory=list)
    watch: list[str] = field(default_factory=list)
    # execution outcome
    outcome: str = ""  # ok | skipped | failed | ""
    message: str = ""

    @property
    def retry(self) -> str:
        import shlex

        return "bd " + shlex.join(self.argv) if self.argv else ""


def _argv(op: Op) -> list[str]:
    k, i, a = op.kind, op.id, op.arg
    if k == "close":
        return ["close", i, f"--reason={a}"]
    if k == "parent":
        return ["update", i, f"--parent={a}"]
    if k == "dup":
        return ["duplicate", i, f"--of={a}"]
    if k == "priority":
        return ["update", i, f"--priority={a}"]
    if k == "label-add":
        return ["update", i, f"--add-label={a}"]
    if k == "label-rm":
        return ["update", i, f"--remove-label={a}"]
    if k == "reopen":
        return ["reopen", i]
    if k == "status":
        return ["update", i, f"--status={a}"]
    if k == "note":
        return ["update", i, f"--append-notes={a}"]
    raise ValueError(k)


def _parse_priority(text: str) -> int | None:
    m = re.fullmatch(r"[Pp]?([0-4])", text)
    return int(m.group(1)) if m else None


def _reopen_undo(old_status: str, ident: str) -> Op:
    if old_status == "open":
        return Op("reopen", ident)
    return Op("status", ident, old_status)


def validate(
    ops: list[Op],
    snap: snapshot.Snapshot,
    *,
    mode: str = "apply",
    allow_closed_parent: bool = False,
    force_dependents: bool = False,
) -> list[Item]:
    """Check each op in order against an evolving copy of the snapshot."""
    undo_mode = mode == "undo"
    state = snapshot.clone(snap)
    items: list[Item] = []
    for op in ops:
        item = Item(op=op)
        items.append(item)
        item.argv = _argv(op)
        cur = state.get(op.id)
        if cur is None:
            item.verdict, item.detail = "refused", f"unknown id {op.id}"
            continue
        item.watch = [op.id]

        def refuse(msg: str, item=item) -> None:
            item.verdict, item.detail = "refused", msg

        def noop(msg: str, item=item) -> None:
            item.verdict, item.detail = "noop", msg

        k = op.kind
        if k in ("close", "dup"):
            target = op.arg if k == "dup" else None
            if k == "dup":
                if target == op.id:
                    refuse("an issue cannot be a duplicate of itself")
                    continue
                if target not in state:
                    refuse(f"unknown canonical id {target}")
                    continue
                item.watch.append(target)
            if cur.status == "closed":
                (noop if undo_mode else refuse)("already closed" if undo_mode else "issue is already closed")
                continue
            deps = snapshot.open_dependents(state, op.id)
            if deps and not force_dependents and not undo_mode:
                shown = ", ".join(deps[:5]) + (f" (+{len(deps) - 5} more)" if len(deps) > 5 else "")
                refuse(f"has open dependents: {shown}; close them first or pass --force-dependents")
                continue
            item.old, item.new = cur.status, "closed"
            item.undo = [_reopen_undo(cur.status, op.id)]
            if k == "dup":
                item.detail = f"duplicate of {target}"
            else:
                item.detail = f"reason: {op.arg}"
            cur.status = "closed"
        elif k == "parent":
            new = op.arg or None
            if new == op.id:
                refuse("parent equals the issue itself")
                continue
            if new is not None:
                if new not in state:
                    refuse(f"unknown parent id {new}")
                    continue
                item.watch.append(new)
                if state[new].status == "closed" and not allow_closed_parent and not undo_mode:
                    refuse(f"parent {new} is closed; pass --allow-closed-parent to override")
                    continue
                walker, seen = new, set()
                cycle = False
                while walker is not None and walker not in seen:
                    if walker == op.id:
                        cycle = True
                        break
                    seen.add(walker)
                    walker = state[walker].parent if walker in state else None
                if cycle:
                    refuse(f"would create a parent cycle ({op.id} is an ancestor of {new})")
                    continue
            if cur.parent == new:
                noop(f"parent already {new or '(none)'}")
                continue
            item.old, item.new = cur.parent or "(none)", new or "(none)"
            item.undo = [Op("parent", op.id, cur.parent or "")]
            cur.parent = new
        elif k == "priority":
            n = _parse_priority(op.arg)
            if n is None:
                refuse(f"priority must be 0-4 or P0-P4, got {op.arg!r}")
                continue
            if cur.priority == n:
                noop(f"priority already {n}")
                continue
            item.old, item.new = str(cur.priority), str(n)
            item.argv = ["update", op.id, f"--priority={n}"]
            if cur.priority is not None:
                item.undo = [Op("priority", op.id, str(cur.priority))]
            else:
                item.irreversible = True
            cur.priority = n
        elif k == "label-add":
            if op.arg in cur.labels:
                noop(f"label {op.arg} already present")
                continue
            item.old, item.new = "(absent)", op.arg
            item.undo = [Op("label-rm", op.id, op.arg)]
            cur.labels.add(op.arg)
        elif k == "label-rm":
            if op.arg not in cur.labels:
                noop(f"label {op.arg} not present")
                continue
            item.old, item.new = op.arg, "(removed)"
            item.undo = [Op("label-add", op.id, op.arg)]
            cur.labels.discard(op.arg)
        elif k == "reopen":
            if cur.status != "closed":
                noop(f"issue is not closed (status {cur.status})")
                continue
            item.old, item.new = "closed", "open"
            item.undo = [Op("close", op.id, "embeadify undo: re-closing after reopen")]
            cur.status = "open"
        elif k == "status":
            if op.arg == "closed":
                refuse("use `close` to close an issue")
                continue
            if cur.status == op.arg:
                noop(f"status already {op.arg}")
                continue
            item.old, item.new = cur.status, op.arg
            item.undo = [
                Op("close", op.id, "embeadify undo: restoring closed status")
                if cur.status == "closed"
                else Op("status", op.id, cur.status)
            ]
            cur.status = op.arg
        elif k == "note":
            item.old, item.new = "", "(append note)"
            item.detail = "append-only; cannot be undone automatically"
            item.irreversible = True
    return items


def undo_text(items: list[Item], source: str, target: str) -> str:
    """Render the undo decisions (reverse order) as a replayable decisions file."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "# embeadify undo file: replay with `embeadify undo FILE --apply`",
        f"# created {stamp} from {source}",
        f"# target: {target}",
        "",
    ]
    for item in reversed(items):
        if item.verdict != "apply":
            continue
        if item.irreversible:
            lines.append(f"# cannot undo automatically: {item.op.render()}")
        for u in item.undo:
            lines.append(u.render())
    return "\n".join(lines) + "\n"


def write_text_exclusive(path: Path, text: str) -> None:
    with open(path, "x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def replace_text(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent or ".", prefix=".embeadify-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)


def default_undo_path() -> Path:
    return Path(f"embeadify-undo-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.decisions")


def detect_drift(items: list[Item], before: snapshot.Snapshot, after: snapshot.Snapshot) -> dict[str, str]:
    """Return {issue id: reason} for issues touched by pending ops that changed since the snapshot."""
    drift: dict[str, str] = {}
    for item in items:
        if item.verdict != "apply":
            continue
        for ident in item.watch:
            if ident in drift:
                continue
            old, new = before.get(ident), after.get(ident)
            if old is None:
                continue
            if new is None:
                drift[ident] = "issue no longer exists"
            elif old.watch() != new.watch():
                names = ("status", "parent", "priority", "labels")
                changed = [
                    f"{n} {a} -> {b}"
                    for n, a, b in zip(names, old.watch(), new.watch(), strict=True)
                    if a != b
                ]
                drift[ident] = "changed since snapshot: " + "; ".join(changed)
    return drift


def _execute_one(item: Item, timeout: float, retries: int = 1) -> None:
    attempt = 0
    while True:
        try:
            result = bd.run(item.argv, timeout=timeout)
        except bd.BdError as error:
            item.outcome, item.message = "failed", str(error)
            return
        if result.returncode == 0:
            item.outcome, item.message = "ok", ""
            return
        message = bd.redact(result.stderr.strip() or result.stdout.strip() or "no output").splitlines()
        message = " | ".join(message[:3])
        guard = bool(GUARD.search(message)) and not result.timed_out
        transient = bool(TRANSIENT.search(message)) or result.timed_out
        ambiguous = result.timed_out and item.op.kind == "note"  # an append might have landed
        if attempt < retries and transient and not guard and not ambiguous:
            attempt += 1
            time.sleep(0.2)
            continue
        item.outcome, item.message = "failed", f"exit {result.returncode}: {message}"
        return


def _run_group(group: list[Item], timeout: float, on_done: Callable[[Item], None]) -> None:
    failed_id = None
    for item in group:
        if failed_id is not None:
            item.outcome, item.message = "skipped", "an earlier op on this issue failed"
        else:
            _execute_one(item, timeout)
            if item.outcome == "failed":
                failed_id = item.op.id
        on_done(item)


def execute(
    items: list[Item],
    *,
    jobs: int,
    timeout: float,
    drift: dict[str, str],
    progress: bool,
) -> None:
    """Run every pending item. Failures never abort the batch."""
    pending = [i for i in items if i.verdict == "apply"]
    for item in items:
        if item.verdict == "noop":
            item.outcome, item.message = "skipped", item.detail
    groups: dict[str, list[Item]] = {}
    for item in pending:
        reasons = [drift[w] for w in item.watch if w in drift]
        if reasons:
            item.outcome = "skipped"
            item.message = "drift on " + next(w for w in item.watch if w in drift) + ": " + reasons[0]
            continue
        groups.setdefault(item.op.id, []).append(item)
    total = len(pending)
    lock = threading.Lock()
    counts = {"done": 0, "ok": 0, "failed": 0, "skipped": 0}
    tty = sys.stderr.isatty()

    def tick(item: Item) -> None:
        with lock:
            counts["done"] += 1
            counts[item.outcome] += 1
            if progress:
                line = (
                    f"[{counts['done']}/{total}] ok {counts['ok']}  "
                    f"failed {counts['failed']}  skipped {counts['skipped']}"
                )
                if tty:
                    sys.stderr.write("\r" + line)
                else:
                    sys.stderr.write(f"{line}  last: {item.op.render()}\n")
                sys.stderr.flush()

    for item in pending:
        if item.outcome == "skipped":
            tick(item)
    if groups:
        with ThreadPoolExecutor(max_workers=max(1, min(jobs, MAX_WORKERS))) as pool:
            futures = [pool.submit(_run_group, g, timeout, tick) for g in groups.values()]
            for f in as_completed(futures):
                f.result()
    if progress and tty and total:
        sys.stderr.write("\n")
