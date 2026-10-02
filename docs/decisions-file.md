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
| `note ID text...` | `bd update ID --append-notes=text` | none: append-only, recorded as a comment |

`status` exists mainly so an undo can restore `in_progress`, `blocked`, or `deferred`.

## Validation

All ops are checked, in file order, against one snapshot of the tracker (`bd list --all --limit 0
--json`), tracking the effect of earlier lines. Any of these refuses the whole file before anything is
written:

- unknown issue id, parent, or canonical id;
- `close` / `dup` on an issue that is already closed;
- `close` / `dup` on an issue with open children or open blocked-by dependents
  (override: `--force-dependents`; closing the dependents earlier in the same file also satisfies it);
- `parent` equal to the issue itself, a parent cycle, or a closed parent (override:
  `--allow-closed-parent`);
- a placeholder such as `<fill-in>` that was not replaced.

An op that would change nothing (same priority, label already present, and so on) is reported as a no-op
and skipped. Ops on the same issue run in file order inside one worker; ops on different issues run in
parallel with no ordering guarantee. Use `-j 1` when order across issues matters.

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
