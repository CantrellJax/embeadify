import json
import os
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

from embeadify import bd as bd_module

FAKE = Path(__file__).parent / "fake_bd.py"
# cmd.exe cannot carry multi-line or `=`/`&`/`%` arguments, so on Windows (or with EMBEADIFY_TEST_PYSHIM=1)
# a marker file is found on PATH like a real `bd`, and the subprocess call is redirected to the Python fake.
PYSHIM = os.name == "nt" or bool(os.environ.get("EMBEADIFY_TEST_PYSHIM"))
for name in list(os.environ):
    if name.startswith(("BEADS_", "BD_", "FAKE_BD")):
        os.environ.pop(name)


def demo_issues():
    def issue(ident, status="open", parent=None, priority=2, labels=(), blocks=()):
        return {
            "id": ident,
            "title": f"Synthetic {ident}",
            "status": status,
            "priority": priority,
            "labels": list(labels),
            "parent_id": parent,
            "comment_count": 0,
            "dependencies": [{"issue_id": ident, "depends_on_id": b, "type": "blocks"} for b in blocks],
        }

    return [
        issue("demo-1", "closed"),
        issue("demo-2", parent="demo-1"),
        issue("demo-3", parent="demo-2", priority=3),
        issue("demo-4", labels=["keep"]),
        issue("demo-5"),
        issue("demo-6", blocks=("demo-5",)),
    ]


class Env:
    def __init__(self, root: Path, monkeypatch):
        self.root = root
        self.db = root / "db.json"
        self.bin = root / "bin"
        self.bin.mkdir()
        self.mp = monkeypatch
        self.log = root / "writes.log"
        self.set_issues(demo_issues())
        shim = self.bin / ("bd.cmd" if os.name == "nt" else "bd")
        if PYSHIM:
            shim.write_text("marker: redirected to fake_bd.py by tests/conftest.py\n")
            shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
            real_run = subprocess.run

            def redirected(argv, *args, **kwargs):
                if os.path.normcase(str(argv[0])) == os.path.normcase(str(shim)):
                    argv = [sys.executable, str(FAKE), *argv[1:]]
                return real_run(argv, *args, **kwargs)

            patched = types.SimpleNamespace(**{**vars(subprocess), "run": redirected})
            monkeypatch.setattr(bd_module, "subprocess", patched)
        else:
            shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
            shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", str(self.bin) + os.pathsep + os.environ["PATH"])
        monkeypatch.setenv("FAKE_BD_DB", str(self.db))
        monkeypatch.setenv("FAKE_BD_LOG", str(self.log))
        self.calls_log = root / "calls.jsonl"
        monkeypatch.setenv("FAKE_BD_CALLS", str(self.calls_log))
        monkeypatch.chdir(root)

    def set_issues(self, issues):
        self.db.write_text(json.dumps(issues))

    def issues(self):
        return {i["id"]: i for i in json.loads(self.db.read_text())}

    def writes(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def calls(self):
        """Every write call bd received, as argv lists (reads are not recorded)."""
        if not self.calls_log.exists():
            return []
        return [json.loads(line) for line in self.calls_log.read_text().splitlines()]

    def decisions(self, text, name="d.decisions"):
        path = self.root / name
        path.write_text(text)
        return str(path)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)
