import json
import re
from pathlib import Path

import pytest

from embeadify import bd, decisions, doctor, engine
from embeadify.cli import main


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---- grammar ---------------------------------------------------------------------------------


def test_grammar_parses_every_op_and_ignores_comments():
    ops = decisions.parse_text(
        "# c\n\nclose a-1 done, see #12\nparent a-2 -\nparent a-3 a-4  # why\ndup a-5 a-6\n"
        "priority a-1 P1\nlabel-add a-1 x\nlabel-rm a-1 y\nreopen a-1\nnote a-1 hello world\n"
    )
    assert [o.kind for o in ops] == [
        "close",
        "parent",
        "parent",
        "dup",
        "priority",
        "label-add",
        "label-rm",
        "reopen",
        "note",
    ]
    assert ops[0].arg == "done, see #12"
    assert ops[1].arg == "" and ops[2].arg == "a-4"


def test_grammar_rejects_unfilled_placeholders_and_junk():
    with pytest.raises(decisions.GrammarError) as err:
        decisions.parse_text("parent demo-1 <fill-in>\nbogus demo-1\nclose demo-1\ndup demo-1 a b\n")
    assert len(err.value.errors) == 4


# ---- plan ------------------------------------------------------------------------------------

EXAMPLES = {
    "orphans": {
        "schema_version": 1,
        "report_type": "orphans",
        "dangling_parent": [
            {
                "issue_id": "demo-3",
                "status": "open",
                "title": "T",
                "parent_id": "demo-1",
                "parent_status": "closed",
            }
        ],
    },
    "mentions": {
        "schema_version": 1,
        "report_type": "mentions",
        "claims": [
            {
                "issue_id": "demo-4",
                "related_issue_id": "demo-3",
                "kind": "absorbed-by",
                "source_fields": ["notes"],
                "typed_link": False,
                "issue_title": "A",
                "related_title": "B",
            },
            {
                "issue_id": "demo-5",
                "related_issue_id": "demo-3",
                "kind": "fixed-by",
                "source_fields": ["notes"],
                "typed_link": False,
            },
        ],
    },
    "triage": {
        "schema_version": 1,
        "report_type": "triage",
        "candidates": [
            {
                "kind": "completed-work-echo",
                "issue_id": "demo-1",
                "related_issue_id": "demo-2",
                "similarity": 0.9,
                "what_to_verify": "Verify.",
            }
        ],
    },
}


@pytest.mark.parametrize("kind", ["orphans", "mentions", "triage"])
def test_plan_output_is_fully_commented_out(env, capsys, kind):
    report = env.root / "r.json"
    report.write_text(json.dumps(EXAMPLES[kind]))
    code, out, _ = run(capsys, "plan", str(report), "--kind", kind, "-o", "-")
    assert code == 0
    body = [line for line in out.splitlines() if line.strip()]
    assert body and all(line.startswith("#") for line in body)
    assert decisions.parse_text(out) == []
    if kind == "mentions":
        assert "# dup demo-4 demo-3" in out and "# parent demo-4 demo-3" in out
        assert "no proposal for kind 'fixed-by'" in out
    if kind == "orphans":
        assert "# parent demo-3 <fill-in>" in out
    if kind == "triage":
        assert re.search(r"^# close demo-1 <reason", out, re.M)


def test_plan_refuses_overwrite_and_wrong_kind(env, capsys):
    report = env.root / "r.json"
    report.write_text(json.dumps(EXAMPLES["orphans"]))
    assert run(capsys, "plan", str(report))[0] == 0
    assert run(capsys, "plan", str(report))[0] == 2
    assert run(capsys, "plan", str(report), "--kind", "mentions", "-o", "-")[0] == 2


def test_plan_rules_emit_commented_close_proposals(env, capsys):
    (env.root / ".embeadify.toml").write_text(
        '[[filter]]\nname = "review-residue"\ntitle_regex = "^Synthetic demo-[45]$"\n'
        "max_priority = 2\nrequire_no_dependents = true\nrequire_no_comments = true\n"
        'reason = "review residue: no longer needed"\n'
    )
    report = env.root / "r.json"
    report.write_text(json.dumps(EXAMPLES["orphans"]))
    code, out, _ = run(capsys, "plan", str(report), "-o", "-")
    assert code == 0
    assert "# close demo-4 review residue" in out
    assert "demo-5" not in out.split("rule review-residue")[1]  # demo-6 depends on it
    assert decisions.parse_text(out) == []


