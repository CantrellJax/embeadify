"""`embeadify doctor`: what would be written to, with secrets redacted."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from . import bd

ENV_TARGET = {
    "BEADS_DOLT_SERVER_HOST": "host",
    "BEADS_DOLT_SERVER_PORT": "port",
    "BEADS_DOLT_SERVER_DATABASE": "database",
    "BEADS_DOLT_SERVER_USER": "user",
    "BEADS_DOLT_SERVER_MODE": "mode",
}
CONTEXT_KEY = re.compile(r"host|port|database|^db|path|dir|backend|server|mode|workspace|remote|url", re.I)


@dataclass
class Report:
    bd_path: str | None = None
    version: str = ""
    workspace_ok: bool = False
    target: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def can_write(self) -> bool:
        return not self.problems and bool(self.target)


def _flatten(value, prefix="") -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}{k}."))
    elif isinstance(value, (str, int, float, bool)) and prefix:
        out[prefix[:-1]] = str(value)
    return out


def _safe(key: str, value: str) -> str:
    if bd.SECRET_KEY.search(key):
        return "***"
    return bd.redact(value)


def inspect() -> Report:
    report = Report(bd_path=bd.find_bd())
    if report.bd_path is None:
        report.problems.append("`bd` was not found on PATH")
        return report
    try:
        version = bd.run(["--version"], timeout=30)
        report.version = bd.redact(version.stdout.strip() or version.stderr.strip())
        if version.returncode != 0:
            report.problems.append("`bd --version` failed: " + report.version)
        listing = bd.run(["list", "--all", "--limit", "1", "--json"], timeout=60)
        report.workspace_ok = listing.returncode == 0
        if not report.workspace_ok:
            msg = bd.redact(listing.stderr.strip() or listing.stdout.strip() or "no output")
            report.problems.append("workspace did not resolve: " + " | ".join(msg.splitlines()[:3]))
        context = bd.run(["context", "--json"], timeout=30)
    except bd.BdError as error:
        report.problems.append(str(error))
        return report
    target: dict[str, str] = {}
    if context.returncode == 0:
        try:
            flat = _flatten(json.loads(context.stdout))
        except json.JSONDecodeError:
            flat = {}
        for key, value in sorted(flat.items()):
            if CONTEXT_KEY.search(key) and value != "":
                target[key] = _safe(key, value)
    else:
        report.problems.append("`bd context --json` failed, so bd cannot report its write target")
    for env_name, label in ENV_TARGET.items():
        if os.environ.get(env_name):
            target[f"env.{label}"] = bd.redact(os.environ[env_name])
    if not target and context.returncode == 0:
        report.problems.append("bd reported no write target (no host, database or path)")
    report.target = target
    return report


def target_line(report: Report) -> str:
    return ", ".join(f"{k}={v}" for k, v in sorted(report.target.items())) or "(unknown)"


def render(report: Report) -> str:
    lines = [f"bd: {report.bd_path or 'NOT FOUND'}"]
    if report.version:
        lines.append(f"version: {report.version}")
    lines.append(f"workspace resolves: {'yes' if report.workspace_ok else 'no'}")
    lines.append("write target:")
    for k, v in sorted(report.target.items()):
        lines.append(f"  {k}: {v}")
    if not report.target:
        lines.append("  (unknown)")
    if report.problems:
        lines.append("problems:")
        lines.extend(f"  - {p}" for p in report.problems)
        lines.append("`apply --apply` is refused until these are fixed.")
    else:
        lines.append("ok: `apply --apply` would write to the target above.")
    return "\n".join(lines)
