"""Batched matching, parallel recommending, resume, timing and the model budget: synthetic and offline."""

import json
import sys
import time

import pytest
from conftest import demo_issues
from test_scribe import HERE, Scribe, rec  # noqa: F401
from test_trainee import bead, nb

from embeadify import llm_recommend as llm
from embeadify import snapshot
from embeadify.cli import main
from embeadify.scribe import match, recommend, replay, runner
from embeadify.scribe import store as st
from embeadify.scribe.policy import PolicyError, load

N = 10
IDS = [f"b-{k:02d}" for k in range(1, N + 1)]


def world():
    items = [bead("p-1", "2026-09-01T09:00:00Z", "Epic")]
    items += [bead(i, f"2026-09-{k + 1:02d}T09:00:00Z", parent="p-1") for k, i in enumerate(IDS, 1)]
    return items


@pytest.fixture
def sp(env, monkeypatch):
    sx = Scribe(env, monkeypatch)
    env.set_issues(world())
    table = {}
    for k, i in enumerate(IDS):
        near = [nb("p-1", 0.5)]
        if k:
            near.insert(0, nb(IDS[k - 1], 0.9))
        if k + 1 < N:
            near.insert(0, nb(IDS[k + 1], 0.99))  # a LATER bead: temporal fairness must drop it
        table["replay-" + i] = near
    fixture = env.root / "embead.json"
    fixture.write_text(json.dumps(table))
    monkeypatch.setenv("FAKE_EMBEAD_FIXTURE", str(fixture))
    sx.embead_calls = env.root / "embead-calls.jsonl"
    monkeypatch.setenv("FAKE_EMBEAD_CALLS", str(sx.embead_calls))
    return sx


def embead_calls(sp):
    if not sp.embead_calls.exists():
        return []
    return [json.loads(line) for line in sp.embead_calls.read_text().splitlines()]


def rp(sp, *extra, capsys=None):
    argv = ["scribe", "replay", "--queue-dir", str(sp.queue), "--policy", str(sp.policy_path), *extra]
    code = main(argv)
    return code


def rows(sp):
    return {r["candidate_id"]: r for r in sp.log()}


def neighbor_ids(row):
    return [n["issue_id"] for n in row["neighbors"]]


# ---- batch matching ----------------------------------------------------------------------------


def test_replay_of_ten_candidates_calls_embead_exactly_once(sp):
    assert rp(sp, "--since", "2026-09-02") == 0
    calls = embead_calls(sp)
    assert len(calls) == 1 and sorted(calls[0]["candidates"]) == sorted("replay-" + i for i in IDS)
    got = rows(sp)
    assert len(got) == N
    assert neighbor_ids(got["replay-b-01"]) == ["p-1"]
    for k in range(1, N):  # each sees its OWN earlier neighbor, never a later bead
        assert neighbor_ids(got["replay-" + IDS[k]]) == [IDS[k - 1], "p-1"]
    assert [r["candidate_id"] for r in sp.log()] == ["replay-" + i for i in IDS]  # bead order


def test_results_map_back_by_candidate_id_when_the_report_is_out_of_order(sp, monkeypatch):
    monkeypatch.setenv("FAKE_EMBEAD_REVERSE", "1")
    rp(sp, "--since", "2026-09-02")
    assert len(embead_calls(sp)) == 1
    assert neighbor_ids(rows(sp)["replay-b-05"]) == ["b-04", "p-1"]


def test_a_candidate_missing_from_the_report_is_degraded_alone(sp, monkeypatch):
    monkeypatch.setenv("FAKE_EMBEAD_OMIT", "replay-b-03")
    rp(sp, "--since", "2026-09-02")
    assert len(embead_calls(sp)) == 1  # no retry storm for one omitted entry
    got = rows(sp)
    assert "no entry" in got["replay-b-03"]["degraded"] and got["replay-b-03"]["neighbors"] == []
    assert got["replay-b-04"]["degraded"] is None and neighbor_ids(got["replay-b-04"]) == ["b-03", "p-1"]


