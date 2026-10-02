"""The only place that talks to ``bd``: its public CLI, nothing else."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

SECRET_KEY = re.compile(r"pass|token|secret|key|credential|auth", re.I)
_URL_USERINFO = re.compile(r"(://)[^/@\s]+@")
_KV_SECRET = re.compile(r"(?i)\b([\w-]*(?:pass(?:word)?|token|secret|key)[\w-]*)(\s*[=:]\s*)(\S+)")


class BdError(Exception):
    """A problem running ``bd`` that the user should see verbatim."""


class BdMissing(BdError):
    """``bd`` is not on PATH."""


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def find_bd() -> str | None:
    return shutil.which("bd")


def redact(text: str, env: dict[str, str] | None = None) -> str:
    """Mask credentials in text that may be shown to a human."""
    source = os.environ if env is None else env
    for name, value in source.items():
        if SECRET_KEY.search(name) and len(value) >= 4:
            text = text.replace(value, "***")
    text = _URL_USERINFO.sub(r"\1***@", text)
    return _KV_SECRET.sub(r"\1\2***", text)


def run(args: list[str], timeout: float = 60.0) -> Result:
    """Run ``bd`` with the caller's environment passed through unchanged."""
    exe = find_bd()
    if exe is None:
        raise BdMissing(
            "`bd` was not found on PATH; install Beads first (https://github.com/gastownhall/beads)."
        )
    try:
        done = subprocess.run(
            [exe, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return Result(124, "", f"timed out after {timeout:g}s", timed_out=True)
    except OSError as error:
        raise BdError(f"could not execute bd: {error}") from error
    return Result(done.returncode, done.stdout, done.stderr)


def run_json(args: list[str], timeout: float = 300.0):
    result = run(args, timeout=timeout)
    if result.returncode != 0:
        raise BdError(f"bd {' '.join(args)} failed: {redact(result.stderr.strip() or result.stdout.strip())}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise BdError(f"bd {' '.join(args)} did not return JSON: {error}") from error
