"""One snapshot of the tracker, taken with a single ``bd list`` call."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import bd

LIST_ARGS = ["list", "--all", "--limit", "0", "--json"]
DEP_EDGES = ("blocks", "parent-child")


@dataclass
class State:
    id: str
    status: str
    parent: str | None
    priority: int | None
    labels: set[str]
    blocks: set[str] = field(default_factory=set)  # ids this issue depends on via `blocks`
    title: str = ""
    comment_count: int | None = None

    def watch(self) -> tuple:
        return (self.status, self.parent, self.priority, tuple(sorted(self.labels)))


Snapshot = dict[str, State]


def _priority(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse(raw) -> Snapshot:
    if isinstance(raw, dict):
        raw = raw.get("issues", raw.get("data", []))
    if not isinstance(raw, list):
        raise bd.BdError("unexpected `bd list --json` shape: expected a JSON array of issues")
    snap: Snapshot = {}
    for item in raw:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        ident = str(item["id"])
        parent = item.get("parent_id") or item.get("parent") or None
        blocks: set[str] = set()
        for dep in item.get("dependencies") or []:
            if not isinstance(dep, dict):
                continue
            target = dep.get("depends_on_id")
            kind = dep.get("type") or dep.get("dependency_type")
            if kind == "parent-child" and parent is None and dep.get("issue_id", ident) == ident:
                parent = target
            elif kind == "blocks" and target:
                blocks.add(str(target))
        snap[ident] = State(
            id=ident,
            status=str(item.get("status") or "open"),
            parent=str(parent) if parent else None,
            priority=_priority(item.get("priority")),
            labels={str(x) for x in item.get("labels") or []},
            blocks=blocks,
            title=" ".join(str(item.get("title") or "").split()),
            comment_count=_priority(item.get("comment_count")),
        )
    return snap


def take() -> Snapshot:
    return parse(bd.run_json(LIST_ARGS))


def clone(snap: Snapshot) -> Snapshot:
    return {
        k: State(v.id, v.status, v.parent, v.priority, set(v.labels), set(v.blocks), v.title, v.comment_count)
        for k, v in snap.items()
    }


def open_dependents(snap: Snapshot, ident: str) -> list[str]:
    """Open issues that are children of, or blocked by, ``ident``."""
    return sorted(
        s.id
        for s in snap.values()
        if s.status != "closed" and s.id != ident and (s.parent == ident or ident in s.blocks)
    )


def any_dependents(snap: Snapshot, ident: str) -> bool:
    return any(s.id != ident and (s.parent == ident or ident in s.blocks) for s in snap.values())
