"""`scribe replay`: judge beads that already exist as if they were fresh submissions, in SHADOW only.

Nothing here writes the tracker (one read-only `bd list` snapshot) and nothing is queued: a replayed
candidate never enters `submissions/`, so a later `scribe run --live` can never act on it. Decisions are
appended to `log.jsonl` with `replay: true` and an `actual` block (what the filer really did).

TEMPORAL FAIRNESS lives in this file and nowhere else. When judging bead X the scribe may only know
what existed when X was filed:

* ``visible_neighbors`` (the ONE adapter function over `embead match` output) drops X itself and every bead
  created at or after X (ordered by ``(created_at, id)``), and rewinds a neighbor that was closed after X
  was filed to open.
* ``TemporalSnapshot`` gives the executor the same view of the tracker (target and parent lookups).
* The candidate carries no `parent` hint and no `source.bead`: both would leak X's own parent.

Known limit: titles, parents and labels are as of the snapshot, not as of X's creation time.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path

from .. import sanitize, snapshot
from ..decisions import CREATE_TYPES
from . import candidate as cand
from . import match, runner
from . import store as st
from .match import Neighbor
from .policy import Policy

REPLAY_PREFIX = "replay-"
NEIGHBOR_PAD = 15  # ask the matcher for this many extra neighbors, then filter in visible_neighbors
DUP_RE = re.compile(r"\bduplicate\b|\bdup of\b|\bdupe\b", re.IGNORECASE)
_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_ts(value) -> datetime | None:
    """An ISO-8601 timestamp (any fractional precision, Z or offset) as an aware UTC datetime."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = _FRACTION.sub(r"\1", value.strip())
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


@dataclass(frozen=True)
class Bead:
    id: str
    title: str
    body: str
    type: str
    priority: int
    status: str
    parent: str | None
    created_at: datetime | None
    closed_at: datetime | None
    created_by: str
    ephemeral: bool
    duplicate: bool
    labels: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[datetime, str]:
        return (self.created_at or datetime.max.replace(tzinfo=UTC), self.id)


def raw_items(raw) -> list[dict]:
    if isinstance(raw, dict):
        raw = raw.get("issues", raw.get("data", []))
    return [i for i in raw if isinstance(i, dict) and i.get("id")] if isinstance(raw, list) else []


def parse_beads(raw) -> dict[str, Bead]:
    """The creation-time facts replay needs, from the same raw `bd list --json` the snapshot parses."""
    parsed = snapshot.parse(raw)
    out: dict[str, Bead] = {}
    for item in raw_items(raw):
        ident = str(item["id"])
        kinds = [
            str(d.get("type") or d.get("dependency_type") or "")
            for d in item.get("dependencies") or []
            if isinstance(d, dict)
        ]
        reason = str(item.get("close_reason") or "")
        status = str(item.get("status") or "open")
        prio = parsed[ident].priority
        out[ident] = Bead(
            id=ident,
            title=" ".join(str(item.get("title") or "").split()),
            body=str(item.get("description") or ""),
            type=str(item.get("issue_type") or "task"),
            priority=prio if prio is not None and 0 <= prio <= 4 else 2,
            status=status,
            parent=parsed[ident].parent,
            created_at=parse_ts(item.get("created_at")),
            closed_at=parse_ts(item.get("closed_at")),
            created_by=str(item.get("created_by") or item.get("owner") or ""),
            ephemeral=bool(item.get("ephemeral")),
            duplicate=status == "closed"
            and (bool(DUP_RE.search(reason)) or any(k in ("duplicates", "duplicate-of") for k in kinds)),
            labels=tuple(str(x) for x in item.get("labels") or []),
        )
    return out


def closed_at_the_time(bead: Bead, x: Bead) -> bool:
    """Was ``bead`` closed when X was filed? Closed with no close time: assume it was (cannot rewind)."""
    if bead.status != "closed":
        return False
    return bead.closed_at is None or bead.closed_at <= (x.created_at or bead.closed_at)


def existed_before(bead: Bead | None, x: Bead) -> bool:
    return bead is not None and bead.id != x.id and bead.created_at is not None and bead.key < x.key


class TemporalSnapshot(Mapping):
    """The tracker as X saw it: only earlier beads, each with the status it had when X was filed."""

    def __init__(self, base: snapshot.Snapshot, beads: dict[str, Bead], x: Bead):
        self._base, self._beads, self._x = base, beads, x

    def _visible(self, ident) -> bool:
        return ident in self._base and existed_before(self._beads.get(ident), self._x)

    def __contains__(self, ident) -> bool:
        return self._visible(ident)

    def __getitem__(self, ident):
        if not self._visible(ident):
            raise KeyError(ident)
        state = self._base[ident]
        if state.status == "closed" and not closed_at_the_time(self._beads[ident], self._x):
            return replace(state, status="open")
        return state

    def __iter__(self) -> Iterator[str]:
        return (i for i in self._base if self._visible(i))

    def __len__(self) -> int:
        return sum(1 for _ in self)


def visible_neighbors(neighbors: list[Neighbor], x: Bead, beads: dict[str, Bead], cap: int) -> list[Neighbor]:
    """THE temporal filter for neighbors. Excludes X and every bead not created strictly before X."""
    out = []
    for n in neighbors:
        bead = beads.get(n.issue_id)
        if not existed_before(bead, x):
            continue
        if n.is_closed and bead is not None and not closed_at_the_time(bead, x):
            n = replace(n, status="open", is_closed=False, resolution_evidence="")
        out.append(n)
    return out[:cap]