# ---- apply: dry run, undo, parallel ------------------------------------------------------------


def test_dry_run_never_calls_a_writer(env, capsys):
    path = env.decisions("close demo-4 done\npriority demo-5 0\nparent demo-5 demo-2\n")
    code, out, _ = run(capsys, "apply", path)
    assert code == 0 and "DRY RUN" in out
    assert "open -> closed" in out and "2 -> 0" in out
    assert env.writes() == []
    assert not list(env.root.glob("embeadify-undo-*"))
    assert env.issues()["demo-4"]["status"] == "open"


def test_apply_writes_undo_first_and_undo_restores_original(env, capsys, monkeypatch):
    before = env.issues()
    path = env.decisions(
        "close demo-4 done\npriority demo-5 0\nparent demo-5 demo-2\nlabel-add demo-5 x\n"
        "label-rm demo-4 keep\ndup demo-3 demo-5\n"
    )
    undo = env.root / "undo.decisions"
    code, out, err = run(capsys, "apply", path, "--apply", "--undo-file", str(undo))
    assert code == 0, out + err
    after = env.issues()
    assert after["demo-4"]["status"] == "closed" and after["demo-5"]["parent_id"] == "demo-2"
    assert after["demo-3"]["status"] == "closed"
    assert undo.exists()
    text = undo.read_text()
    assert "reopen demo-4" in text and "parent demo-5 -" in text and "priority demo-5 2" in text
    assert decisions.parse_text(text)  # itself a valid decisions file
    code, out, err = run(
        capsys, "undo", str(undo), "--apply", "--undo-file", str(env.root / "redo.decisions")
    )
    assert code == 0, out + err
    restored = env.issues()
    for ident in before:
        for key in ("status", "parent_id", "priority"):
            assert restored[ident][key] == before[ident][key], (ident, key)
        assert sorted(restored[ident].get("labels", [])) == sorted(before[ident].get("labels", []))


def test_undo_file_exists_before_first_write(env, capsys, monkeypatch):
    seen = {}
    real = bd.run

    def spy(args, timeout=60.0):
        if args[0] in ("close", "update"):
            seen["undo"] = Path("u.decisions").exists()
        return real(args, timeout)

    monkeypatch.setattr(bd, "run", spy)
    path = env.decisions("close demo-4 done\n")
    assert run(capsys, "apply", path, "--apply", "--undo-file", "u.decisions", "-j", "1")[0] == 0
    assert seen["undo"] is True


def test_parallel_run_is_correct_and_bounded(env, capsys, monkeypatch):
    issues = [dict(i) for i in env.issues().values()]
    for n in range(10, 30):
        issues.append({"id": f"demo-{n}", "title": "x", "status": "open", "priority": 2, "labels": []})
    env.set_issues(issues)
    monkeypatch.setenv("FAKE_BD_SLEEP", "0.15")
    path = env.decisions("".join(f"priority demo-{n} 0\nlabel-add demo-{n} p\n" for n in range(10, 30)))
    code, out, err = run(capsys, "apply", path, "--apply", "-j", "4")
    assert code == 0, out + err
    state = env.issues()
    assert all(
        state[f"demo-{n}"]["priority"] == 0 and state[f"demo-{n}"]["labels"] == ["p"] for n in range(10, 30)
    )
    events = []
    for line in env.writes():
        phase, _verb, _ident, stamp = line.split()
        events.append((float(stamp), 1 if phase == "start" else -1))
    live = peak = 0
    for _, delta in sorted(events):
        live += delta
        peak = max(peak, live)
    assert 2 <= peak <= 4


def test_default_jobs_is_two_and_high_jobs_prints_a_note(env, capsys):
    from embeadify import cli

    assert cli.DEFAULT_JOBS == 2
    path = env.decisions("close demo-4 done\n")
    _, _, err = run(capsys, "apply", path)
    assert "shared beads server" not in err
    _, _, err = run(capsys, "apply", path, "-j", "2")
    assert "shared beads server" not in err
    _, _, err = run(capsys, "apply", path, "-j", "3")
    assert err.count("note: parallel bd writes load the shared beads server;") == 1


