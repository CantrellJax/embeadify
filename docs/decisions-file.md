# Decisions-file grammar

One operation per line. Blank lines and lines starting with `#` are ignored. An inline comment starts at
a `#` preceded by two or more spaces (or a tab), so `close demo-1 fixed in PR #12` keeps its `#12`.

| Line | Effect (public `bd` CLI) | Undo |
| --- | --- | --- |
| `close ID reason...` | `bd close ID --reason=...` | `reopen ID` (or `status ID old` if it was not `open`) |
| `parent ID NEW_PARENT` | `bd update ID --parent=NEW_PARENT` (`-` clears) | `parent ID old` (or `-`) |
| `dup ID CANONICAL` | `bd duplicate ID --of=CANONICAL` | `reopen ID` |
| `priority ID N` | `bd update ID --priority=N` (0-4 or P0-P4) | `priority ID old` |
| `label-add ID LABEL` | `bd update ID --add-label=LABEL` | `label-rm ID LABEL` |
| `label-rm ID LABEL` | `bd update ID --remove-label=LABEL` | `label-add ID LABEL` |
| `reopen ID` | `bd reopen ID` | `close ID ...` |
| `status ID STATUS` | `bd update ID --status=STATUS` (not `closed`) | `status ID old` |
| `create CANDIDATE_ID type=task priority=2 parent=ID title="..." body-file=PATH` | `bd create --title=... --type=... --priority=... --description=... [--parent=ID --no-inherit-labels] --json` | `close NEW_ID` (written once the id is known) |
| `note ID text...` | `bd update ID --append-notes=text` | none: append-only, recorded as a comment |

`status` exists mainly so an undo can restore `in_progress`, `blocked`, or `deferred`.

## `create`

With a `parent=`, `--no-inherit-labels` is always passed: `bd create --parent` otherwise copies the parent's labels onto the child (a stray `theme` label once leaked onto 13 real beads). A created bead never inherits labels; set them explicitly.

`CANDIDATE_ID` is a durable key you choose, not a tracker id. Words after it are `key=value` with
shell-style quoting (no shell is involved): `title=` is required; `type=` is `task` (default), `bug`,
`feature`, `chore` or `epic`; `priority=` is 0-4 or P0-P4 (default 2); `parent=` is an existing, open
issue; `body-file=` is a text file (max 1 MB, cleaned and cut to 8,000 characters). Because an inline
comment starts at two spaces before `#`, put no `  #` inside a quoted title.

The description ends with the line `embeadify-candidate: CANDIDATE_ID`. That marker makes `create`
idempotent: if any bead already carries it, the line is a no-op, and after a failed or timed-out
`bd create` the marker is looked up in a fresh snapshot before any retry, so one candidate id yields one
bead. Marker-looking lines inside a body are defanged.

Undo caveats: the new id is unknown until `bd create` answers, so the undo file written first holds a
comment, and the final rewrite adds `close NEW_ID embeadify undo: created for CANDIDATE_ID`. If the process
dies between the write and that rewrite, find the bead by its marker line. Undo closes the bead; it does not
delete it, it stays in history and search, and replaying the undo does not touch edits made afterwards.
`create` cannot appear in an undo file. Other ops in the same file cannot refer to the new bead by id.

## Validation

All ops are checked, in file order, against one snapshot of the tracker (`bd list --all --limit 0
--json`), tracking the effect of earlier lines. Any of these refuses the whole file before anything is
written:

- unknown issue id, parent, or canonical id (for `create`: an unknown or closed `parent=`, an unreadable `body-file`);
- `close` / `dup` on an issue that is already closed;
- `close` / `dup` on an issue with open children or open blocked-by dependents
  (override: `--force-dependents`; closing the dependents earlier in the same file also satisfies it);
- `parent` equal to the issue itself, a parent cycle, or a closed parent (override:
  `--allow-closed-parent`);
- a placeholder such as `<fill-in>` that was not replaced.

An op that would change nothing (same priority, label already present, and so on) is reported as a no-op
and skipped. Ops on the same issue run in file order inside one worker; ops on different issues run in
parallel with no ordering guarantee. Use `-j 1` (a true serial path, same undo, drift and report behavior) when order across issues matters. The default is `-j 2` (max 8); more than 2 prints a note, because parallel `bd` writes load the shared beads server (a 77-call burst alongside a heavy DB job once took it down).

## The undo file

Written before the first write to `./embeadify-undo-<UTC timestamp>.decisions` (or `--undo-file`), in
reverse order, and rewritten after the run to cover only ops that were applied or failed (ops skipped for
drift are left out so replaying cannot overwrite someone else's change). `embeadify undo` is lenient about
state that already matches, so replaying after a partial failure is safe.

## Exit codes

`0` success; `1` an op failed or was skipped for drift; `2` usage error, invalid decisions, `bd` missing
or unusable, or a refusal to write.

## `--json`

`apply` and `undo` print one JSON object: `mode`, `target`, `undo_file`, `summary`
(`ok`, `skipped`, `failed`, `refused`, `would_apply`), and an `ops` array in file order with `op`,
`verdict`, `outcome`, `old`, `new`, `detail`, `message`, and `retry` for failures.
