"""Owner-routing and never-drop guards, question routing, self-trigger rejection: synthetic and offline.

Every guard is enforced by the deterministic executor; the recommender below always says dup/fold/drop with
confidence 0.99 and the matcher always reports similarity 0.99, and the executor must still create or route.
"""

import json
from pathlib import Path

import pytest
from conftest import demo_issues
from test_scribe import Scribe, neighbor

from embeadify import llm_recommend as llm
from embeadify import snapshot
from embeadify.cli import main
from embeadify.scribe import candidate as cand
from embeadify.scribe import executor, guards, tune
from embeadify.scribe import recommend as rc
from embeadify.scribe import store as st
from embeadify.scribe.policy import Policy, PolicyError, load

SRC = Path(__file__).parent.parent / "src" / "embeadify"
CLOSE = 'Jackson ruled 2026-09-20: "this exact finding is resolved and verified" and moved on.'
QUOTE = 'demo-9 closed. Jackson 2026-09-20: "this exact finding is resolved and verified"'


def issue(ident, status="open", **over):
    item = {
        "id": ident,
        "title": f"Synthetic {ident}",
        "status": status,
        "issue_type": "task",
        "priority": 2,
        "labels": [],
        "parent_id": None,
        "comment_count": 0,
        "dependencies": [],
    }
    item.update(over)
    return item


def world():
    return demo_issues() + [
        issue("g-plain"),
        issue("g-sens", labels=["security"]),
        issue("g-money", title="Refund rounding bug in billing"),
        issue("g-claimed", "in_progress", assignee="farmer-2"),
        issue("g-owner-run", labels=["owner-run"]),
        issue("g-owner-dec", labels=["owner-decision"]),
        issue("g-ask", labels=["ask:owner"]),
        issue("g-human", labels=["human"]),
        issue("g-questions", labels=["questions"]),
        issue("g-clay", labels=["needs-clay"]),
        issue("g-night", labels=["needs-night-ruling"]),
        issue("g-decision", issue_type="decision"),
        issue("g-hold1", description="Do this later. Close only on prod evidence."),
        issue("g-hold2", notes="awaiting   schema_migrations"),
        issue("g-qtitle", title="QUESTION: which window?"),
        issue("g-closed", "closed"),
        issue("g-summary", metadata={"owner_summary": "o" * 600}),
        issue("g-epic", issue_type="epic"),
    ]


@pytest.fixture
def sg(env, monkeypatch):
    sx = Scribe(env, monkeypatch)
    env.set_issues(world())
    return sx


def created(sg):
    return [i for i in sg.env.issues().values() if i["id"].startswith("demo-") and int(i["id"][5:]) > 6]


def go(sg, table, recs, *, live=True, policy=None, cands=None, **over):
    """Submit one candidate per id in `recs` (or `cands`), run one pass with the fake recommender."""
    path = sg.env.root / "nb.json"
    path.write_text(json.dumps(table))
    sg.mp.setenv("FAKE_EMBEAD_FIXTURE", str(path))
    sg.recs(recs)
    sg.write_policy(recommender=True, **(policy or {}))
    for cid in recs:
        c = (cands or {}).get(cid) or sg.candidate(cid, **over)
        assert sg.submit(c) == 0
    return sg.run(live=live)


def dup(target, *, action="dup", ev=None, **kw):
    ev = ev or [f"{target} already tracks exactly this finding"]
    return {"action": action, "target_id": target, "confidence": 0.99, "evidence": ev,
            "acceptance_covered": True, **kw}  # fmt: skip


def forced(sg, cid):
    receipt = sg.decisions()[cid]
    assert receipt["final_action"] == "create", receipt
    return receipt


# ---- A: never fold / drop / dup --------------------------------------------------------------------