def test_a_failed_batch_falls_back_to_one_call_per_candidate(sp, monkeypatch):
    monkeypatch.setenv("FAKE_EMBEAD_FAIL_MULTI", "1")
    rp(sp, "--since", "2026-09-02")
    calls = embead_calls(sp)
    assert len(calls) == 1 + N and len(calls[0]["candidates"]) == N
    assert all(len(c["candidates"]) == 1 for c in calls[1:])
    got = rows(sp)
    assert all(r["degraded"] is None for r in got.values())
    assert neighbor_ids(got["replay-b-05"]) == ["b-04", "p-1"]


def test_a_dead_matcher_degrades_every_candidate_and_still_decides(sp, monkeypatch):
    monkeypatch.setenv("FAKE_EMBEAD_FAIL", "1")
    rp(sp, "--since", "2026-09-02")
    got = rows(sp)
    assert len(got) == N and all(r["degraded"] and "embead_failed" in r["degraded"] for r in got.values())


def test_neighbor_limit_still_applies_per_candidate(sp, monkeypatch):
    table = json.loads((sp.env.root / "embead.json").read_text())
    table["replay-b-10"] = [nb(i, 0.9 - n / 100) for n, i in enumerate(reversed(IDS[:9]))]
    (sp.env.root / "embead.json").write_text(json.dumps(table))
    rp(sp, "--since", "2026-09-02", "--neighbor-limit", "2")
    (call,) = embead_calls(sp)
    assert call["limit"] == 2 + replay.NEIGHBOR_PAD
    got = rows(sp)
    assert neighbor_ids(got["replay-b-10"]) == ["b-09", "b-08"]
    assert all(len(r["neighbors"]) <= 2 for r in got.values())


def test_fetch_many_unit_maps_by_id_and_single_candidate_does_not_retry(sp):
    cmd = tuple(sp.match_command())
    cs = [{"candidate_id": f"x-{n}", "title": "t", "body": "b"} for n in range(3)]
    out, calls = match.fetch_many(cs, cmd, 30)
    assert calls == 1 and set(out) == {"x-0", "x-1", "x-2"}
    sp.mp.setenv("FAKE_EMBEAD_FAIL", "1")
    out, calls = match.fetch_many(cs[:1], cmd, 30)
    assert calls == 1 and "embead_failed" in out["x-0"][1]


# ---- live runs: stale re-match ------------------------------------------------------------------


def test_shadow_run_matches_once_for_the_whole_queue(sp):
    for n in range(4):
        sp.submit(sp.candidate(f"s-{n}"))
    assert sp.run() == 0
    (call,) = embead_calls(sp)
    assert sorted(call["candidates"]) == [f"s-{n}" for n in range(4)]


def test_a_live_create_makes_only_later_candidates_stale_and_they_rematch_together(sp, monkeypatch):
    monkeypatch.delenv("FAKE_EMBEAD_FIXTURE")
    sp.env.set_issues(demo_issues())
    monkeypatch.setenv("FAKE_EMBEAD_TITLE_CONTAINS", "Flaky widget")
    for cid, title in [("p-1", "Flaky widget fails on save"), ("p-2", "Flaky widget save breaks"),
                       ("p-3", "Flaky widget save failure")]:  # fmt: skip
        sp.submit(sp.candidate(cid, title=title))
    assert sp.run(live=True) == 0
    calls = embead_calls(sp)
    # one batch of 3; after the create, ONE re-match of the 2 later ones; the dups create nothing: no more
    assert [len(c["candidates"]) for c in calls] == [3, 2]
    assert sp.verbs() == ["create", "update", "update"]
    assert [d["final_action"] for _, d in sorted(sp.decisions().items())] == ["create", "dup", "dup"]
    (b,) = sp.new_beads()
    assert sp.bead_markers()[b["id"]] == ["candidate:p-1", "provenance:p-2", "provenance:p-3"]


