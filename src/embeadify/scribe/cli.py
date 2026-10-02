"""`embeadify scribe ...` commands. Exit codes: 0 ok, 1 something failed, 2 refusal or usage error."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import shlex
import sys
from collections import Counter
from pathlib import Path

from .. import bd, doctor, snapshot
from . import candidate as cand
from . import judge, metrics, replay, runner, tune
from . import labels as lb
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
    unplaced = []
    placed = Counter()
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
        if plan.get("placement_rule"):
            placed[plan["placement_rule"]] += 1
        if plan.get("unplaced"):
            unplaced.append(entry["candidate_id"])
        if entry.get("degraded"):
            degraded.append({"candidate_id": entry["candidate_id"], "degraded": entry["degraded"]})
    data = {
        "candidates": len(latest),
        "recommended": dict(sorted(recommended.items())),
        "executor": dict(sorted(final.items())),
        "downgraded": downgraded,
        "degraded": degraded,
        "placement": dict(sorted(placed.items())),
        "unplaced": unplaced,
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
    print("placement:   " + (", ".join(f"{k} {v}" for k, v in sorted(placed.items())) or "none"))
    print(f"unplaced creates ({len(unplaced)}):")
    for cid in unplaced:
        print(f"  {cid}")
    print(f"degraded inputs ({len(degraded)}):")
    for d in degraded:
        print(f"  {d['candidate_id']}: {d['degraded']}")
    return EXIT_OK


def cmd_run(args) -> int:
    queue = _queue(args)
    policy_path = Path(args.policy) if args.policy else (queue.root / "policy.toml")
    try:
        policy = _with_budget(load(policy_path if args.policy or policy_path.exists() else None), args)
    except PolicyError as error:
        return _err(str(error))
    if args.live and not policy.live:
        return _err(
            "refusing --live: the policy file does not set `live = true` (shadow mode is the default)"
        )
    recommender = (
        tuple(shlex.split(args.recommender, posix=os.name != "nt"))
        if args.recommender
        else policy.recommender_command
    )
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
                    jobs=args.recommender_jobs,
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
                    jobs=args.recommender_jobs,
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
    if line := runner.budget_line(summary.timing, policy):
        print(line)
    if args.timing:
        print(summary.timing.line())
    return EXIT_PARTIAL if summary.failed or summary.invalid else EXIT_OK


def _policy(args, queue: st.Queue):
    path = Path(args.policy) if args.policy else (queue.root / "policy.toml")
    return load(path if args.policy or path.exists() else None)


def _recommender(args, policy) -> tuple[str, ...]:
    if args.recommender:
        return tuple(shlex.split(args.recommender, posix=os.name != "nt"))
    return policy.recommender_command


def _progress(n: int, total: int, elapsed: float) -> None:
    """A live progress line on stderr (stdout stays the report, and stays valid for --json)."""
    print(f"replayed {n}/{total} ({elapsed:.0f}s elapsed)", file=sys.stderr, flush=True)


def _jobs(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if not 1 <= n <= runner.MAX_JOBS:
        raise argparse.ArgumentTypeError(f"must be from 1 to {runner.MAX_JOBS}")
    return n


def _with_budget(policy, args):
    over = {}
    if args.max_llm_calls is not None:
        over["max_llm_calls"] = args.max_llm_calls
    if args.max_llm_tokens is not None:
        over["max_llm_tokens"] = args.max_llm_tokens
    return dataclasses.replace(policy, **over) if over else policy


def _nonneg(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
    if n < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return n


def _add_speed_flags(p) -> None:
    p.add_argument(
        "--max-llm-calls",
        type=_nonneg,
        metavar="N",
        help="model calls per pass; past it candidates use the built-in recommender (default: policy, 40)",
    )
    p.add_argument(
        "--max-llm-tokens",
        type=_nonneg,
        metavar="N",
        help="model tokens (in + out, as the backend reports them) per pass (default: policy, 250000)",
    )
    p.add_argument(
        "--recommender-jobs",
        type=_jobs,
        default=runner.DEFAULT_JOBS,
        metavar="N",
        help=f"recommender calls in flight at once, 1-{runner.MAX_JOBS} (default {runner.DEFAULT_JOBS};"
        " 1 = one at a time). Decisions and writes stay in candidate order",
    )
    p.add_argument("--timing", action="store_true", help="print where the time went")


def cmd_replay(args) -> int:
    queue = _queue(args)
    try:
        policy = _with_budget(_policy(args, queue), args)
        since = replay.parse_since(args.since) if args.since else None
        ids = replay.read_ids_file(args.ids_file) if args.ids_file else None
    except (PolicyError, ValueError, OSError) as error:
        return _err(str(error))
    print(
        f"mode: SHADOW REPLAY (nothing is written to the tracker or queued); policy {policy.policy_version}",
        file=sys.stderr,
    )
    try:
        raw = bd.run_json(snapshot.LIST_ARGS)  # the ONE tracker read
    except bd.BdError as error:
        return _err(str(error))
    summary = replay.replay(
        queue,
        policy,
        raw,
        since=since,
        ids=ids,
        limit=args.limit,
        again=args.again,
        include_ephemeral=args.include_ephemeral,
        include_duplicates=args.include_duplicates,
        recommender=_recommender(args, policy),
        neighbor_limit=args.neighbor_limit,
        timeout=args.timeout,
        jobs=args.recommender_jobs,
        progress=_progress,
    )
    data = {
        "selected": summary.selected,
        "replayed": summary.replayed,
        "already_replayed": summary.already,
        "skipped": dict(sorted(summary.skipped.items())),
        "degraded": summary.degraded,
        "details": summary.details,
        "budget_skipped": summary.timing.budget_skipped,
    }
    if args.timing:
        data["timing"] = summary.timing.to_dict()
    if args.json:
        print(json.dumps(data, sort_keys=True))
    else:
        skipped = ", ".join(f"{k} {v}" for k, v in sorted(summary.skipped.items())) or "none"
        print(
            f"selected {summary.selected}: replayed {summary.replayed}, already replayed {summary.already}"
            f" (use --again), skipped: {skipped}, degraded matcher {summary.degraded}"
        )
        for line in summary.details:
            print(f"  {line}")
        if line := runner.budget_line(summary.timing, policy):
            print(line)
        if args.timing:
            print(summary.timing.line())
    return EXIT_OK


def cmd_judge_pack(args) -> int:
    queue = _queue(args)
    try:
        rows = lb.since_filter(list(lb.latest_rows(queue).values()), args.since)
    except lb.LabelError as error:
        return _err(str(error))
    if not args.include_labeled:
        done = lb.current_labels(queue)
        rows = [r for r in rows if lb.key(r) not in done]
    rows = [r for r in rows if r.get("candidate") or r.get("replay")]
    pack = judge.build(rows, args.n, args.seed, queue)
    text = json.dumps(pack, indent=2, sort_keys=True) if args.json else judge.render_markdown(pack)
    if args.out:
        try:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
        except OSError as error:
            return _err(f"cannot write {args.out}: {error}")
        print(f"wrote {len(pack['items'])} items to {args.out}")
    else:
        print(text)
    return EXIT_OK


def cmd_label(args) -> int:
    queue = _queue(args)
    rows = [r for k, r in lb.latest_rows(queue).items() if k[0] == args.candidate_id]
    if args.policy_version is not None:
        rows = [r for r in rows if r.get("policy_version") == args.policy_version]
    if not rows:
        return _err(
            f"no logged decision for {args.candidate_id}"
            + (f" under policy_version {args.policy_version!r}" if args.policy_version is not None else "")
        )
    row = sorted(rows, key=lambda r: r.get("ts", ""))[-1]  # the newest decision
    try:
        lb.validate(row, args.verdict, args.of, args.better)
        if args.of or args.better:
            lb.check_ids(row, args.of, args.better, replay.raw_items(bd.run_json(snapshot.LIST_ARGS)))
    except lb.LabelError as error:
        return _err(str(error))
    except bd.BdError as error:
        return _err(f"cannot check the ids against the tracker: {error}")
    entry = lb.append(queue, row, args.verdict, of=args.of, better=args.better, note=args.note, by=args.by)
    print(
        f"labeled {entry['candidate_id']} ({entry['policy_version'] or 'no policy_version'},"
        f" decision {entry['decision_action']}): {entry['verdict']}"
    )
    return EXIT_OK


def cmd_metrics(args) -> int:
    queue = _queue(args)
    try:
        rows = lb.since_filter(list(lb.latest_rows(queue).values()), args.since)
    except lb.LabelError as error:
        return _err(str(error))
    data = metrics.compute(rows, lb.current_labels(queue))
    print(json.dumps(data, sort_keys=True) if args.json else metrics.render(data, args.by_version))
    return EXIT_OK


def cmd_tune(args) -> int:
    queue = _queue(args)
    try:
        base = _policy(args, queue)
    except PolicyError as error:
        return _err(str(error))
    data = tune.run(lb.latest_rows(queue), lb.current_labels(queue), base, args.min_labels)
    if args.json:
        data["policy_snippet"] = (
            tune.snippet(data["recommendation"]["setting"]) if data["recommendation"] else None
        )
        print(json.dumps(data, sort_keys=True))
    else:
        print(tune.render(data))
    return EXIT_OK


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
    _add_speed_flags(p)
    p.set_defaults(func=cmd_run)

    p = ssub.add_parser(
        "replay", help="judge existing beads as fresh submissions, SHADOW only (never writes the tracker)"
    )
    common(p)
    when = p.add_mutually_exclusive_group()
    when.add_argument("--since", help="only beads created on or after this UTC date (YYYY-MM-DD)")
    when.add_argument("--ids-file", help="file of bead ids, one per line (# comments allowed)")
    p.add_argument("--limit", type=int, help="replay at most N beads, oldest first")
    p.add_argument(
        "--again", action="store_true", help="replay beads already replayed under this policy_version"
    )
    p.add_argument("--include-ephemeral", action="store_true")
    p.add_argument("--include-duplicates", action="store_true", help="include beads closed as duplicates")
    p.add_argument("--policy", help="policy TOML (default <queue-dir>/policy.toml if present)")
    p.add_argument("--recommender", help="external recommender command; default built-in")
    p.add_argument(
        "--neighbor-limit", type=int, default=10, help="neighbors kept per bead after the temporal filter"
    )
    p.add_argument("--timeout", type=float, default=120.0)
    _add_speed_flags(p)
    p.set_defaults(func=cmd_replay)

    p = ssub.add_parser("judge-pack", help="a stratified sample of shadow decisions for an adjudicator")
    common(p)
    p.add_argument("--n", type=int, default=20)
    p.add_argument("--since", help="only decisions logged on or after YYYY-MM-DD")
    p.add_argument("--seed", default="0", help="same seed + same log = same pack")
    p.add_argument("--include-labeled", action="store_true", help="also sample already-labeled decisions")
    p.add_argument("--out", help="write the pack to this file instead of stdout")
    p.set_defaults(func=cmd_judge_pack)

    p = ssub.add_parser("label", help="append one adjudicator verdict to labels.jsonl (immutable)")
    common(p, json_flag=False)
    p.add_argument("candidate_id")
    p.add_argument("verdict", choices=list(lb.VERDICTS), metavar="VERDICT", help=", ".join(lb.VERDICTS))
    p.add_argument("--of", help="should_have_been_dup: the live bead that already covers it")
    p.add_argument("--better", help="bad_placement: the parent it belongs under")
    p.add_argument("--note", default="")
    p.add_argument("--by", default="", help="who judged (default: your user name)")
    p.add_argument("--policy-version", help="the decision's policy_version (default: the newest decision)")
    p.set_defaults(func=cmd_label)

    p = ssub.add_parser("metrics", help="decision mix, agreement with the filer, and label-based precision")
    common(p)
    p.add_argument("--since", help="only decisions logged on or after YYYY-MM-DD")
    p.add_argument("--by-version", action="store_true", help="break the table down by policy_version")
    p.set_defaults(func=cmd_metrics)

    p = ssub.add_parser("tune", help="offline threshold search over labeled decisions; never edits policy")
    common(p)
    p.add_argument("--min-labels", type=int, default=tune.DEFAULT_MIN_LABELS)
    p.add_argument("--policy", help="base policy TOML (default <queue-dir>/policy.toml if present)")
    p.set_defaults(func=cmd_tune)
