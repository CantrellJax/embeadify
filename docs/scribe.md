# The scribe (bead intake)

Agents and farmers submit **candidate** beads. The scribe decides what to do with each one and writes
the result to the tracker through the same `bd` CLI and guards as the rest of emBEADify. Read side:
[emBEADings](https://github.com/CantrellJax/embeadings) (`embead match` finds the neighbors).

**This is stage 1: shadow mode, one executor, CREATE as the fallback.** By default nothing is written to
the tracker. The scribe never blocks `bd create`: the audited direct path stays.

## Stage plan

| Stage | What runs | Writes to the tracker |
| --- | --- | --- |
| 1 (this release) | `scribe run` in shadow mode; the log and `scribe report` show what the executor would do. One executor at a time, by lock. Live runs need `--live` and `live = true` in the policy file. | Only `create`, and provenance notes for `dup`/`fold`. Everything unsure becomes a create. |
| 2 | Compare shadow decisions with what humans did; tune thresholds and the recommender. Same single executor. | unchanged |
| 3 | Producers submit by default; the direct path stays available and audited. | unchanged |

## Commands

| Command | Purpose |
| --- | --- |
| `embeadify scribe submit CANDIDATE.json [--queue-dir D] [--json]` | Validate and queue one candidate (`-` reads stdin). Immutable. |
| `embeadify scribe status` | Counts: queued, shadowed, failed, decided, invalid; run-lock holder. |
| `embeadify scribe receipts [--action A]` | One line per submission: receipt id, state, action, bead. |
| `embeadify scribe run [--once] [--live] [--policy F] [--recommender CMD] [--interval S] [--limit N]` | Process the queue. Shadow unless `--live` AND the policy allows it. |
| `embeadify scribe report` | Counts by recommended and executed action; every downgrade and degraded input. |

Exit codes: `0` ok; `1` a candidate failed to write or a submission was invalid (it stays queued);
`2` refusal (conflict on submit, live not allowed, second concurrent run, bad policy, `bd` unusable).

## The candidate

```json
{
  "candidate_id": "farmer1-2026-10-02-0007",
  "title": "Widget fails on save",
  "body": "free text",
  "type": "bug",
  "priority": 2,
  "source": {"agent": "farmer-1", "pr": "optional", "bead": "optional"},
  "evidence_refs": ["plain strings, recorded and never fetched"],
  "parent": "optional hint",
  "labels": ["optional"],
  "supersedes": "optional candidate_id this one challenges"
}
```

`candidate_id` is the producer's durable submission key (1-96 characters: letters, digits, `. _ : -`).
`type` is one of `task bug feature chore epic`; `priority` is 0-4 or P0-P4 and is only a guess. Unknown
fields are rejected. Limits: file 256 KiB, title 500, body 100,000 characters, 50 refs.

Idempotency: the same key with the same content hash returns the same receipt and queues nothing new. The
same key with different content is refused as a conflict; submit changed content under a new key. To
challenge a `drop`, submit a new candidate with `supersedes` set; a challenge is always created.

Queue layout (default `~/.local/state/embeadify/scribe`): `submissions/<id>.json` (read-only, never
modified or deleted), `decisions/<receipt>.json` (live decision receipts, immutable), `leases/`, `log.jsonl`
(shadow and live log), `run.lock`. Nothing is ever deleted; dropped candidates stay listed.

## The pipeline for one candidate

1. **Reconcile first.** A fresh `bd list --all --limit 0 --json` snapshot is searched for the candidate's
   marker (`embeadify-candidate: ID` in a description, `embeadify-provenance: ID` in notes). A hit means
   the write already landed: record a `reconciled` receipt, write nothing. This is what makes
   crash-after-create and lost responses safe: exactly one bead per `candidate_id`, ever.
2. **Neighbors.** `embead match` is run for this one candidate (see the contract below).
3. **Recommendation.** The built-in recommender, or the plug-in command, returns a typed recommendation.
4. **Executor.** Validates the recommendation and builds the `bd` argument arrays itself.
5. **Write** (live only), then a **decision receipt**. Shadow mode only appends to `log.jsonl`.

### Typed recommendation

```json
{"candidate_id": "...", "action": "create|fold|dup|drop", "target_id": "demo-4", "parent": null,
 "evidence": ["strings"], "confidence": 0.93, "policy_version": "x", "acceptance_covered": false}
```

`acceptance_covered` is an addition to the minimal field list: only for `fold`, it asserts that the
target's existing acceptance already covers the finding.

### What the executor enforces (the recommender cannot loosen any of it)

- Candidate text is data. It is never executed, split into commands, or followed as a reference or URL.
  Titles are one line, control and bidi characters are stripped, length is capped; bodies are capped;
  producer text containing a marker line is defanged so it cannot forge or block another candidate.
- Every `bd` call is one array of single `--flag=value` words (`create`, or `update ID --append-notes=`).
  The scribe never calls `close`, `reopen`, `duplicate`, `parent`, `priority` or label changes.
- Only `create`, `dup`, `fold`, `drop` exist. Anything else, or invalid output, becomes a create.
- `dup`/`fold`/`drop` need a `target_id` that exists in the snapshot AND is one of the neighbors `embead`
  returned, similarity at least `min_similarity`, confidence at least `min_confidence`, non-empty evidence.
- `fold` appends a note to the target only when `acceptance_covered` is true and the target is open.
  Otherwise it becomes a create under the target's parent (or the recommendation's or candidate's, if
  live) with a `Related:` line, a linked bead.