def test_every_create_rematches_the_remaining_tail_once(sp, monkeypatch):
    monkeypatch.delenv("FAKE_EMBEAD_FIXTURE")
    sp.env.set_issues(demo_issues())
    for n in range(4):
        sp.submit(sp.candidate(f"c-{n}"))
    assert sp.run(live=True) == 0
    assert [len(c["candidates"]) for c in embead_calls(sp)] == [4, 3, 2, 1]
    assert sp.verbs() == ["create"] * 4


# ---- parallel recommender -----------------------------------------------------------------------


def overlap(path):
    spans = [json.loads(x) for x in path.read_text().splitlines()]
    events = sorted([(s["start"], 1) for s in spans] + [(s["end"], -1) for s in spans])
    cur = best = 0
    for _, d in events:
        cur += d
        best = max(best, cur)
    return best


def rec_setup(sp, monkeypatch, sleep="0.4"):
    sp.write_policy(recommender=True)
    times = sp.env.root / "times.jsonl"
    monkeypatch.setenv("FAKE_REC_TIMES", str(times))
    monkeypatch.setenv("FAKE_REC_SLEEP", sleep)
    sp.recs({})
    return times


def timed_replay(sp, *extra):
    t = time.monotonic()
    assert rp(sp, "--since", "2026-09-02", "--again", *extra) == 0
    return time.monotonic() - t


def test_recommender_calls_overlap_but_the_log_order_stays_deterministic(sp, monkeypatch):
    times = rec_setup(sp, monkeypatch)
    slow = timed_replay(sp, "--recommender-jobs", "1")
    assert overlap(times) == 1
    seq_order = [r["candidate_id"] for r in sp.log()]
    times.unlink()
    sp.queue.joinpath("log.jsonl").unlink()
    fast = timed_replay(sp, "--recommender-jobs", "4")
    assert 1 < overlap(times) <= 4
    assert fast < slow * 0.75, (fast, slow)
    assert [r["candidate_id"] for r in sp.log()] == seq_order == ["replay-" + i for i in IDS]


def test_jobs_one_never_overlaps_and_jobs_are_bounded(sp, monkeypatch, capsys):
    for bad in ("0", "9", "x"):
        with pytest.raises(SystemExit) as exc:
            rp(sp, "--recommender-jobs", bad)
        assert exc.value.code == 2
    assert runner.clamp_jobs(100) == runner.MAX_JOBS and runner.clamp_jobs(0) == 1
    assert runner.clamp_jobs(None) == runner.DEFAULT_JOBS == 4
    times = rec_setup(sp, monkeypatch, "0.05")
    assert rp(sp, "--since", "2026-09-02", "--recommender-jobs", "8") == 0
    assert overlap(times) <= 8


def test_parallel_recommending_never_reorders_or_double_writes_live_creates(sp, monkeypatch):
    monkeypatch.delenv("FAKE_EMBEAD_FIXTURE")
    sp.env.set_issues(demo_issues())
    sp.write_policy(recommender=True)
    seen = sp.env.root / "rec-seen.jsonl"
    times = sp.env.root / "t.jsonl"
    monkeypatch.setenv("FAKE_REC_TIMES", str(times))
    sleeps = sp.env.root / "sleeps.json"  # the FIRST candidate is the slowest: completion order is reversed
    sleeps.write_text(json.dumps({f"c-{n}": 0.9 - 0.25 * n for n in range(4)}))
    monkeypatch.setenv("FAKE_REC_SLEEP_MAP", str(sleeps))
    sp.recs({})
    for n in range(4):
        sp.submit(sp.candidate(f"c-{n}"))
    assert sp.run("--recommender-jobs", "4", live=True) == 0
    assert overlap(times) > 1
    creates = [c for c in sp.env.calls() if c[0] == "create"]
    assert len(creates) == 4 and len(sp.new_beads()) == 4
    titles = [next(a for a in c if a.startswith("--title=")) for c in creates]
    assert titles == [f"--title=Synthetic finding c-{n}" for n in range(4)]  # candidate order, one write each
    assert [json.loads(x)["candidate"]["candidate_id"] for x in seen.read_text().splitlines()].count(
        "c-0"
    ) == 1
    assert [r["candidate_id"] for r in sp.log()] == [f"c-{n}" for n in range(4)]
    assert all(r["outcome"] == "applied" for r in sp.log())


