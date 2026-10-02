"""A synthetic `bd` for offline tests. State lives in the JSON file named by FAKE_BD_DB.

Knobs (environment): FAKE_BD_LOG (append one line per write), FAKE_BD_SLEEP (seconds per write),
FAKE_BD_FAIL (comma ids whose writes fail permanently), FAKE_BD_FLAKY (comma ids whose first write
fails with a transient error), FAKE_BD_GUARD (any write is refused by a guard),
FAKE_BD_MUTATE_ON_THIRD_LIST ("id:field=value": applied before the 3rd `list` (doctor, snapshot, recheck)),
FAKE_BD_NO_CONTEXT (context --json fails), FAKE_BD_SECRET (echoed into context to test redaction),
FAKE_BD_CALLS (append every write's full argv as one JSON line),
FAKE_BD_CRASH_AFTER_CREATE / FAKE_BD_CRASH_AFTER_NOTE (the first such write lands, then bd reports a lost
connection and exits 1: the ambiguous-write case).
"""

import json
import os
import sys
import time
from pathlib import Path

DB = Path(os.environ["FAKE_BD_DB"])
LOCK = DB.with_suffix(".lock")


def acquire():
    while True:
        try:
            return os.open(LOCK, os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            time.sleep(0.005)


def release(fd):
    os.close(fd)
    os.unlink(LOCK)


def load():
    return json.loads(DB.read_text())


def save(data):
    DB.write_text(json.dumps(data))


def log(line):
    path = os.environ.get("FAKE_BD_LOG")
    if path:
        with open(path, "a") as handle:
            handle.write(f"{line} {time.time():.4f}\n")


def record_call(args):
    path = os.environ.get("FAKE_BD_CALLS")
    if path:
        with open(path, "a") as handle:
            handle.write(json.dumps(args) + "\n")


def flag(args, name):
    for a in args:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return None


def create(args):
    if os.environ.get("FAKE_BD_GUARD"):
        print("bd-guard: writes to this database are refused", file=sys.stderr)
        return 1
    fd = acquire()
    try:
        data = load()
        numbers = [int(i["id"].rsplit("-", 1)[1]) for i in data if i["id"].rsplit("-", 1)[-1].isdigit()]
        new_id = f"demo-{max(numbers, default=0) + 1}"
        parent = flag(args, "parent")
        if parent and not any(i["id"] == parent for i in data):
            print(f"unknown parent {parent}", file=sys.stderr)
            return 1
        data.append(
            {
                "id": new_id,
                "title": flag(args, "title"),
                "status": "open",
                "issue_type": flag(args, "type"),
                "priority": int(flag(args, "priority")),
                "description": flag(args, "description") or "",
                "labels": [],
                "parent_id": parent,
                "comment_count": 0,
                "dependencies": [],
            }
        )
        save(data)
    finally:
        release(fd)
    if os.environ.get("FAKE_BD_CRASH_AFTER_CREATE") and not DB.with_suffix(".crashed-create").exists():
        DB.with_suffix(".crashed-create").write_text("1")
        print("read tcp: connection reset by peer", file=sys.stderr)
        return 1
    print(json.dumps({"id": new_id}))
    return 0


def main():
    args = sys.argv[1:]
    if args[:1] == ["--version"]:
        print("bd version 0.0.0-fake")
        return 0
    if args[:1] == ["context"]:
        if os.environ.get("FAKE_BD_NO_CONTEXT"):
            print("context unavailable", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "backend": "dolt",
                    "server": {
                        "host": "dolt.example.test",
                        "port": 3306,
                        "database": "demo",
                        "password": os.environ.get("FAKE_BD_SECRET", ""),
                    },
                    "dsn": f"mysql://root:{os.environ.get('FAKE_BD_SECRET', 'x')}@dolt.example.test/demo",
                }
            )
        )
        return 0
    if args[:1] == ["list"]:
        fd = acquire()
        try:
            counter = DB.with_suffix(".lists")
            n = int(counter.read_text()) + 1 if counter.exists() else 1
            counter.write_text(str(n))
            data = load()
            mutate = os.environ.get("FAKE_BD_MUTATE_ON_THIRD_LIST")
            if mutate and n == 3:
                ident, rest = mutate.split(":", 1)
                key, value = rest.split("=", 1)
                for issue in data:
                    if issue["id"] == ident:
                        issue[key] = value
                save(data)
        finally:
            release(fd)
        print(json.dumps(data))
        return 0
    # writers
    verb = args[0]
    record_call(args)
    if verb == "create":
        return create(args)
    ident = args[1]
    log(f"start {verb} {ident}")
    time.sleep(float(os.environ.get("FAKE_BD_SLEEP", "0")))
    if os.environ.get("FAKE_BD_GUARD"):
        print("bd-guard: writes to this database are refused", file=sys.stderr)
        log(f"end {verb} {ident}")
        return 1
    if ident in os.environ.get("FAKE_BD_FAIL", "").split(","):
        print("exit: permission-independent hard failure", file=sys.stderr)
        log(f"end {verb} {ident}")
        return 1
    fd = acquire()
    try:
        flaky = os.environ.get("FAKE_BD_FLAKY", "").split(",")
        marker = DB.with_suffix(f".flaky-{ident}")
        if ident in flaky and not marker.exists():
            marker.write_text("1")
            print("dial tcp: connection refused", file=sys.stderr)
            return 1
        data = load()
        issue = next(i for i in data if i["id"] == ident)
        if verb == "close":
            issue["status"], issue["close_reason"] = "closed", flag(args, "reason")
        elif verb == "duplicate":
            issue["status"], issue["close_reason"] = "closed", "duplicate of " + flag(args, "of")
        elif verb == "reopen":
            issue["status"] = "open"
        elif verb == "update":
            if (v := flag(args, "parent")) is not None:
                issue["parent_id"] = v or None
            if (v := flag(args, "priority")) is not None:
                issue["priority"] = int(v)
            if (v := flag(args, "add-label")) is not None:
                issue.setdefault("labels", []).append(v)
            if (v := flag(args, "remove-label")) is not None:
                issue["labels"] = [x for x in issue.get("labels", []) if x != v]
            if (v := flag(args, "status")) is not None:
                issue["status"] = v
            if (v := flag(args, "append-notes")) is not None:
                issue["notes"] = (issue.get("notes", "") + "\n" + v).strip()
        save(data)
    finally:
        release(fd)
    log(f"end {verb} {ident}")
    if (
        verb == "update"
        and flag(args, "append-notes") is not None
        and os.environ.get("FAKE_BD_CRASH_AFTER_NOTE")
        and not DB.with_suffix(".crashed-note").exists()
    ):
        DB.with_suffix(".crashed-note").write_text("1")
        print("read tcp: connection reset by peer", file=sys.stderr)
        return 1
    return 0


raise SystemExit(main())