def candidate_for(bead: Bead) -> dict | None:
    """The bead as a fresh submission. No parent hint and no source bead: both would leak the answer."""
    title = bead.title[: cand.MAX_TITLE]
    if not title:
        return None
    labels = [lab for lab in bead.labels if 0 < len(lab) <= cand.MAX_LABEL][: cand.MAX_LABELS]
    try:
        return cand.validate(
            {
                "candidate_id": REPLAY_PREFIX + bead.id,
                "title": title,
                "body": bead.body[: cand.MAX_BODY] or title,
                "type": bead.type if bead.type in CREATE_TYPES else "task",
                "priority": bead.priority,
                "source": {"agent": "replay"},
                "labels": labels,
            }
        )
    except cand.CandidateError:
        return None


def actual_of(bead: Bead) -> dict:
    """What the filer really did. `parent` is the CURRENT parent (the snapshot has no history)."""
    return {
        "bead_id": bead.id,
        "parent": bead.parent,
        "parent_source": "current",
        "status": bead.status,
        "created_by": bead.created_by,
        "created_at": bead.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if bead.created_at else None,
        "filer_action": "dup" if bead.duplicate else "create",
    }


@dataclass
class ReplaySummary:
    selected: int = 0
    replayed: int = 0
    already: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    degraded: int = 0
    details: list[str] = field(default_factory=list)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


def replayed_keys(queue: st.Queue) -> set[tuple[str, str]]:
    return {
        (e.get("candidate_id", ""), e.get("policy_version", ""))
        for e in queue.read_log()
        if e.get("replay") and e.get("mode") == "shadow"
    }


def read_ids_file(path: str) -> list[str]:
    ids = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        word = line.split("#", 1)[0].strip()
        if word:
            ids.append(word)
    return ids


def parse_since(text: str) -> datetime:
    try:
        return datetime.combine(date.fromisoformat(text), datetime.min.time(), tzinfo=UTC)
    except ValueError:
        raise ValueError(f"--since must be YYYY-MM-DD, got {text!r}") from None


def replay(
    queue: st.Queue,
    policy: Policy,
    raw,
    *,
    since: datetime | None = None,
    ids: list[str] | None = None,
    limit: int | None = None,
    again: bool = False,
    include_ephemeral: bool = False,
    include_duplicates: bool = False,
    recommender: tuple[str, ...] = (),
    neighbor_limit: int = match.MAX_NEIGHBORS,
    timeout: float = 120.0,
) -> ReplaySummary:
    """Replay beads from ONE snapshot (``raw``, the decoded `bd list --all --limit 0 --json`)."""
    summary = ReplaySummary()
    base = snapshot.parse(raw)
    beads = parse_beads(raw)
    wanted = None if ids is None else set(ids)
    if wanted is not None:
        for missing in sorted(wanted - set(beads)):
            summary.skip("not_in_snapshot")
            summary.details.append(f"{missing}: not in the snapshot")
    done = replayed_keys(queue)
    for bead in sorted(beads.values(), key=lambda b: b.key):
        if wanted is not None and bead.id not in wanted:
            continue
        if since is not None and (bead.created_at is None or bead.created_at < since):
            continue
        summary.selected += 1
        if bead.created_at is None:
            summary.skip("no_created_at")
        elif bead.ephemeral and not include_ephemeral:
            summary.skip("ephemeral")
        elif bead.duplicate and not include_duplicates:
            summary.skip("closed_as_duplicate")
        elif (REPLAY_PREFIX + bead.id, policy.policy_version) in done and not again:
            summary.already += 1
        elif (c := candidate_for(bead)) is None:
            summary.skip("no_title")
        else:
            if limit is not None and summary.replayed >= limit:
                break
            _one(queue, policy, bead, c, beads, base, recommender, neighbor_limit, timeout, summary)
    return summary


def _one(queue, policy, bead, c, beads, base, recommender, neighbor_limit, timeout, summary) -> None:
    asked = neighbor_limit + NEIGHBOR_PAD
    found, degraded = match.fetch(c, policy.match_command, timeout, limit=asked)
    neighbors = visible_neighbors(found, bead, beads, neighbor_limit)
    view = TemporalSnapshot(base, beads, bead)
    rec, plan, fields = runner.decide(c, neighbors, degraded, view, policy, recommender, timeout)
    digest = cand.digest(c)
    queue.log(
        {
            "candidate_id": c["candidate_id"],
            "hash": digest,
            "receipt_id": cand.receipt_id(c["candidate_id"], digest),
            "policy_version": policy.policy_version,
            "mode": "shadow",
            "outcome": "shadow",
            "replay": True,
            "ts": st.now(),
            "actual": actual_of(bead),
            "neighbors_dropped": len(found) - len(neighbors),
            **fields,
        }
    )
    summary.replayed += 1
    summary.degraded += bool(degraded)
    summary.details.append(
        f"{c['candidate_id']}: {plan.action}"
        + (f" -> {plan.target_id}" if plan.target_id else "")
        + (f" [{degraded}]" if degraded else "")
        + (f" (filer: {sanitize.clean_line(bead.created_by, 40) or '?'})")
    )