def test_a_changed_neighbor_set_after_a_create_gets_a_fresh_recommendation(sp, monkeypatch):
    monkeypatch.delenv("FAKE_EMBEAD_FIXTURE")
    sp.env.set_issues(demo_issues())
    monkeypatch.setenv("FAKE_EMBEAD_TITLE_CONTAINS", "Flaky widget")
    monkeypatch.setenv("FAKE_EMBEAD_SIM", "0.9")
    sp.write_policy(recommender=True)
    sp.recs({})
    sp.submit(sp.candidate("p-1", title="Flaky widget fails on save"))
    sp.submit(sp.candidate("p-2", title="Flaky widget save breaks"))
    assert sp.run("--recommender-jobs", "2", live=True) == 0
    seen = [json.loads(x) for x in (sp.env.root / "rec-seen.jsonl").read_text().splitlines()]
    p2 = [s for s in seen if s["candidate"]["candidate_id"] == "p-2"]
    assert (
        len(p2) == 2 and not p2[0]["neighbors"] and len(p2[1]["neighbors"]) == 1
    )  # redone WITH the new bead


def test_a_crashing_recommender_slot_falls_back_for_that_candidate_only(sp, monkeypatch):
    sp.write_policy(recommender=True)
    sp.recs({})
    orig = recommend.external

    def flaky(command, c, neighbors, policy, timeout=120.0):
        if c["candidate_id"] == "replay-b-04":
            raise RuntimeError("boom")
        return orig(command, c, neighbors, policy, timeout)

    monkeypatch.setattr(recommend, "external", flaky)
    rp(sp, "--since", "2026-09-02", "--recommender-jobs", "4")
    got = rows(sp)
    assert len(got) == N and got["replay-b-04"]["recommendation"]["action"] == "create"
    assert "recommender_failed" in " ".join(
        got["replay-b-04"]["executor_plan"]["reasons"] + [str(got["replay-b-04"])]
    )


# ---- progress, resume, timing --------------------------------------------------------------------


def test_progress_goes_to_stderr_and_json_stays_clean(sp, capsys):
    rp(sp, "--since", "2026-09-02", "--json", "--timing")
    out, err = capsys.readouterr()
    assert "replayed 10/10" in err and "replayed 1/10" in err
    data = json.loads(out)
    t = data["timing"]
    assert t["match_calls"] == 1 and t["wall_seconds"] > 0 and data["replayed"] == N
    assert t["llm_calls"] == 0


def test_an_interrupted_replay_leaves_a_valid_log_and_resumes(sp, monkeypatch):
    policy = load(sp.policy_path)
    raw = world()
    queue = st.Queue(sp.queue)
    orig = st.Queue.log
    count = {"n": 0}

    def dying(self, entry):
        count["n"] += 1
        if count["n"] == 4:
            self.ensure()  # a torn half-line, as a hard kill mid-write would leave
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write('{"candidate_id": "replay-b-04", "hash"')
            raise KeyboardInterrupt
        return orig(self, entry)

    monkeypatch.setattr(st.Queue, "log", dying)
    with pytest.raises(KeyboardInterrupt):
        replay.replay(queue, policy, raw, since=replay.parse_since("2026-09-02"))
    monkeypatch.setattr(st.Queue, "log", orig)
    assert [r["candidate_id"] for r in queue.read_log()] == ["replay-" + i for i in IDS[:3]]
    again = replay.replay(
        queue, policy, raw, since=replay.parse_since("2026-09-02")
    )  # resumes: skips the 3 done, finishes the other 7
    assert again.already == 3 and again.replayed == 7
    ids = [r["candidate_id"] for r in queue.read_log()]
    assert ids == ["replay-" + i for i in IDS]  # every entry intact: the torn line did not fuse
    assert len(embead_calls(sp)) == 2


