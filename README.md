# emBEADify

<img src="https://raw.githubusercontent.com/CantrellJax/embeadify/main/assets/brand/embeadify-mark.svg" width="72" alt="Beads strung on a string; one amber bead moving into place">

[![CI](https://github.com/CantrellJax/embeadify/actions/workflows/ci.yml/badge.svg)](https://github.com/CantrellJax/embeadify/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/CantrellJax/embeadify?include_prereleases)](https://github.com/CantrellJax/embeadify/releases)

Apply reviewed decisions to a [Beads](https://github.com/gastownhall/beads) tracker—dry run first,
snapshot and undo always, several `bd` processes at a time.

> **The read side lives in [emBEADings](https://github.com/CantrellJax/embeadings).** It finds leads and
> never writes. emBEADify is its write-side sister: it executes the decisions a human approved from
> those leads. It is not an auto-fixer, and it never decides anything.

> **Status:** v0.1 technical preview.

## The pipeline

```text
 emBEADings            a human or            emBEADify              emBEADify
 finds leads   ───►    coordinator    ───►   applies approved ───►  undo
 (read-only JSON)      reviews + edits       decisions (--apply)    (replay the undo file)
        │                     ▲                      ▲
        └─ embeadify plan ────┘  commented-out       └─ dry run by default
           template              proposals
```

## Quick start

Python 3.11 or later and an installed `bd` CLI are required. emBEADify has no other dependencies,
makes no network calls of its own, and sends no telemetry.

```bash
pipx install embeadify
# or: uv tool install embeadify

# 1. Check what would be written to (host and database, secrets redacted)
embeadify doctor

# 2. Turn an emBEADings report into a decisions template (every line commented out)
embead orphans --json --output orphans.json
embeadify plan orphans.json            # writes orphans.decisions

# 3. A human uncomments what they approve and fills in the blanks, then:
embeadify apply orphans.decisions                  # DRY RUN: shows old -> new, writes nothing
embeadify apply orphans.decisions --apply          # writes the undo file first, then applies

# 4. Changed your mind?
embeadify undo embeadify-undo-20261002T101500Z.decisions          # dry run
embeadify undo embeadify-undo-20261002T101500Z.decisions --apply
```

## What a decisions file looks like

One operation per line; `#` starts a comment, blank lines are ignored.

```text
close demo-3 superseded by demo-9
parent demo-4 demo-2        # re-parent (use `-` to clear)
dup demo-5 demo-6           # mark as duplicate of demo-6
priority demo-7 1
label-add demo-7 reviewed
```

The full grammar is in [`docs/decisions-file.md`](docs/decisions-file.md).

## Safety model

| Guard | What it does |
| --- | --- |
| Dry run by default | Nothing is written without an explicit `--apply`. The dry run prints every change with old and new values. |
| One snapshot, never per-id reads | One `bd list --all --limit 0 --json` call validates the whole file. |
| Validation before writes | Unknown ids, closing a closed issue, a parent that is the issue itself, a parent cycle, a closed parent, and closing an issue with open dependents are refused. Any refusal stops the run before the first write. |
| Undo file first | The undo file is written before the first `bd` write. It is itself a decisions file: `embeadify undo FILE --apply`. |
| Drift check | Just before writing, a second snapshot is compared; an op whose issue changed since validation is skipped and reported. |
| Failures never hide | A failed op never aborts the batch, is listed with the exact `bd` retry command, and makes the exit code non-zero. |
| Bounded parallelism | `-j N` workers (default 4, hard maximum 8). Each worker runs a plain `bd` subprocess. |
| Honors `bd` guards | Your environment (`BEADS_DOLT_*` and friends) is passed to `bd` unchanged. A guard refusal is shown, never bypassed, never retried. |
| Secrets stay hidden | Passwords, tokens, and URL credentials are masked in every message. |
| Target is visible | `embeadify doctor` and every run print the host and database; `--apply` is refused when `bd` cannot report its target. |

Why parallel? Against a remote Dolt server each `bd` write costs a network round trip—roughly 5–10
seconds. 73 sequential updates took about 15 minutes; four `bd` processes in parallel cut that about
fourfold. Why the snapshot and undo? A single mistaken "no-op" check once moved a parent unnoticed and
was recovered only because an undo snapshot existed.

Two limits to know: a `note` is append-only and cannot be undone automatically (the undo file says so),
and `dup` / `close` are undone by reopening, which does not remove a duplicate link `bd` may have added.

## Commands

| Command | Purpose |
| --- | --- |
| `embeadify plan REPORT.json [--kind orphans\|mentions\|triage]` | Write a commented-out decisions template from an emBEADings schema-v1 report. |
| `embeadify apply FILE [--apply] [-j N] [--undo-file PATH] [--json]` | Dry run, or execute. |
| `embeadify undo UNDO_FILE [--apply]` | Replay an undo file through the same engine. |
| `embeadify doctor` | Check `bd` and show the redacted write target. |

Exit codes: `0` success, `1` one or more ops failed or were skipped for drift, `2` usage error, invalid
decisions, `bd` missing or unusable, or refusal to write.

## Optional policy filters (plan only)

Policy is not built in. If you want recurring proposals, such as "review residue", write a small
`.embeadify.toml`; `plan` turns matches into commented-out `close` proposals. See
[`docs/plan-rules.md`](docs/plan-rules.md).

## Development

```bash
python3 scripts/worktree_env.py
. .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python scripts/validate.py
```

Tests are synthetic and offline: a fake `bd` shim on `PATH` implements just enough of the CLI. Read
[`CONTRIBUTING.md`](CONTRIBUTING.md) before submitting fixtures.

## Documentation

- [Documentation index](docs/README.md)
- [Decisions-file grammar](docs/decisions-file.md)
- [emBEADings consumer contract](docs/consumer-contract.md)
- [Plan rules](docs/plan-rules.md)

## Principles

- **Nothing is applied without `--apply`.** Reading, planning, and validating never write.
- **Humans decide.** emBEADify proposes nothing on its own authority; plan output is commented out.
- **The tracker stays authoritative.** Only the public `bd` CLI is used.
- **Reversible by construction.** Every applied batch leaves an undo file.
- **Deterministic and local.** Same inputs, same output; no network, embedding model, or telemetry.

MIT licensed. emBEADify is not affiliated with Beads.
