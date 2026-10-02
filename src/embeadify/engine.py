"""Validate decisions against a snapshot, compute undo, and execute with bounded parallelism."""

from __future__ import annotations

import json
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

from . import bd, sanitize, snapshot
from .decisions import Op, create_fields

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
    marker: str = (
        ""  # `candidate:ID` or `provenance:ID`: how to tell, after an ambiguous write, that it landed
    )
    marker_on: str = ""  # bead that carries a `provenance:` marker
    created_id: str = ""
    # execution outcome
    outcome: str = ""  # ok | skipped | failed | ""
    message: str = ""

    @property
    def retry(self) -> str:
        import shlex

        return "bd " + shlex.join(self.argv) if self.argv else ""


MAX_CREATE_BODY_FILE = 1_000_000
MAX_CREATE_TITLE = 200
MAX_CREATE_BODY = 8000


def create_argv(
    title: str,
    type_: str,
    priority: int | str,
    parent: str | None,
    description: str,
    labels: list[str] | None = None,
) -> list[str]:
    """The one place a `bd create` argument array is built. Every value is a single `--flag=value` word."""
    argv = [
        "create",
        f"--title={title}",
        f"--type={type_}",
        f"--priority={priority}",
        f"--description={description}",
        "--json",
    ]
    if labels:
        argv.append("--labels=" + ",".join(labels))
    if parent:
        # bd copies the parent's labels onto a child unless told not to (a stray 'theme' label leaked onto
        # 13 real beads); labels are always set explicitly, never inherited.
        argv.append(f"--parent={parent}")
        argv.append("--no-inherit-labels")
    return argv


def description_with_marker(body: str, candidate_id: str, extra: list[str] | None = None) -> str:
    parts = [part for part in [body, *(extra or [])] if part]
    parts.append(sanitize.marker_line("candidate", candidate_id))
    return "\n\n".join(parts)


def _argv(op: Op) -> list[str]:
    k, i, a = op.kind, op.id, op.arg
    if k == "create":
        return []  # built in validate(): needs the body file and the snapshot
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
    created: set[str] = set()
    for op in ops:
        item = Item(op=op)
        items.append(item)
        item.argv = _argv(op)
        if op.kind == "create":
            _validate_create(item, state, created, undo_mode, allow_closed_parent)
            continue
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


def _validate_create(
    item: Item, state, created: set[str], undo_mode: bool, allow_closed_parent: bool
) -> None:
    op = item.op
    if undo_mode:
        item.verdict, item.detail = "refused", "`create` cannot appear in an undo file"
        return
    fields = create_fields(op.arg)
    hit = snapshot.find_marker(state, op.id, kinds=("candidate",))
    if hit or op.id in created:
        item.verdict, item.detail = (
            "noop",
            f"candidate {op.id} already created as {hit[1] if hit else 'earlier line'}",
        )
        return
    parent = fields.get("parent")
    if parent:
        if parent not in state:
            item.verdict, item.detail = "refused", f"unknown parent id {parent}"
            return
        if state[parent].status == "closed" and not allow_closed_parent:
            item.verdict = "refused"
            item.detail = f"parent {parent} is closed; pass --allow-closed-parent to override"
            return
        item.watch = [parent]
    body = ""
    if "body-file" in fields:
        try:
            with open(fields["body-file"], "rb") as handle:
                raw = handle.read(MAX_CREATE_BODY_FILE + 1)
        except OSError as error:
            item.verdict, item.detail = "refused", f"cannot read body-file: {error.strerror or error}"
            return
        if len(raw) > MAX_CREATE_BODY_FILE:
            item.verdict, item.detail = "refused", f"body-file is larger than {MAX_CREATE_BODY_FILE} bytes"
            return
        body, truncated = sanitize.clean_block(raw.decode("utf-8", errors="replace"), MAX_CREATE_BODY)
        if truncated:
            body += f"\n\n[body truncated at {MAX_CREATE_BODY} characters]"
    title = sanitize.clean_line(fields["title"], MAX_CREATE_TITLE)
    if not title:
        item.verdict, item.detail = "refused", "title is empty after cleaning"
        return
    item.argv = create_argv(
        title, fields["type"], fields["priority"], parent, description_with_marker(body, op.id)
    )
    item.marker = f"candidate:{op.id}"
    item.old, item.new = "(none)", f"(new {fields['type']} P{fields['priority']})"
    item.detail = f"title: {title}; undo is `close NEW_ID` once the id is known"
    created.add(op.id)


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
        if item.op.kind == "create":
            if item.created_id:
                reason = f"embeadify undo: created for {item.op.id}"
                lines.append(Op("close", item.created_id, reason).render())
            else:
                lines.append(
                    f"# create {item.op.id}: new id not known yet; find it by the line"
                    f" `{sanitize.marker_line('candidate', item.op.id)}` in the description, then close it"
                )
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


def _created_id(stdout: str) -> str:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return ""
    if isinstance(data, list) and data:
        data = data[0]
    value = data.get("id") if isinstance(data, dict) else None
    return value if sanitize.is_id(value) else ""


def _find_applied(item: Item) -> str:
    """Fresh snapshot lookup of the item's marker. Returns the bead id when the write already landed."""
    snap = snapshot.take()
    kind, _, cid = item.marker.partition(":")
    if kind == "candidate":
        hit = snapshot.find_marker(snap, cid, kinds=("candidate",))
        return hit[1] if hit else ""
    state = snap.get(item.marker_on)
    return item.marker_on if state is not None and item.marker in state.markers else ""


def _execute_marked(item: Item, timeout: float, retries: int) -> None:
    """Run a write that carries a marker. A failure is never retried before the marker is looked up."""
    attempt = 0
    is_create = item.op.kind == "create"
    while True:
        try:
            result = bd.run(item.argv, timeout=timeout)
        except bd.BdError as error:
            item.outcome, item.message = "failed", str(error)
            return
        if result.returncode == 0:
            item.outcome, item.message = "ok", ""
            if is_create:
                item.created_id = _created_id(result.stdout)
                if not item.created_id:
                    try:
                        item.created_id = _find_applied(item)
                    except bd.BdError:
                        item.message = "created, but the new id could not be read back"
            return
        message = bd.redact(result.stderr.strip() or result.stdout.strip() or "no output").splitlines()
        message = " | ".join(message[:3])
        try:
            landed = _find_applied(item)
        except bd.BdError as error:
            item.outcome = "failed"
            item.message = (
                f"exit {result.returncode}: {message}; could not confirm whether it landed: {error}"
            )
            return
        if landed:
            item.outcome, item.message = "ok", "reconciled: the write had already landed"
            if is_create:
                item.created_id = landed
            return
        guard = bool(GUARD.search(message)) and not result.timed_out
        transient = bool(TRANSIENT.search(message)) or result.timed_out
        if attempt < retries and transient and not guard:
            attempt += 1
            time.sleep(0.2)
            continue
        item.outcome, item.message = "failed", f"exit {result.returncode}: {message}"
        return


def run_item(item: Item, timeout: float) -> None:
    """Execute one validated item (used by the scribe executor)."""
    _execute_one(item, timeout)


def _execute_one(item: Item, timeout: float, retries: int = 1) -> None:
    if item.marker:
        _execute_marked(item, timeout, retries)
        return
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