def test_timing_line_for_run(sp, capsys):
    sp.submit(sp.candidate("t-1"))
    assert sp.run("--timing") == 0
    assert "timing: wall" in capsys.readouterr().out


# ---- the model prefilter, usage telemetry and the budget -------------------------------------------


@pytest.fixture
def llm_sp(sp, monkeypatch):
    reply = sp.env.root / "reply.txt"
    reply.write_text(json.dumps({"action": "create", "target_id": None, "confidence": 0.9, "evidence": []}))
    seen = sp.env.root / "llm-seen.jsonl"
    monkeypatch.setenv("EMBEADIFY_LLM_CMD", json.dumps([sys.executable, str(HERE / "fake_llm.py")]))
    monkeypatch.setenv("FAKE_LLM_REPLY_FILE", str(reply))
    monkeypatch.setenv("FAKE_LLM_SEEN", str(seen))
    monkeypatch.setenv("FAKE_LLM_ENVELOPE", "1")
    monkeypatch.setenv("FAKE_LLM_USAGE", json.dumps({"input_tokens": 900, "output_tokens": 100,
                                                      "cache_read_input_tokens": 100}))  # fmt: skip
    words = [sys.executable, "-m", "embeadify.llm_recommend"]
    sp.write_policy(recommender_command="[" + ", ".join(f"'{w}'" for w in words) + "]")
    sp.llm_seen = seen
    return sp


def llm_prompts(sp):
    return len(sp.llm_seen.read_text().splitlines()) if sp.llm_seen.exists() else 0


def test_the_prefilter_spawns_no_llm_process_and_counts_as_skipped(llm_sp, capsys):
    rp(llm_sp, "--since", "2026-09-02", "--json", "--timing", "--recommender-jobs", "4")
    data = json.loads(capsys.readouterr().out)
    # b-01 only has a 0.5 neighbor (below the 0.80 floor): its model call never happens
    assert llm_prompts(llm_sp) == N - 1
    assert data["timing"]["llm_calls"] == N - 1 and data["timing"]["llm_skipped"] == 1
    assert data["budget_skipped"] == 0


def test_prefilter_calls_at_the_floor_and_skips_below_it_without_spawning(llm_sp, capsys, monkeypatch):
    table = {"replay-" + i: [nb("p-1", 0.80 if i == "b-01" else 0.79 if i == "b-02" else 0.5)] for i in IDS}
    fixture = llm_sp.env.root / "embead-floor.json"
    fixture.write_text(json.dumps(table))
    monkeypatch.setenv("FAKE_EMBEAD_FIXTURE", str(fixture))
    rp(
        llm_sp,
        "--json",
        "--timing",
        "--ids-file",
        _ids_file(llm_sp, ["b-01", "b-02", "b-03"]),
    )
    t = json.loads(capsys.readouterr().out)["timing"]
    assert t["llm_calls"] == 1 and t["llm_skipped"] == 2  # only the 0.80 candidate reaches the model
    assert llm_prompts(llm_sp) == 1


def _ids_file(sp, ids):
    f = sp.env.root / "ids.txt"
    f.write_text("\n".join(ids) + "\n")
    return str(f)


def test_a_far_below_floor_policy_prints_a_stderr_note(llm_sp, capsys):
    llm_sp.write_policy(llm_min_similarity=0.5)
    rp(llm_sp, "--since", "2026-09-02", "--json")
    assert "rarely change decisions" in capsys.readouterr().err
    llm_sp.write_policy(llm_min_similarity=0.7)
    rp(llm_sp, "--since", "2026-09-02", "--json")
    assert "rarely change decisions" not in capsys.readouterr().err