def test_jobs_one_is_serial_with_same_undo_drift_and_report(env, capsys, monkeypatch):
    issues = [dict(i) for i in env.issues().values()]
    for n in range(10, 16):
        issues.append({"id": f"demo-{n}", "title": "x", "status": "open", "priority": 2, "labels": []})
    env.set_issues(issues)
    monkeypatch.setenv("FAKE_BD_SLEEP", "0.05")
    monkeypatch.setenv("FAKE_BD_MUTATE_ON_THIRD_LIST", "demo-4:status=closed")
    path = env.decisions("close demo-4 done\n" + "".join(f"priority demo-{n} 0\n" for n in range(10, 16)))
    code, out, err = run(capsys, "apply", path, "--apply", "-j", "1", "--undo-file", "u.decisions", "--json")
    assert code == 1 and "shared beads server" not in err
    payload = json.loads(out)
    assert payload["summary"]["ok"] == 6 and payload["summary"]["skipped"] == 1
    events = []
    for line in env.writes():
        phase, _verb, _ident, stamp = line.split()
        events.append((float(stamp), 1 if phase == "start" else -1))
    live = peak = 0
    for _, delta in sorted(events):
        live += delta
        peak = max(peak, live)
    assert peak == 1
    undo = Path("u.decisions").read_text()
    assert "demo-4" not in undo and "priority demo-10 2" in undo
    code, _, _ = run(capsys, "undo", "u.decisions", "--apply", "-j", "1")
    assert code == 0 and env.issues()["demo-10"]["priority"] == 2


def test_jobs_hard_max(env, capsys):
    path = env.decisions("close demo-4 done\n")
    assert run(capsys, "apply", path, "-j", "9")[0] == 2


# ---- validation ---------------------------------------------------------------------------------


def test_parent_cycle_self_unknown_and_closed_parent_refused(env, capsys):
    cases = {
        "parent demo-2 demo-3\n": "cycle",  # demo-3 is a child of demo-2
        "parent demo-4 demo-4\n": "itself",
        "parent demo-4 demo-99\n": "unknown parent",
        "parent demo-4 demo-1\n": "closed",
        "close demo-99 r\n": "unknown id",
        "close demo-1 r\n": "already closed",
        "close demo-5 r\n": "open dependents",
    }
    for text, needle in cases.items():
        code, out, _ = run(capsys, "apply", env.decisions(text), "--apply")
        assert code == 2 and needle in out, (text, out)
    assert env.writes() == []


def test_overrides_for_closed_parent_and_dependents(env, capsys):
    path = env.decisions("parent demo-4 demo-1\nclose demo-5 r\n")
    code, _, _ = run(capsys, "apply", path, "--apply", "--allow-closed-parent", "--force-dependents")
    assert code == 0
    assert env.issues()["demo-5"]["status"] == "closed"


def test_closing_dependents_first_in_same_file_is_allowed(env, capsys):
    path = env.decisions("close demo-6 r\nclose demo-5 r\n")
    assert run(capsys, "apply", path, "--apply")[0] == 0


def test_noop_is_skipped_not_written(env, capsys):
    path = env.decisions("priority demo-4 2\nlabel-add demo-4 keep\n")
    code, out, _ = run(capsys, "apply", path, "--apply")
    assert code == 0 and env.writes() == [] and "no-op" in out


# ---- drift, failure, guard ----------------------------------------------------------------------