TARGETS = [
    ("g-sens", "sensitive_target"),
    ("g-money", "sensitive_target"),
    ("g-claimed", "claimed_target"),
    ("g-owner-run", "owner_held_target"),
    ("g-owner-dec", "owner_held_target"),
    ("g-ask", "owner_held_target"),
    ("g-human", "owner_held_target"),
    ("g-questions", "owner_held_target"),
    ("g-clay", "owner_held_target"),
    ("g-night", "owner_held_target"),
    ("g-decision", "owner_held_target"),
    ("g-hold1", "close_on_evidence_target"),
    ("g-hold2", "close_on_evidence_target"),
]


@pytest.mark.parametrize("action", ["dup", "fold", "drop"])
@pytest.mark.parametrize(("target", "guard"), TARGETS)
def test_a_guarded_target_is_only_ever_created_against(sg, monkeypatch, action, target, guard):
    assert go(sg, {"a-1": [neighbor(target, 0.99)]}, {"a-1": dup(target, action=action)}) == 0
    receipt = forced(sg, "a-1")
    assert guard in receipt["guards"] and f"guard:{guard}" in receipt["reasons"]
    assert sg.verbs() == ["create"]  # no note, no close: nothing was suppressed
    assert "embeadify-provenance" not in (sg.env.issues()[target].get("notes") or "")
    assert len(created(sg)) == 1
    assert sg.log()[-1]["guards"] == receipt["guards"]  # the log row counts it too


def test_the_same_recommendation_on_a_plain_target_is_not_blocked(sg, monkeypatch):
    assert go(sg, {"a-1": [neighbor("g-plain", 0.99)]}, {"a-1": dup("g-plain")}) == 0
    receipt = sg.decisions()["a-1"]
    assert receipt["final_action"] == "dup" and not receipt.get("guards")
    assert sg.verbs() == ["update"]


@pytest.mark.parametrize(
    "over",
    [
        {"title": "Prod data wrongly shown to residents"},
        {"body": "The published schedule changed under a resident."},
        {"body": "Refund amounts are off by a cent."},
        {"title": "Leaked password in a log line"},
        {"labels": ["privacy"]},
        {"labels": ["Billing"]},
    ],
)
@pytest.mark.parametrize("action", ["dup", "fold", "drop"])
def test_a_sensitive_candidate_is_only_ever_created(sg, monkeypatch, action, over):
    assert go(sg, {"a-1": [neighbor("g-plain", 0.99)]}, {"a-1": dup("g-plain", action=action)}, **over) == 0
    assert "sensitive_candidate" in forced(sg, "a-1")["guards"]
    assert sg.verbs() == ["create"]


def test_a_candidate_that_answers_the_target_is_not_folded_away(sg, monkeypatch):
    ev = ["g-plain: this candidate answers the open question on that bead"]
    assert go(sg, {"a-1": [neighbor("g-plain", 0.99)]}, {"a-1": dup("g-plain", ev=ev)}) == 0
    assert "answer_only" in forced(sg, "a-1")["guards"]
    assert go(sg, {"a-2": [neighbor("g-qtitle", 0.99)]}, {"a-2": dup("g-qtitle")}) == 0
    assert "answer_only" in forced(sg, "a-2")["guards"]


def test_guard_lists_are_policy_configurable_and_fail_closed(sg, monkeypatch):
    # a project keyword list replaces the default; "widget" now fires, "prod" no longer does
    policy = {"sensitive_keywords": '["widget"]', "owner_labels": '["keep"]', "hold_phrases": '["wait"]'}
    table = {
        "p-1": [neighbor("g-plain", 0.99)],
        "p-2": [neighbor("g-plain", 0.99)],
        "p-3": [neighbor("demo-4", 0.99)],
    }
    recs = {"p-1": dup("g-plain"), "p-2": dup("g-plain"), "p-3": dup("demo-4")}
    cands = {
        "p-1": sg.candidate("p-1", body="the synthetic widget broke"),
        "p-2": sg.candidate("p-2", title="Prod data is wrong", body="no keyword here"),
    }
    assert go(sg, table, recs, policy=policy, cands=cands) == 0
    got = {cid: d["final_action"] for cid, d in sg.decisions().items()}
    assert got == {"p-1": "create", "p-2": "dup", "p-3": "create"}  # demo-4 carries the label `keep`
    assert "owner_held_target" in sg.decisions()["p-3"]["guards"]