def test_usage_is_captured_logged_and_summed(llm_sp, capsys):
    rp(llm_sp, "--since", "2026-09-02", "--json", "--timing")
    t = json.loads(capsys.readouterr().out)["timing"]
    assert t["llm_input_tokens"] == 1000 * (N - 1) and t["llm_output_tokens"] == 100 * (N - 1)
    assert t["llm_usage_calls"] == N - 1
    row = rows(llm_sp)["replay-b-02"]
    assert row["llm_input_tokens"] == 1000 and row["llm_output_tokens"] == 100 and row["llm_ms"] >= 0
    assert row["recommendation"]["metadata"]["llm_input_tokens"] == 1000
    assert rows(llm_sp)["replay-b-01"]["llm_input_tokens"] is None  # no call, no telemetry
    assert main(["scribe", "metrics", "--queue-dir", str(llm_sp.queue), "--json"]) == 0
    u = json.loads(capsys.readouterr().out)["overall"]["llm_usage"]
    assert u["calls"] == N - 1 and u["avg_input_tokens"] == 1000 and u["avg_output_tokens"] == 100
    assert main(["scribe", "metrics", "--queue-dir", str(llm_sp.queue)]) == 0
    assert "LLM spend" in capsys.readouterr().out


def test_missing_usage_is_null_not_zero(llm_sp, monkeypatch, capsys):
    monkeypatch.delenv("FAKE_LLM_USAGE")
    rp(llm_sp, "--since", "2026-09-02", "--json", "--timing")
    t = json.loads(capsys.readouterr().out)["timing"]
    assert t["llm_calls"] == N - 1 and t["llm_usage_calls"] == 0
    row = rows(llm_sp)["replay-b-02"]
    assert row["llm_input_tokens"] is None and row["llm_output_tokens"] is None


def test_the_call_budget_cuts_off_loudly_and_the_rest_take_the_builtin_path(llm_sp, capsys):
    rp(llm_sp, "--since", "2026-09-02", "--json", "--max-llm-calls", "3")
    data = json.loads(capsys.readouterr().out)
    assert llm_prompts(llm_sp) == 3 and data["budget_skipped"] == (N - 1) - 3
    got = rows(llm_sp)
    cut = [r for r in got.values() if r["recommender"].get("budget_skipped")]
    assert len(cut) == data["budget_skipped"]
    assert all(
        r["recommender"]["kind"] == "builtin" and r["recommender"]["model_called"] is False for r in cut
    )
    assert all("llm_budget_exhausted" in json.dumps(r) for r in cut)
    rp(llm_sp, "--since", "2026-09-02", "--again", "--max-llm-calls", "3")
    assert "budget:" in capsys.readouterr().out  # the text report says so too


def test_the_token_budget_stops_further_calls(llm_sp, capsys):
    # 1,100 tokens a call (900 + 100 cache + 100 out): the cap is passed after the 2nd call
    rp(llm_sp, "--since", "2026-09-02", "--json", "--max-llm-tokens", "2000", "--recommender-jobs", "1")
    data = json.loads(capsys.readouterr().out)
    assert llm_prompts(llm_sp) == 2 and data["budget_skipped"] == (N - 1) - 2


def test_zero_budget_means_no_model_at_all(llm_sp, capsys):
    rp(llm_sp, "--since", "2026-09-02", "--json", "--max-llm-calls", "0")
    assert llm_prompts(llm_sp) == 0 and json.loads(capsys.readouterr().out)["budget_skipped"] == N - 1


