"""The queue directory: immutable submissions, decision receipts, leases, the run lock, the log."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import candidate as cand

DEFAULT_DIR = Path.home() / ".local" / "state" / "embeadify" / "scribe"


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Conflict(Exception):
    """Same candidate_id, different content hash."""


class LockHeld(Exception):
    pass


@dataclass
class Submission:
    path: Path
    candidate: dict | None = None
    hash: str = ""
    receipt_id: str = ""
    submitted_at: str = ""
    error: str = ""


def _alive(pid: int) -> bool:
    if os.name == "nt":
        return True  # never guess on Windows: a stale lock must be removed by hand
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Queue:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root else DEFAULT_DIR
        self.submissions_dir = self.root / "submissions"
        self.decisions_dir = self.root / "decisions"
        self.leases_dir = self.root / "leases"
        self.log_path = self.root / "log.jsonl"

    def ensure(self) -> None:
        for d in (self.submissions_dir, self.decisions_dir, self.leases_dir):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)

    # -- submissions (immutable) --------------------------------------------------------------
    def submit(self, candidate: dict) -> tuple[str, str, bool]:
        """Store a validated candidate. Returns (receipt_id, hash, newly_stored). Never overwrites."""
        self.ensure()
        cid, digest = candidate["candidate_id"], cand.digest(candidate)
        rid = cand.receipt_id(cid, digest)
        final = self.submissions_dir / f"{cid}.json"
        record = {
            "schema_version": 1,
            "candidate_id": cid,
            "hash": digest,
            "receipt_id": rid,
            "submitted_at": now(),
            "candidate": candidate,
        }
        fd, tmp = tempfile.mkstemp(dir=self.submissions_dir, prefix=".tmp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True, indent=2)
        try:
            os.link(tmp, final)  # atomic, and refuses to replace an existing file
            os.chmod(final, 0o444)
            return rid, digest, True
        except FileExistsError:
            existing = self._load(final)
            if existing.hash == digest:
                return rid, digest, False
            raise Conflict(
                f"candidate_id {cid} was already submitted with different content "
                f"(stored hash {existing.hash[:12]}, new hash {digest[:12]}); use a new candidate_id"
            ) from None
        finally:
            os.unlink(tmp)

    def _load(self, path: Path) -> Submission:
        sub = Submission(path=path)
        try:
            if path.stat().st_size > cand.MAX_FILE_BYTES * 2:
                raise ValueError("submission file is too large")
            record = json.loads(path.read_text(encoding="utf-8"))
            c = cand.validate(record["candidate"])
            sub.candidate, sub.hash = c, cand.digest(c)
            sub.receipt_id = cand.receipt_id(c["candidate_id"], sub.hash)
            sub.submitted_at = str(record.get("submitted_at", ""))
            if record.get("hash") != sub.hash or path.name != f"{c['candidate_id']}.json":
                raise ValueError("stored hash or name does not match the content (file was altered)")
        except (OSError, ValueError, KeyError, TypeError) as error:
            sub.candidate, sub.error = None, str(error)[:300]
        return sub

    def submissions(self) -> list[Submission]:
        if not self.submissions_dir.is_dir():
            return []
        subs = [self._load(p) for p in sorted(self.submissions_dir.glob("*.json"))]
        return sorted(subs, key=lambda s: (s.submitted_at, s.path.name))

    # -- decision receipts (immutable) --------------------------------------------------------
    def decision(self, rid: str) -> dict | None:
        path = self.decisions_dir / f"{rid}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def write_decision(self, rid: str, receipt: dict) -> bool:
        self.ensure()
        try:
            with open(self.decisions_dir / f"{rid}.json", "x", encoding="utf-8") as handle:
                json.dump(receipt, handle, sort_keys=True, indent=2)
        except FileExistsError:
            return False
        return True

    # -- leases: a leftover lease means an earlier run died between the write and its receipt --
    def take_lease(self, cid: str) -> int:
        path = self.leases_dir / f"{cid}.json"
        try:
            attempts = int(json.loads(path.read_text(encoding="utf-8")).get("attempts", 0))
        except (OSError, ValueError, AttributeError):
            attempts = 0
        self.ensure()
        path.write_text(json.dumps({"attempts": attempts + 1, "taken_at": now(), "pid": os.getpid()}))
        return attempts + 1

    def lease_attempts(self, cid: str) -> int:
        try:
            return int(json.loads((self.leases_dir / f"{cid}.json").read_text()).get("attempts", 0))
        except (OSError, ValueError, AttributeError):
            return 0

    def release_lease(self, cid: str) -> None:
        (self.leases_dir / f"{cid}.json").unlink(missing_ok=True)

    # -- the single-executor lock -------------------------------------------------------------
    @contextmanager
    def lock(self):
        self.ensure()
        path = self.root / "run.lock"
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                break
            except FileExistsError:
                try:
                    pid = int(path.read_text().strip())
                except (OSError, ValueError):
                    pid = 0
                if pid and _alive(pid):
                    raise LockHeld(f"another scribe run holds {path} (pid {pid})") from None
                if os.name == "nt" and pid:
                    raise LockHeld(f"{path} exists; remove it by hand if no run is active") from None
                path.unlink(missing_ok=True)  # stale: its owner is gone
        else:
            raise LockHeld(f"could not take {path}")
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        try:
            yield
        finally:
            path.unlink(missing_ok=True)

    def lock_holder(self) -> int | None:
        try:
            pid = int((self.root / "run.lock").read_text().strip())
        except (OSError, ValueError):
            return None
        return pid if _alive(pid) else None

    # -- the log ------------------------------------------------------------------------------
    def log(self, entry: dict) -> None:
        self.ensure()
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    def read_log(self) -> list[dict]:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def latest_log(self) -> dict[str, dict]:
        latest: dict[str, dict] = {}
        for entry in self.read_log():
            latest[entry.get("candidate_id", "")] = entry
        return latest