def test_policy_rejects_a_bad_guard_setting(tmp_path):
    path = tmp_path / "p.toml"
    path.write_text('[scribe]\nowner_labels = "owner-run"\n')
    with pytest.raises(PolicyError):
        load(path)
    path.write_text('[scribe]\nchiefs_questions_epic = "bad id"\n')
    with pytest.raises(PolicyError):
        load(path)


# ---- B: closed neighbors -----------------------------------------------------------------------------


def closed_case(sg, evidence, rec_evidence, action="dup"):
    table = {"b-1": [neighbor("g-closed", 0.99, closed=True, evidence=evidence)]}
    assert go(sg, table, {"b-1": dup("g-closed", action=action, ev=rec_evidence)}) == 0
    return sg.decisions()["b-1"]


def test_a_closed_neighbor_with_a_dated_owner_quote_is_a_basis(sg, monkeypatch):
    receipt = closed_case(sg, CLOSE, [QUOTE])
    assert receipt["final_action"] == "dup" and not receipt.get("guards")
    assert sg.verbs() == ["update"]


def test_a_closed_neighbor_with_a_dated_owner_quote_allows_a_drop(sg, monkeypatch):
    receipt = closed_case(sg, CLOSE, [QUOTE.replace("demo-9", "g-closed")], action="drop")
    assert receipt["final_action"] == "drop" and sg.verbs() == []


@pytest.mark.parametrize(
    ("close", "ev", "why"),
    [
        (CLOSE, ["g-closed is fixed and merged"], "no quote at all"),
        ("Merged PR #12 names g-closed", ["Merged PR #12 names the bead g-closed"], "merged PR"),
        ("Absorbed by demo-4", ["absorbed by demo-4 per a coordinator"], "absorbed by X"),
        (CLOSE, ['Jackson: "this exact finding is resolved and verified"'], "no date"),
        (CLOSE, ['2026-09-20: "this exact finding is resolved and verified"'], "no named owner"),
        (CLOSE, ["2026-09-20 Jackson: this exact finding is resolved and verified"], "not quoted"),
        (CLOSE, ['2026-09-20 Jackson: "a paraphrase that is not in the close reason"'], "not verbatim"),
        (CLOSE, ['2026-09-20 Jackson: "short"'], "quote too short"),
        (
            'Coordinator note 2026-09-20: "this exact finding is resolved and verified"',
            ['2026-09-20 Pat: "this exact finding is resolved and verified"'],
            "not an owner",
        ),
        (CLOSE + " Superseded by a later ruling.", [QUOTE], "later Superseded marker"),
        ("", [QUOTE], "unreadable or missing source"),
    ],
)
def test_a_closed_neighbor_without_a_valid_quote_is_created(sg, monkeypatch, close, ev, why):
    receipt = closed_case(sg, close or None, ev)
    assert receipt["final_action"] == "create", why
    assert "closed_neighbor_quote" in receipt["guards"], why
    assert sg.verbs() == ["create"]


def test_a_superseded_marker_in_the_targets_notes_blocks_the_quote(sg, monkeypatch):
    items = world()
    next(i for i in items if i["id"] == "g-closed")["notes"] = "Superseded: reopened as demo-5"
    sg.env.set_issues(items)
    assert closed_case(sg, CLOSE, [QUOTE])["final_action"] == "create"


def test_a_quote_before_the_superseded_text_is_what_blocks_not_text_in_the_quote(sg, monkeypatch):
    # "Superseded" inside the quoted span is the owner's own words and stays valid only if nothing follows
    close = 'Clay 2026-09-20: "not superseded, this is final and verified"'
    ev = ['2026-09-20 Clay: "not superseded, this is final and verified"']
    assert closed_case(sg, close, ev)["final_action"] == "dup"