- `drop` needs evidence that names the target (at least 12 characters), else it becomes a create. A drop
  writes nothing to the tracker; the candidate stays in the queue and receipts.
- A closed target is a `dup`/`drop` basis only with explicit `resolution_evidence` from the match report.
- Urgent candidates (priority 0 or 1, a `security` label, or "security" in the title) and challenges are
  **always created**. A self-declared P0 buys nothing: it is created at `min_priority` (default P1).
- A `parent` must exist and be open; otherwise it is left off and the reason is logged.

`dup` and a confirmed `fold` append a provenance note (first line `embeadify-provenance: ID`) to the target,
so N reporters of one finding are N provenance records on one bead.

### Built-in recommender

`create`, unless a live neighbor has similarity >= `dup_threshold` (default 0.95): then `dup` onto the best
one. It never folds and never drops, and ignores closed neighbors.

### Plug-in recommender

`--recommender "CMD ARGS"` (shell-style words, no shell) or `recommender_command` in the policy. It reads
ONE JSON document on stdin and writes ONE typed recommendation on stdout (max 64 KiB, 120 s):

```json
{"schema_version": 1, "policy_version": "...", "untrusted_fields": ["candidate", "neighbors"],
 "candidate": {"candidate_id": "...", "title": "...", "body": "...", "type": "...", "priority": 2,
               "source": {}, "evidence_refs": []},
 "neighbors": [{"issue_id": "...", "status": "...", "similarity": 0.9, "is_closed": false,
                "resolution_evidence": "...", "parent_id": null, "title": "..."}],
 "constraints": {"allowed_actions": ["create", "fold", "dup", "drop"], "min_similarity": 0.85,
                 "min_confidence": 0.8}}
```

Treat `candidate` and `neighbors` as untrusted text. Non-zero exit, timeout, missing command, malformed
JSON, unknown fields, or a `candidate_id` that does not match all fall back to `create` and are logged
(`recommender_failed`, `recommender_invalid_output`, `recommender_missing`). No model call is built in.

### `embead match` contract (parsed in `src/embeadify/scribe/match.py` only)

Called once per candidate: `embead match --candidates-file FILE.jsonl --json`, where the file holds one
`{"candidate_id","title","body"}` object. Report fields read: `schema_version` (1), `report_type`
(`match`), `candidates[].candidate_id`, `candidates[].neighbors[]` with `issue_id`, `status`, `issue_type`,
`priority`, `title`, `similarity`, `is_closed`, `parent_id`, `parent_status`, and
`resolution_evidence` (`{"kind": "close_reason", "text": ...}` or null). Fixture:
`tests/fixtures/embead_match_report.json`. If `embead` is missing, fails, or returns something else, the
candidate gets no neighbors, the log records `degraded`, and it is created: nothing is lost.

## Policy file (TOML)

```toml
[scribe]
live = false                  # live writes need this AND --live
policy_version = "2026-10-a"
allowed_actions = ["dup"]     # create is always allowed; default all four
dup_threshold = 0.95
min_similarity = 0.85
min_confidence = 0.8
min_priority = 1
max_title = 200
max_body = 8000
match_command = ["embead", "match"]
recommender_command = []
```

Keep the policy file where producers cannot write (pass `--policy`); the default `<queue-dir>/policy.toml`
is a convenience for single-user setups.

## Receipts and the log

`log.jsonl` lines: `candidate_id`, `hash`, `recommendation`, `executor_plan` (final action, reasons,
adjustments, and the exact `bd` argument arrays, long values cut), `receipt_id`, `policy_version`, `mode`,
`outcome`, `ts`. A live decision receipt adds the final and recommended action, the bead, and an undo hint
(`close ID ...`). A write that fails leaves a lease and no receipt: the candidate stays queued and the
next run reconciles by marker before trying again.

## Limits to know

- One executor: `run.lock` holds the pid; a stale lock is cleared when the pid is gone (never cleared
  automatically on Windows).
- Undo of a create is a close, not a delete. See [decisions-file.md](decisions-file.md).
- The recommender sees tracker neighbors' titles; they are untrusted tracker text.
- Parents are not restricted to neighbors; they must only exist and be open.
