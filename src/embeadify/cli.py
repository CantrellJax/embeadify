"""Command line interface. Exit codes: 0 ok, 1 ops failed or drifted, 2 usage/validation/environment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, bd, decisions, doctor, engine, plan, snapshot
from .scribe import cli as scribe_cli

EXIT_OK, EXIT_PARTIAL, EXIT_ERROR = 0, 1, 2

EPILOG = """exit codes:
  0  success (dry run clean, or every op applied or skipped as a no-op)
  1  one or more ops failed or were skipped because of drift
  2  usage error, invalid decisions, `bd` missing/unusable, or refusal to write
"""


def _err(message: str) -> int:
    print(f"embeadify: {message}", file=sys.stderr)
    return EXIT_ERROR


def _read_ops(path: str) -> list[decisions.Op]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit(_err(f"cannot read {path}: {error}")) from error
    try:
        return decisions.parse_text(text)
    except decisions.GrammarError as error:
        for line in error.errors:
            print(f"embeadify: {path}: {line}", file=sys.stderr)
        raise SystemExit(EXIT_ERROR) from error


def cmd_plan(args) -> int:
    try:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return _err(f"cannot read report {args.report}: {error}")
    rules = snap = None
    rules_path = Path(args.rules) if args.rules else Path(".embeadify.toml")
    if not args.no_rules and (args.rules or rules_path.exists()):
        try:
            rules = plan.load_rules(rules_path)
            if rules:
                if args.snapshot_file:
                    snap = snapshot.parse(json.loads(Path(args.snapshot_file).read_text(encoding="utf-8")))
                else:
                    snap = snapshot.take()
        except (plan.PlanError, bd.BdError, OSError, json.JSONDecodeError) as error:
            return _err(str(error))
    try:
        text = plan.build(report, args.kind, rules, snap)
    except plan.PlanError as error:
        return _err(str(error))
    if args.output == "-":
        sys.stdout.write(text)
        return EXIT_OK
    out = Path(args.output or f"{Path(args.report).stem}.decisions")
    try:
        if args.force:
            engine.replace_text(out, text)
        else:
            engine.write_text_exclusive(out, text)
    except FileExistsError:
        return _err(f"{out} already exists; pass --force to overwrite or -o to choose another path")
    print(f"wrote {out} (every proposal is commented out; uncomment what you approve)", file=sys.stderr)
    return EXIT_OK


def _show_item(item: engine.Item) -> str:
    op = item.op
    change = f"{item.old} -> {item.new}" if item.old or item.new else ""
    extra = f"  [{item.detail}]" if item.detail else ""
    return f"{op.render():<46} {change}{extra}"


def _emit(args, mode_label: str, items, target: str, undo_path, summary: dict) -> None:
    if args.json:
        payload = {
            "mode": mode_label,
            "target": target,
            "undo_file": str(undo_path) if undo_path else None,
            "summary": summary,
            "ops": [
                {
                    "line": i.op.line,
                    "op": i.op.render(),
                    "verdict": i.verdict,
                    "outcome": i.outcome or None,
                    "old": i.old,
                    "new": i.new,
                    "detail": i.detail,
                    "message": i.message,
                    "created_id": i.created_id or None,
                    "retry": i.retry if i.outcome == "failed" else None,
                }
                for i in items
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"target: {target}")
    if mode_label == "dry-run":
        print("DRY RUN: nothing was written. Re-run with --apply to execute.")
    for i in items:
        if i.verdict == "refused":
            print(f"REFUSED  line {i.op.line}: {i.op.render()}  -> {i.detail}")
        elif i.verdict == "noop":
            print(f"no-op    line {i.op.line}: {i.op.render()}  -> {i.detail}")
        elif mode_label == "dry-run":
            print(f"would    line {i.op.line}: {_show_item(i)}")
    if mode_label == "apply":
        print()
        print(f"{'RESULT':<8} {'LINE':>4}  OPERATION")
        for i in items:
            tag = (i.outcome or i.verdict).upper()
            tail = f"  ({i.message})" if i.message else ""
            print(f"{tag:<8} {i.op.line:>4}  {i.op.render()}{tail}")
        failed = [i for i in items if i.outcome == "failed"]
        if failed:
            print("\nretry the failed ops with:")
            for i in failed:
                print(f"  {i.retry}")
    if undo_path:
        print(f"\nundo file: {undo_path}  (replay: embeadify undo {undo_path} --apply)")
    print(
        f"\nsummary: {summary['ok']} ok, {summary['skipped']} skipped, "
        f"{summary['failed']} failed, {summary['refused']} refused, {summary['would_apply']} pending"
    )


DEFAULT_JOBS = 2
JOBS_NOTE = (
    "note: parallel bd writes load the shared beads server; keep -j low for bulk runs "
    "and avoid running heavy DB jobs on the server box at the same time"
)


def _run(args, mode: str) -> int:
    ops = _read_ops(args.file)
    if not ops:
        print("embeadify: no operations found (everything is commented out?)", file=sys.stderr)
        return EXIT_ERROR
    jobs = getattr(args, "jobs", DEFAULT_JOBS)
    if not 1 <= jobs <= engine.MAX_WORKERS:
        return _err(f"-j must be between 1 and {engine.MAX_WORKERS}")
    if jobs > DEFAULT_JOBS:
        print(JOBS_NOTE, file=sys.stderr)
    try:
        if args.apply:
            report = doctor.inspect()
            if not report.can_write:
                print(doctor.render(report), file=sys.stderr)
                return _err("refusing to write: bd cannot report a usable write target")
            target = doctor.target_line(report)
        else:
            report = doctor.inspect()
            target = doctor.target_line(report) if report.target else "(unknown; run `embeadify doctor`)"
        snap = snapshot.take()
    except bd.BdError as error:
        return _err(str(error))
    items = engine.validate(
        ops,
        snap,
        mode=mode,
        allow_closed_parent=getattr(args, "allow_closed_parent", False),
        force_dependents=getattr(args, "force_dependents", False),
    )
    refused = [i for i in items if i.verdict == "refused"]
    pending = [i for i in items if i.verdict == "apply"]

    def summary() -> dict:
        return {
            "ok": sum(i.outcome == "ok" for i in items),
            "skipped": sum(i.outcome == "skipped" for i in items)
            + (0 if args.apply else sum(i.verdict == "noop" for i in items)),
            "failed": sum(i.outcome == "failed" for i in items),
            "refused": len(refused),
            "would_apply": 0 if args.apply else len(pending),
        }

    if refused or not args.apply:
        _emit(args, "dry-run" if not args.apply else "apply-refused", items, target, None, summary())
        if refused:
            print("embeadify: invalid decisions; fix them first (nothing was written)", file=sys.stderr)
            return EXIT_ERROR
        return EXIT_OK
    if not pending:
        _emit(args, "apply", items, target, None, summary())
        return EXIT_OK

    undo_path = Path(args.undo_file) if args.undo_file else engine.default_undo_path()
    try:
        engine.write_text_exclusive(undo_path, engine.undo_text(items, args.file, target))
    except OSError as error:
        return _err(f"cannot write undo file {undo_path}: {error}; nothing was applied")
    try:
        after = snapshot.take()
    except bd.BdError as error:
        return _err(f"{error}; nothing was applied (undo file {undo_path} is unused)")
    drift = engine.detect_drift(items, snap, after)
    engine.execute(items, jobs=jobs, timeout=args.timeout, drift=drift, progress=not args.json)
    done = [i for i in items if i.verdict == "apply" and i.outcome in ("ok", "failed")]
    engine.replace_text(undo_path, engine.undo_text(done, args.file, target))
    result = summary()
    _emit(args, "apply", items, target, undo_path, result)
    return (
        EXIT_PARTIAL
        if result["failed"] or any(i.outcome == "skipped" and i.message.startswith("drift") for i in items)
        else EXIT_OK
    )


def cmd_apply(args) -> int:
    return _run(args, "apply")


def cmd_undo(args) -> int:
    return _run(args, "undo")


def cmd_doctor(args) -> int:
    report = doctor.inspect()
    if args.json:
        print(
            json.dumps(
                {
                    "bd": report.bd_path,
                    "version": report.version,
                    "workspace_ok": report.workspace_ok,
                    "target": report.target,
                    "problems": report.problems,
                    "can_write": report.can_write,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(doctor.render(report))
    return EXIT_OK if report.can_write else EXIT_ERROR


def _add_exec_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--apply", action="store_true", help="actually write (default is a dry run)")
    p.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"parallel bd processes (default {DEFAULT_JOBS}, max {engine.MAX_WORKERS})",
    )
    p.add_argument("--timeout", type=float, default=60.0, help="per-op timeout in seconds (default 60)")
    p.add_argument(
        "--undo-file", help="where to write the undo file (default ./embeadify-undo-<timestamp>.decisions)"
    )
    p.add_argument("--json", action="store_true", help="print the summary as JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="embeadify",
        description="Apply reviewed decisions to a Beads tracker, safely and fast. Dry run unless --apply.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"embeadify {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan", help="write a commented-out decisions template from an emBEADings report")
    p.add_argument("report")
    p.add_argument("--kind", choices=plan.KINDS, help="assert the report type (default: inferred)")
    p.add_argument("-o", "--output", help="output path, or - for stdout (default: <report>.decisions)")
    p.add_argument("--force", action="store_true", help="overwrite an existing output file")
    p.add_argument("--rules", help="rules file (default: ./.embeadify.toml if present)")
    p.add_argument("--no-rules", action="store_true", help="ignore any rules file")
    p.add_argument(
        "--snapshot-file", help="read rules input from a saved `bd list --json` file instead of calling bd"
    )
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("apply", help="apply a decisions file (dry run unless --apply)")
    p.add_argument("file")
    _add_exec_flags(p)
    p.add_argument(
        "--force-dependents", action="store_true", help="allow closing issues that still have open dependents"
    )
    p.add_argument(
        "--allow-closed-parent", action="store_true", help="allow re-parenting under a closed issue"
    )
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("undo", help="replay an undo file (dry run unless --apply)")
    p.add_argument("file")
    _add_exec_flags(p)
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("doctor", help="check bd and show what would be written to")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)
    scribe_cli.add_parsers(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except bd.BdMissing as error:
        return _err(str(error))
    except bd.BdError as error:
        return _err(str(error))
    except KeyboardInterrupt:
        return _err("interrupted")