# ---- C: question kinds ----------------------------------------------------------------------------------


def chief(cid="q-1", **over):
    return {
        "title": "QUESTION: do chiefs want a Friday cut-off?",
        "labels": ["questions"],
        "kind": None,
        **over,
    }


def qcand(sg, cid, **over):
    c = sg.candidate(cid, **over)
    return {k: v for k, v in c.items() if v is not None}


SEARCHED = {"already_searched": True, "searched_sources": ["rulings docs", "bead comments", "closed beads"]}
CREATE = {"action": "create", "confidence": 0.5, "evidence": []}


def test_kind_is_derived_or_explicit():
    base = {
        "candidate_id": "k",
        "title": "t",
        "body": "",
        "type": "task",
        "priority": 2,
        "source": {"agent": "a"},
    }
    assert cand.kind_of({**base, "title": "QUESTION: x"}) == "question_owner"
    assert cand.kind_of({**base, "title": "Question for a chief"}) == "question_owner"
    assert cand.kind_of({**base, "labels": ["questions"]}) == "question_chief"
    assert cand.kind_of({**base, "labels": ["Ask:Owner"]}) == "question_owner"
    assert cand.kind_of({**base, "type": "bug"}) == "bug"
    assert cand.kind_of({**base, "kind": "decision"}) == "decision"
    assert cand.kind_of(base) == "task"
    with pytest.raises(cand.CandidateError):
        cand.validate({**base, "body": "", "kind": "chore"})
    assert cand.validate({**base, "kind": "question_chief"})["kind"] == "question_chief"


def test_a_chief_question_with_an_epic_is_a_note_never_a_task(sg, monkeypatch):
    c = qcand(sg, "q-1", title="QUESTION: Friday cut-off?", labels=["questions"])
    assert (
        go(
            sg,
            {},
            {"q-1": {**CREATE, **SEARCHED}},
            cands={"q-1": c},
            policy={"chiefs_questions_epic": '"g-epic"'},
        )
        == 0
    )
    receipt = sg.decisions()["q-1"]
    assert receipt["final_action"] == "fold" and receipt["target_id"] == "g-epic"
    assert "question_chief_note_on_epic" in receipt["reasons"]
    assert sg.verbs() == ["update"] and created(sg) == []
    assert "embeadify-provenance: q-1" in sg.env.issues()["g-epic"]["notes"]
    assert not receipt.get("route")  # chiefs are not the owners' queue


def test_a_chief_question_without_an_epic_is_a_decision_bead_that_never_blocks(sg, monkeypatch):
    c = qcand(sg, "q-1", title="QUESTION: Friday cut-off?", labels=["questions"])
    assert go(sg, {}, {"q-1": {**CREATE, **SEARCHED}}, cands={"q-1": c}) == 0
    (bead,) = created(sg)
    assert bead["issue_type"] == "decision" and bead["labels"] == ["questions"]
    assert not bead["dependencies"] and "unsearched" not in bead["labels"]
    assert not sg.decisions()["q-1"].get("route")


def test_an_unsearched_chief_question_is_created_under_the_epic_and_flagged(sg, monkeypatch):
    c = qcand(sg, "q-1", title="QUESTION: Friday cut-off?", labels=["questions"])
    assert go(sg, {}, {"q-1": CREATE}, cands={"q-1": c}, policy={"chiefs_questions_epic": '"g-epic"'}) == 0
    (bead,) = created(sg)
    assert bead["issue_type"] == "decision" and bead["parent_id"] == "g-epic"
    assert sorted(bead["labels"]) == ["questions", "unsearched"]
    assert sg.decisions()["q-1"]["flags"] == ["unsearched"]


def test_a_sensitive_chief_question_is_never_noted_away(sg, monkeypatch):
    c = qcand(sg, "q-1", title="QUESTION: who may see prod data?", labels=["questions"])
    assert (
        go(
            sg,
            {},
            {"q-1": {**CREATE, **SEARCHED}},
            cands={"q-1": c},
            policy={"chiefs_questions_epic": '"g-epic"'},
        )
        == 0
    )
    assert sg.decisions()["q-1"]["final_action"] == "create" and len(created(sg)) == 1


