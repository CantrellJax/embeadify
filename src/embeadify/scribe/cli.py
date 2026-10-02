"""`embeadify scribe ...` commands. Exit codes: 0 ok, 1 something failed, 2 refusal or usage error."""

from __future__ import annotations

import json
import shlex
import sys
from collections import Counter
from pathlib import Path

from .. import bd, doctor
from . import candidate as cand
from . import runner
from . import store as st
from .policy import PolicyError, load

EXIT_OK, EXIT_PARTIAL, EXIT_ERROR = 0, 1, 2


def _err(message: str) -> int:
    print(f"embeadify scribe: {message}", file=sys.stderr)
    return EXIT_ERROR


def _queue(args) -> st.Queue:
    return st.Queue(args.queue_dir)


def _read_candidate_file(path: str) -> dict:
    limit = cand.MAX_FILE_BYTES
    if path == "-":
        raw = sys.stdin.buffer.read(limit + 1)
    else:
        with open(path, "rb") as handle:
            raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise cand.CandidateError([f"candidate file is larger than {limit} bytes"])
    try:
        return cand.validate(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise cand.CandidateError([f"not valid UTF-8 JSON: {error}"]) from error


def cmd_submit(args) -> int:
    try:
        candidate = _read_candidate_file(args.file)
    except OSError as error:
        return _err(f"cannot read {args.file}: {error}")
    except cand.CandidateError as error:
        for line in error.errors:
            print(f"embeadify scribe: {args.file}: {line}", file=sys.stderr)
        return EXIT_ERROR
    try:
        rid, digest, new = _queue(args).submit(candidate)
    except st.Conflict as error:
        return _err(f"conflict: {error}")
    except OSError as error:
        return _err(f"cannot write the queue: {error}")
    state = "queued" if new else "duplicate (same content already queued; nothing new stored)"
    if args.json:
        print(
            json.dumps(
                {
                    "receipt_id": rid,
                    "candidate_id": candidate["candidate_id"],
                    "hash": digest,
                    "state": "queued" if new else "duplicate",
                },
                sort_keys=True,
            )
        )
    else:
        print(f"{state}: {candidate['candidate_id']}  receipt {rid}  hash {digest[:12]}")
    return EXIT_OK


def _state(queue: st.Queue, sub: st.Submission, latest: dict) -> tuple[str, str, str]:
    """(state, action, bead) for one submission."""
    if sub.error:
        return "invalid", "", ""
    decision = queue.decision(sub.receipt_id)
    if decision:
        return (
            "decided",
            decision.get("final_action", ""),
            decision.get("bead_id") or decision.get("target_id") or "",
        )
    entry = latest.get(sub.candidate["candidate_id"])
    if entry and entry.get("hash") == sub.hash:
        plan = entry.get("executor_plan") or {}
        if entry.get("outcome") == "failed":
            return "failed", plan.get("action", ""), ""
        return "shadowed", plan.get("action", ""), entry.get("bead_id") or ""
    return "queued", "", ""


def cmd_status(args) -> int:
    queue, latest = _queue(args), None
    latest = queue.latest_log()
    counts = Counter(_state(queue, s, latest)[0] for s in queue.submissions())
    holder = queue.lock_holder()
    data = {"queue_dir": str(queue.root), "counts": dict(sorted(counts.items())), "run_lock_pid": holder}
    if args.json:
        print(json.dumps(data, sort_keys=True))
    else:
        print(f"queue: {queue.root}")
        for key in ("queued", "shadowed", "failed", "decided", "invalid"):
            print(f"  {key:<9} {counts.get(key, 0)}")
        print(f"run lock: {'held by pid ' + str(holder) if holder else 'free'}")
    return EXIT_OK


def cmd_receipts(args) -> int:
    queue = _queue(args)
    latest = queue.latest_log()
    rows = []
    for sub in queue.submissions():
        state, action, bead = _state(queue, sub, latest)
        rows.append(
            {
                "receipt_id": sub.receipt_id or "-",
                "candidate_id": sub.candidate["candidate_id"] if sub.candidate else sub.path.stem,
                "state": state,
                "action": action,
                "bead": bead,
                "hash": sub.hash[:12],
            }
        )
    if args.action:
        rows = [r for r in rows if r["action"] == args.action]
    if args.json:
        print(json.dumps(rows, sort_keys=True))
    else:
        for r in rows:
            print(
                f"{r['receipt_id']}  {r['candidate_id']:<28} {r['state']:<9} "
                f"{r['action'] or '-':<7} {r['bead'] or '-'}"
            )
        if not rows:
            print("no submissions")
    return EXIT_OK


def cmd_report(args) -> int:
    queue = _queue(args)
    latest = queue.latest_log()
    recommended = Counter()
    final = Counter()
    downgraded = []
    degraded = []
    for entry in latest.values():
        plan = entry.get("executor_plan") or {}
        rec = entry.get("recommendation") or {}
        final[plan.get("action", "?")] += 1
        if rec:
            recommended[rec.get("action", "?")] += 1
        if plan.get("downgraded"):
            downgraded.append(
                {
                    "candidate_id": entry["candidate_id"],
                    "recommended": rec.get("action"),
                    "final": plan.get("action"),
                    "reasons": plan.get("reasons", []),
                }
            )
        if entry.get("degraded"):
            degraded.append({"candidate_id": entry["candidate_id"], "degraded": entry["degraded"]})
    data = {
        "candidates": len(latest),
        "recommended": dict(sorted(recommended.items())),
        "executor": dict(sorted(final.items())),
        "downgraded": downgraded,
        "degraded": degraded,
    }
    if args.json:
        print(json.dumps(data, sort_keys=True))
        return EXIT_OK
    print(f"candidates in the log: {len(latest)}")
    print("recommended: " + (", ".join(f"{k} {v}" for k, v in sorted(recommended.items())) or "none"))
    print("executor:    " + (", ".join(f"{k} {v}" for k, v in sorted(final.items())) or "none"))
    print(f"downgraded ({len(downgraded)}):")
    for d in downgraded:
        print(f"  {d['candidate_id']}: {d['recommended']} -> {d['final']}  [{', '.join(d['reasons'])}]")
    print(f"degraded inputs ({len(degraded)}):")
    for d in degraded:
        print(f"  {d['candidate_id']}: {d['degraded']}")
    return EXIT_OK


def cmd_run(args) -> int:
    queue = _queue(args)
    policy_path = Path(args.policy) if args.policy else (queue.root / "policy.toml")
    try:
        policy = load(policy_path if args.policy or policy_path.exists() else None)
    except PolicyError as error:
        return _err(str(error))
    if args.live and not policy.live:
        return _err(
            "refusing --live: the policy file does not set `live = true` (shadow mode is the default)"
        )
    recommender = tuple(shlex.split(args.recommender)) if args.recommender else policy.recommender_command
    try:
        if args.live:
            report = doctor.inspect()
            if not report.can_write:
                print(doctor.render(report), file=sys.stderr)
                return _err("refusing to write: bd cannot report a usable write target")
            print(f"target: {doctor.target_line(report)}", file=sys.stderr)
        print(
            f"mode: {'LIVE' if args.live else 'SHADOW (nothing is written to the tracker)'};"
            f" policy {policy.policy_version}",
            file=sys.stderr,
        )
        with queue.lock():
            if args.once:
                summary = runner.run_once(
                    queue,
                    policy,
                    live=args.live,
                    recommender=recommender,
                    timeout=args.timeout,
                    limit=args.limit,
                )
            else:
                summary = runner.run_loop(
                    queue,
                    policy,
                    live=args.live,
                    recommender=recommender,
                    timeout=args.timeout,
                    interval=args.interval,
                    limit=args.limit,
                )
    except st.LockHeld as error:
        return _err(str(error))
    except bd.BdError as error:
        return _err(f"{error}; candidates stay queued")
    print(
        f"seen {summary.seen}: applied {summary.applied}, reconciled {summary.reconciled}, "
        f"shadowed {summary.shadowed}, "
        f"failed {summary.failed}, invalid {summary.invalid}, dropped {summary.skipped}"
    )
    for line in summary.details:
        print(f"  {line}")
    return EXIT_PARTIAL if summary.failed or summary.invalid else EXIT_OK


def add_parsers(sub) -> None:
    scribe = sub.add_parser(
        "scribe", help="bead intake: submit candidates, run the scribe (shadow by default)"
    )
    ssub = scribe.add_subparsers(dest="scribe_command", required=True)

    def common(p, json_flag=True):
        p.add_argument("--queue-dir", help=f"queue directory (default {st.DEFAULT_DIR})")
        if json_flag:
            p.add_argument("--json", action="store_true")

    p = ssub.add_parser("submit", help="validate a candidate JSON file and queue it (immutable)")
    p.add_argument("file", help="candidate JSON file, or - for stdin")
    common(p)
    p.set_defaults(func=cmd_submit)

    p = ssub.add_parser("status", help="queue counts and run-lock state")
    common(p)
    p.set_defaults(func=cmd_status)

    p = ssub.add_parser("receipts", help="one line per submission: receipt, state, action, bead")
    common(p)
    p.add_argument("--action", choices=["create", "fold", "dup", "drop"], help="only this action")
    p.set_defaults(func=cmd_receipts)

    p = ssub.add_parser("report", help="counts by action and the candidates the executor downgraded")
    common(p)
    p.set_defaults(func=cmd_report)

    p = ssub.add_parser("run", help="process the queue with ONE executor (shadow unless --live)")
    common(p, json_flag=False)
    p.add_argument("--once", action="store_true", help="one pass, then exit (default polls)")
    p.add_argument(
        "--live", action="store_true", help="write to the tracker; the policy file must also allow it"
    )
    p.add_argument("--policy", help="policy TOML (default <queue-dir>/policy.toml if present)")
    p.add_argument("--recommender", help="external recommender command (JSON in, JSON out); default built-in")
    p.add_argument("--interval", type=float, default=30.0, help="seconds between passes without --once")
    p.add_argument("--limit", type=int, help="process at most N candidates per pass")
    p.add_argument("--timeout", type=float, default=120.0, help="per bd/embead/recommender call, seconds")
    p.set_defaults(func=cmd_run)