def test_the_budget_also_bounds_a_parallel_pass(llm_sp, capsys):
    rp(llm_sp, "--since", "2026-09-02", "--json", "--max-llm-calls", "4", "--recommender-jobs", "4")
    # count from the log, not the shared seen-file: concurrent appends can interleave on Windows
    called = [r for r in rows(llm_sp).values() if r["recommender"]["model_called"]]
    assert len(called) == 4 and json.loads(capsys.readouterr().out)["budget_skipped"] == (N - 1) - 4


def test_budget_applies_to_scribe_run_and_defaults_are_conservative(llm_sp, capsys):
    assert load(None).max_llm_calls == 40 and load(None).max_llm_tokens == 250_000
    for n in range(3):
        llm_sp.submit(llm_sp.candidate(f"r-{n}"))
    monkey_fixture = {f"r-{n}": [nb("demo-4", 0.9)] for n in range(3)}
    (llm_sp.env.root / "embead.json").write_text(json.dumps(monkey_fixture))
    assert llm_sp.run("--max-llm-calls", "1") == 0
    assert llm_prompts(llm_sp) == 1
    assert "2 candidate(s) skipped the model" in capsys.readouterr().out


def test_the_policy_file_sets_the_budget_and_validates_it(tmp_path):
    p = tmp_path / "p.toml"
    p.write_text("[scribe]\nmax_llm_calls = 7\nmax_llm_tokens = 1234\n")
    assert (load(p).max_llm_calls, load(p).max_llm_tokens) == (7, 1234)
    p.write_text('[scribe]\nmax_llm_calls = "lots"\n')
    with pytest.raises(PolicyError):
        load(p)


# ---- the shrunken prompt and usage parsing ----------------------------------------------------------


def test_the_prompt_is_a_few_thousand_tokens_at_most():
    payload = {
        "candidate": {"candidate_id": "c", "title": "T" * 900, "body": "B" * 20_000, "type": "task",
                      "evidence_refs": ["r" * 500] * 9},
        "neighbors": [{"issue_id": f"n-{i}", "status": "open", "similarity": 0.9, "title": "N" * 900,
                       "resolution_evidence": "line one\nline two " + "e" * 900} for i in range(9)],
    }  # fmt: skip
    prompt = llm.build_prompt(payload)
    assert "n-4" in prompt and "n-5" not in prompt  # the top 5 only
    assert "B" * llm.MAX_BODY in prompt and "B" * (llm.MAX_BODY + 1) not in prompt and llm.MAX_BODY <= 1500
    assert len(prompt) < 7000  # under ~2k tokens
    assert llm.neighbor_ids(payload) == [f"n-{i}" for i in range(5)]


def test_usage_parsing_is_defensive():
    env = json.dumps({"result": "x", "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5,
                                               "cache_read_input_tokens": 85, "output_tokens": 7},
                      "total_cost_usd": 0.02})  # fmt: skip
    m = llm.usage_of(env, 12.7)
    assert (m["llm_input_tokens"], m["llm_output_tokens"], m["llm_ms"], m["llm_cost_usd"]) == (
        100,
        7,
        12,
        0.02,
    )
    for junk in ("plain text", "[]", "null", json.dumps({"usage": "x"}),
                 json.dumps({"usage": {"input_tokens": -1, "output_tokens": True}})):  # fmt: skip
        m = llm.usage_of(junk)
        assert m["llm_input_tokens"] is None and m["llm_output_tokens"] is None


def test_recommendation_metadata_is_validated():
    ok = {"candidate_id": "c", "action": "create", "confidence": 0.5,
          "metadata": {"llm_input_tokens": 5, "llm_ms": None}}  # fmt: skip
    assert recommend.from_obj(ok, "c").metadata == {"llm_input_tokens": 5, "llm_ms": None}
    for bad in ({"x": 1}, {"llm_ms": -1}, {"llm_ms": "9"}, "no"):
        with pytest.raises(recommend.BadRecommendation):
            recommend.from_obj({**ok, "metadata": bad}, "c")


def test_snapshot_import_is_used():  # keeps the shared helper imports honest
    assert snapshot is not None