def test_a_chief_epic_that_is_not_live_falls_back_to_a_decision_bead(sg, monkeypatch):
    c = qcand(sg, "q-1", title="QUESTION: Friday cut-off?", labels=["questions"])
    assert (
        go(
            sg,
            {},
            {"q-1": {**CREATE, **SEARCHED}},
            cands={"q-1": c},
            policy={"chiefs_questions_epic": '"g-closed"'},
        )
        == 0
    )
    assert sg.decisions()["q-1"]["final_action"] == "create"
    assert created(sg)[0]["issue_type"] == "decision"


def test_an_owner_question_is_a_decision_bead_with_a_route_for_the_hub(sg, monkeypatch, capsys):
    c = qcand(sg, "q-1", title="QUESTION: may we publish early?", labels=["ask:owner", "theme:calendar"])
    assert go(sg, {}, {"q-1": {**CREATE, **SEARCHED}}, cands={"q-1": c}) == 0
    (bead,) = created(sg)
    assert bead["issue_type"] == "decision" and "ask:owner" in bead["labels"]
    receipt = sg.decisions()["q-1"]
    assert receipt["route"] == {
        "route": "owner_queue",
        "hub_brief": {"title": "QUESTION: may we publish early?", "ref": bead["id"], "theme": "calendar"},
    }
    # the executor only records the route: no hub/ssh/network call, no extra bd verb
    assert sg.verbs() == ["create"]
    capsys.readouterr()
    assert main(["scribe", "routes", "--queue-dir", str(sg.queue), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["candidate_id"], r["pending"], r["hub_brief"]["ref"]) for r in rows] == [
        ("q-1", True, bead["id"])
    ]
    assert main(["scribe", "routes", "--queue-dir", str(sg.queue), "--ack", rows[0]["receipt_id"]]) == 0
    capsys.readouterr()
    assert main(["scribe", "routes", "--queue-dir", str(sg.queue), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert main(["scribe", "routes", "--queue-dir", str(sg.queue), "--all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["pending"] is False
    assert main(["scribe", "routes", "--queue-dir", str(sg.queue), "--ack", "rcpt-nope"]) == 2


def test_a_decision_kind_candidate_routes_like_an_owner_question(sg, monkeypatch):
    c = qcand(sg, "q-1", title="Pick the rotation order", kind="decision")
    assert go(sg, {}, {"q-1": {**CREATE, **SEARCHED}}, cands={"q-1": c}) == 0
    assert created(sg)[0]["issue_type"] == "decision"
    assert sg.decisions()["q-1"]["route"]["hub_brief"]["theme"] is None


def test_routes_in_shadow_have_no_ref_yet_and_nothing_is_written(sg, monkeypatch):
    c = qcand(sg, "q-1", title="QUESTION: may we publish early?")
    assert go(sg, {}, {"q-1": {**CREATE, **SEARCHED}}, cands={"q-1": c}, live=False) == 0
    assert sg.verbs() == [] and sg.decisions() == {}
    plan = sg.log()[-1]["executor_plan"]
    assert plan["route"]["hub_brief"]["ref"] is None and plan["kind"] == "question_owner"


def test_a_question_without_a_search_attestation_is_created_flagged_never_dropped(sg, monkeypatch):
    c = qcand(sg, "q-1", title="QUESTION: may we publish early?")
    ev = [QUOTE.replace("demo-9", "g-closed")]
    table = {"q-1": [neighbor("g-closed", 0.99, closed=True, evidence=CLOSE)]}
    # even a perfectly quoted closed neighbor cannot drop it: nobody attested a search
    assert go(sg, table, {"q-1": dup("g-closed", action="drop", ev=ev)}, cands={"q-1": c}) == 0
    receipt = forced(sg, "q-1")
    assert "unsearched_question" in receipt["guards"] and receipt["flags"] == ["unsearched"]
    (bead,) = created(sg)
    assert bead["issue_type"] == "decision" and "unsearched" in bead["labels"]
    assert "Unsearched:" in bead["description"]
    assert receipt["route"]["hub_brief"]["ref"] == bead["id"]


def test_a_searched_question_may_still_dup_but_never_onto_an_unquoted_closed_bead(sg, monkeypatch):
    c1 = qcand(sg, "q-1", title="QUESTION: may we publish early?")
    c2 = qcand(sg, "q-2", title="QUESTION: may we publish late?")
    table = {
        "q-1": [neighbor("g-plain", 0.99)],
        "q-2": [neighbor("g-closed", 0.99, closed=True, evidence="Merged PR names the bead")],
    }
    recs = {
        "q-1": {**dup("g-plain"), **SEARCHED},
        "q-2": {**dup("g-closed", ev=["g-closed answered by a merged PR"]), **SEARCHED},
    }
    assert go(sg, table, recs, cands={"q-1": c1, "q-2": c2}) == 0
    assert sg.decisions()["q-1"]["final_action"] == "dup"
    assert sg.decisions()["q-2"]["final_action"] == "create"


def test_recommendation_attestation_fields_are_validated():
    ok = {
        "candidate_id": "c",
        "action": "create",
        "confidence": 0.5,
        "already_searched": True,
        "searched_sources": ["rulings"],
    }
    got = rc.from_obj(ok, "c")
    assert got.already_searched and got.searched_sources == ("rulings",)
    assert got.to_dict()["searched_sources"] == ["rulings"]
    for bad in ({"already_searched": "yes"}, {"searched_sources": "rulings"}, {"searched_sources": [1]}):
        with pytest.raises(rc.BadRecommendation):
            rc.from_obj({"candidate_id": "c", "action": "create", "confidence": 0.5, **bad}, "c")


def test_the_reference_recommender_passes_the_attestation_through():
    checked = llm.validate(
        {
            "action": "create",
            "confidence": 0.5,
            "already_searched": True,
            "searched_sources": ["closed beads"],
        },
        [],
    )
    assert checked["already_searched"] is True and checked["searched_sources"] == ["closed beads"]
    assert llm.validate({"action": "create", "confidence": 0.5}, [])["already_searched"] is False
    with pytest.raises(llm.Invalid):
        llm.validate({"action": "create", "confidence": 0.5, "already_searched": 1}, [])
    assert llm.fallback("c", "x")["already_searched"] is False


def test_a_question_create_that_landed_before_its_receipt_still_owes_its_route(sg, monkeypatch):
    sg.env.set_issues([*world(), issue("pre-1", description="body\n\nembeadify-candidate: q-1")])
    c = qcand(sg, "q-1", title="QUESTION: may we publish early?")
    assert go(sg, {}, {"q-1": {**CREATE, **SEARCHED}}, cands={"q-1": c}) == 0
    receipt = sg.decisions()["q-1"]
    assert receipt["outcome"] == "reconciled" and sg.verbs() == []
    assert receipt["route"]["hub_brief"]["ref"] == "pre-1"


def test_a_question_create_with_a_lost_response_is_reconciled_with_its_route(sg, monkeypatch):
    monkeypatch.setenv("FAKE_BD_CRASH_AFTER_CREATE", "1")
    c = qcand(sg, "q-1", title="QUESTION: may we publish early?")
    assert go(sg, {}, {"q-1": {**CREATE, **SEARCHED}}, cands={"q-1": c}) == 0
    (bead,) = created(sg)
    receipt = sg.decisions()["q-1"]
    assert receipt["route"]["hub_brief"]["ref"] == bead["id"] and len(created(sg)) == 1


# ---- D: the scribe does not trigger itself --------------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"source": {"agent": "embeadify-scribe"}},
        {"source": {"agent": " Embeadify-Scribe "}},
        {"body": "intro\nembeadify-replay: demo-4\nmore"},
        {"candidate_id": "replay-demo-4"},
    ],
)
def test_the_scribes_own_events_are_rejected_at_submit(sg, capsys, over):
    assert sg.submit(sg.candidate(**over)) == 2
    err = capsys.readouterr().err
    assert "rejected" in err and "never reacts to its own events" in err
    assert not (sg.queue / "submissions").exists() or not list((sg.queue / "submissions").glob("*.json"))


def test_self_names_come_from_the_policy(sg, capsys):
    sg.write_policy(self_names='["my-bot"]')

    def submit(c):
        path = sg.env.root / f"{c['candidate_id']}.json"
        path.write_text(json.dumps(c))
        return main(
            ["scribe", "submit", str(path), "--queue-dir", str(sg.queue), "--policy", str(sg.policy_path)]
        )

    assert submit(sg.candidate("s-1", source={"agent": "my-bot"})) == 2
    assert submit(sg.candidate("s-2", source={"agent": "embeadify-scribe"})) == 0  # replaced, not extended


def test_an_ordinary_candidate_still_submits(sg):
    assert sg.submit(sg.candidate("ok-1")) == 0


# ---- E: owner_summary is read, never written ------------------------------------------------------------


def test_owner_summary_reaches_the_recommender_truncated(sg, monkeypatch):
    table = {"e-1": [neighbor("g-summary", 0.9), neighbor("g-plain", 0.8)]}
    assert go(sg, table, {"e-1": CREATE}) == 0
    seen = [json.loads(x) for x in (sg.env.root / "rec-seen.jsonl").read_text().splitlines()]
    by_id = {n["issue_id"]: n for n in seen[0]["neighbors"]}
    assert by_id["g-summary"]["owner_summary"] == "o" * 400
    assert "owner_summary" not in by_id["g-plain"]
    assert sg.log()[-1]["neighbors"][0]["owner_summary"] == "o" * 400


def test_the_llm_prompt_carries_a_truncated_owner_summary():
    payload = {
        "candidate": {"title": "t", "body": "b", "type": "task"},
        "neighbors": [
            {"issue_id": "n-1", "status": "open", "similarity": 0.9, "owner_summary": "w " * 500},
            {"issue_id": "n-2", "status": "open", "similarity": 0.8},
        ],
    }
    prompt = llm.build_prompt(payload)
    block = prompt.split("=== BEGIN UNTRUSTED DATA")[1].split("\n")[1]
    ns = json.loads(block)["neighbors"]
    assert len(ns[0]["owner_summary"]) <= 400 + 40 and ns[0]["owner_summary"].startswith("w w w")
    assert "owner_summary" not in ns[1]


def test_snapshot_reads_owner_summary_from_object_or_json_metadata():
    snap = snapshot.parse(
        [
            issue("m-1", metadata={"owner_summary": "  a\n b  "}),
            issue("m-2", metadata=json.dumps({"owner_summary": "c"})),
            issue("m-3", metadata="not json"),
            issue("m-4", metadata={"other": "x"}),
        ]
    )
    assert [snap[k].owner_summary for k in ("m-1", "m-2", "m-3", "m-4")] == ["a b", "c", "", ""]


def test_no_bd_argv_ever_writes_metadata(sg, monkeypatch):
    c1 = qcand(sg, "w-1", title="QUESTION: may we publish early?")
    c2 = qcand(sg, "w-2", title="Plain finding")
    c3 = qcand(sg, "w-3", title="QUESTION: chiefs?", labels=["questions"])
    table = {"w-2": [neighbor("g-summary", 0.99)]}
    recs = {"w-1": {**CREATE, **SEARCHED}, "w-2": dup("g-summary"), "w-3": CREATE}
    assert go(sg, table, recs, cands={"w-1": c1, "w-2": c2, "w-3": c3}) == 0
    assert sg.verbs() and not any("metadata" in arg for call in sg.env.calls() for arg in call)
    # the source of every scribe module and the engine never spells a metadata write
    for path in [*(SRC / "scribe").glob("*.py"), SRC / "engine.py"]:
        text = path.read_text(encoding="utf-8")
        assert "set-metadata" not in text.replace('"set-metadata", "--set-metadata"', ""), path


def test_the_metadata_tripwire_refuses_a_plan_that_would_write_it(sg, monkeypatch):
    assert executor.writes_metadata(["update", "x", "--set-metadata=owner_summary=y"])
    assert executor.writes_metadata(["update", "x", "set-metadata"])
    assert not executor.writes_metadata(["update", "x", "--append-notes=metadata is a word"])
    monkeypatch.setattr(executor, "writes_metadata", lambda argv: True)
    assert go(sg, {}, {"w-1": CREATE}) == 1
    assert sg.verbs() == [] and sg.log()[-1]["outcome"] == "failed"


# ---- G: counting and reporting ------------------------------------------------------------------------


def test_guards_are_counted_in_report_metrics_and_the_judge_pack(sg, monkeypatch, capsys):
    table = {
        "m-1": [neighbor("g-sens", 0.99)],
        "m-2": [neighbor("g-claimed", 0.99)],
        "m-3": [neighbor("g-plain", 0.99)],
    }
    recs = {"m-1": dup("g-sens"), "m-2": dup("g-claimed"), "m-3": dup("g-plain")}
    assert go(sg, table, recs, live=False) == 0
    capsys.readouterr()
    q = str(sg.queue)
    assert main(["scribe", "report", "--queue-dir", q, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["guard_forced_create"] == {
        "claimed_target": 1,
        "sensitive_target": 1,
    }
    assert main(["scribe", "metrics", "--queue-dir", q, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["overall"]["guard_forced_create"] == {"claimed_target": 1, "sensitive_target": 1}
    assert main(["scribe", "metrics", "--queue-dir", q]) == 0
    assert "guard_forced_create: claimed_target 1, sensitive_target 1 (total 2)" in capsys.readouterr().out
    assert main(["scribe", "judge-pack", "--queue-dir", q, "--n", "5"]) == 0
    out = capsys.readouterr().out
    assert "guard_forced_create (population): claimed_target 1, sensitive_target 1" in out
    assert '"guards": [' in out
    assert (
        main(["scribe", "judge-pack", "--queue-dir", q, "--n", "5", "--out", str(sg.env.root / "p.md")]) == 0
    )


def test_tune_rejudges_a_guarded_row_the_same_way(sg, monkeypatch):
    assert go(sg, {"t-1": [neighbor("g-claimed", 0.99)]}, {"t-1": dup("g-claimed")}, live=False) == 0
    row = sg.log()[-1]
    assert row["target_facts"]["assignee"] == "farmer-2"
    plan, _ = tune.decide(row, Policy())
    assert plan.action == "create" and plan.guards == ["claimed_target"]


def test_routes_lists_nothing_on_an_empty_queue(sg, capsys):
    assert main(["scribe", "routes", "--queue-dir", str(sg.queue)]) == 0
    assert "no pending routes" in capsys.readouterr().out
    assert st.Queue(sg.queue).routes() == []


def test_closed_quote_problem_unit():
    rec = rc.Recommendation("c", "dup", "x", evidence=(QUOTE,))
    state = snapshot.State("x", "closed", None, None, set())
    assert guards.closed_quote_problem(rec, CLOSE, state) == ""
    assert guards.closed_quote_problem(rec, "", state) == "closed_target_without_resolution_evidence"
    assert (
        guards.closed_quote_problem(rec, "something else entirely", state)
        == "closed_target_without_owner_quote"
    )
    curly = rc.Recommendation(
        "c", "dup", "x", evidence=("Clay 2026-01-02: “this exact finding is resolved and verified”",)
    )
    assert guards.closed_quote_problem(curly, CLOSE, state) == ""