def test_drift_is_skipped_and_reported(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_BD_MUTATE_ON_THIRD_LIST", "demo-4:status=closed")
    path = env.decisions("close demo-4 done\npriority demo-5 0\n")
    code, out, _ = run(capsys, "apply", path, "--apply", "--undo-file", "u.decisions")
    assert code == 1 and "drift on demo-4" in out
    state = env.issues()
    assert state["demo-4"]["status"] == "closed"  # closed by someone else, not by us
    assert "close_reason" not in state["demo-4"]
    assert state["demo-5"]["priority"] == 0
    assert not any("close demo-4" in line for line in env.writes())
    undo = Path("u.decisions").read_text()
    assert "demo-4" not in undo and "priority demo-5 2" in undo


def test_failures_are_reported_with_retry_and_exit_nonzero(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_BD_FAIL", "demo-4")
    path = env.decisions("close demo-4 done\npriority demo-5 0\n")
    code, out, _ = run(capsys, "apply", path, "--apply", "--json")
    payload = json.loads(out)
    assert code == 1
    assert payload["summary"]["failed"] == 1 and payload["summary"]["ok"] == 1
    failed = next(o for o in payload["ops"] if o["outcome"] == "failed")
    assert failed["retry"] == "bd close demo-4 --reason='done'" or failed["retry"].startswith(
        "bd close demo-4"
    )
    assert env.issues()["demo-5"]["priority"] == 0  # batch continued


def test_transient_failure_retries_once(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_BD_FLAKY", "demo-4")
    code, out, _ = run(capsys, "apply", env.decisions("priority demo-4 0\n"), "--apply")
    assert code == 0 and env.issues()["demo-4"]["priority"] == 0


def test_guard_refusal_is_surfaced_not_retried(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_BD_GUARD", "1")
    code, out, _ = run(capsys, "apply", env.decisions("priority demo-4 0\n"), "--apply")
    assert code == 1 and "bd-guard: writes to this database are refused" in out
    assert sum(1 for line in env.writes() if line.startswith("start")) == 1


# ---- environment --------------------------------------------------------------------------------


def test_bd_absent_gives_clear_error(env, capsys, monkeypatch):
    monkeypatch.setenv("PATH", str(env.root / "nowhere"))
    for argv in (["apply", env.decisions("close demo-4 x\n")], ["doctor"]):
        code, out, err = run(capsys, *argv)
        assert code == 2 and "bd" in (out + err) and "not found" in (out + err).lower()


def test_apply_refused_when_bd_cannot_report_target(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_BD_NO_CONTEXT", "1")
    code, _, err = run(capsys, "apply", env.decisions("close demo-4 x\n"), "--apply")
    assert code == 2 and "refusing to write" in err and env.writes() == []


def test_doctor_redacts_secrets(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_BD_SECRET", "hunter2-super-secret")
    monkeypatch.setenv("BEADS_DOLT_PASSWORD", "another-secret-value")
    monkeypatch.setenv("BEADS_DOLT_SERVER_HOST", "dolt.example.test")
    code, out, err = run(capsys, "doctor")
    assert code == 0
    assert "dolt.example.test" in out and "demo" in out
    for secret in ("hunter2-super-secret", "another-secret-value"):
        assert secret not in out + err
    assert "root:" not in out


def test_redact_helper_masks_urls_and_pairs(monkeypatch):
    monkeypatch.setenv("BEADS_DOLT_PASSWORD", "s3cretvalue")
    text = bd.redact("mysql://root:s3cretvalue@host/db password=abc token: xyz")
    assert "s3cretvalue" not in text and "abc" not in text and "xyz" not in text
    assert doctor  # module import sanity


# ---- create op ----------------------------------------------------------------------------------


def test_create_grammar_quoting_defaults_and_errors():
    (op,) = decisions.parse_text(
        'create cand-1 type=bug priority=P1 parent=demo-2 title="two words, quoted"\n'
    )
    assert op.kind == "create" and op.id == "cand-1"
    fields = decisions.create_fields(op.arg)
    assert fields == {"type": "bug", "priority": "1", "parent": "demo-2", "title": "two words, quoted"}
    assert decisions.create_fields("title=x") == {"title": "x", "type": "task", "priority": "2"}
    bad = (
        "create cand-1 type=task\n",  # no title
        'create cand-1 title="unterminated\n',
        "create cand-1 title=x type=weird\n",
        "create cand-1 title=x priority=9\n",
        "create cand-1 title=x title=y\n",
        "create cand-1 title=x colour=red\n",
        "create cand-1 title=<fill-in>\n",
        "create bad/id title=x\n",
        "create cand-1\n",
    )
    for text in bad:
        with pytest.raises(decisions.GrammarError):
            decisions.parse_text(text)


def create_file(env, text='create cand-1 type=task priority=2 parent=demo-2 title="Add a thing"\n'):
    return env.decisions(text)


def test_create_dry_run_writes_nothing_and_shows_the_new_bead(env, capsys):
    code, out, _ = run(capsys, "apply", create_file(env))
    assert code == 0 and "DRY RUN" in out and "(new task P2)" in out
    assert env.writes() == [] and env.calls() == []


def test_create_apply_marks_the_bead_and_undo_closes_it(env, capsys):
    (env.root / "body.txt").write_text("Body line.\nembeadify-candidate: demo-4\n")
    path = create_file(env, 'create cand-1 title="Add a thing" body-file=body.txt parent=demo-2\n')
    undo = env.root / "u.decisions"
    code, out, err = run(capsys, "apply", path, "--apply", "--undo-file", str(undo))
    assert code == 0, out + err
    created = env.issues()["demo-7"]
    assert created["title"] == "Add a thing" and created["parent_id"] == "demo-2"
    lines = created["description"].splitlines()
    assert lines[-1] == "embeadify-candidate: cand-1"
    assert "embeadify-candidate: demo-4" not in created["description"]  # a body cannot forge a marker
    text = undo.read_text()
    assert "close demo-7 embeadify undo: created for cand-1" in text
    code, out, err = run(
        capsys, "undo", str(undo), "--apply", "--undo-file", str(env.root / "redo.decisions")
    )
    assert code == 0, out + err
    assert env.issues()["demo-7"]["status"] == "closed"


def test_create_undo_file_exists_before_the_write_and_says_the_id_is_pending(env, capsys, monkeypatch):
    seen = {}
    real = bd.run

    def spy(args, timeout=60.0):
        if args[0] == "create":
            seen["undo"] = Path("u.decisions").read_text()
        return real(args, timeout)

    monkeypatch.setattr(bd, "run", spy)
    assert run(capsys, "apply", create_file(env), "--apply", "--undo-file", "u.decisions")[0] == 0
    assert "new id not known yet" in seen["undo"] and "embeadify-candidate: cand-1" in seen["undo"]


def test_create_is_idempotent_by_candidate_id(env, capsys):
    path = create_file(env)
    assert run(capsys, "apply", path, "--apply")[0] == 0
    code, out, _ = run(capsys, "apply", path, "--apply", "--undo-file", "second.decisions")
    assert code == 0 and "already created" in out
    assert [c[0] for c in env.calls()] == ["create"]
    twice = create_file(env, "create c-2 title=a\ncreate c-2 title=b\n")
    assert run(capsys, "apply", twice, "--apply", "--undo-file", "third.decisions")[0] == 0
    assert [c[0] for c in env.calls()] == ["create", "create"]


def test_create_refuses_unknown_or_closed_parent_and_missing_body_file(env, capsys):
    cases = {
        "create c-1 title=x parent=demo-99\n": "unknown parent",
        "create c-1 title=x parent=demo-1\n": "closed",
        "create c-1 title=x body-file=nowhere.txt\n": "cannot read body-file",
    }
    for text, needle in cases.items():
        code, out, _ = run(capsys, "apply", env.decisions(text), "--apply")
        assert code == 2 and needle in out, (text, out)
    assert env.calls() == []


def test_create_crash_after_the_write_is_reconciled_not_retried(env, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_BD_CRASH_AFTER_CREATE", "1")
    code, out, err = run(capsys, "apply", create_file(env), "--apply", "--undo-file", "u.decisions")
    assert code == 0, out + err
    assert "reconciled" in out
    assert [c[0] for c in env.calls()] == ["create"]  # one write, no retry
    assert sum(i["title"] == "Add a thing" for i in env.issues().values()) == 1
    assert "close demo-7" in Path("u.decisions").read_text()


def test_create_title_and_argv_are_single_words(env, capsys):
    path = env.decisions('create c-1 title="close demo-4; $(touch pwned) `id`"\n')
    assert run(capsys, "apply", path, "--apply")[0] == 0
    (call,) = env.calls()
    assert call[0] == "create" and call[1].startswith("--title=close demo-4;")
    assert not (env.root / "pwned").exists()
    assert env.issues()["demo-4"]["status"] == "open"


def test_create_argv_passes_no_inherit_labels_iff_a_parent_is_set():
    with_parent = engine.create_argv("t", "task", 2, "demo-2", "d")
    without = engine.create_argv("t", "task", 2, None, "d")
    assert "--no-inherit-labels" in with_parent and "--parent=demo-2" in with_parent
    assert "--no-inherit-labels" not in without and not any(a.startswith("--parent") for a in without)


def test_create_under_a_parent_never_inherits_the_parents_labels(env, capsys):
    issues = [dict(i) for i in env.issues().values()]
    next(i for i in issues if i["id"] == "demo-2")["labels"] = ["theme"]
    env.set_issues(issues)
    path = env.decisions("create c-1 parent=demo-2 title=child\ncreate c-2 title=orphan\n")
    assert run(capsys, "apply", path, "--apply")[0] == 0
    kids = {i["title"]: i for i in env.issues().values() if i["title"] in ("child", "orphan")}
    assert kids["child"]["parent_id"] == "demo-2" and kids["child"]["labels"] == []
    assert kids["orphan"]["labels"] == []
    assert any("--no-inherit-labels" in c for c in env.calls())
