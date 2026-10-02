# Contributing

Thanks for helping build emBEADify.

## Project boundaries

- Nothing is ever applied without an explicit `--apply`. Dry run is the default for every writing command.
- emBEADify decides nothing. No heuristics that pick a parent, a duplicate, or a close reason.
- Invoke `bd` only through its public CLI. Pass the caller's environment through unchanged; never bypass
  or disable a `bd` guard.
- Standard library only. No network access, embedding model, or telemetry.
- Keep `plan` output fully commented out.

## Privacy

Issues often contain private product plans, customer context, incidents, and source references.

- Use synthetic fixtures (`demo-1` style ids). Do not commit real tracker exports, reports, decisions
  files, undo files, hostnames, tokens, or logs.
- Scrub issue IDs, people, organizations, domains, and operational identifiers from bug reports.
- Reproduce defects against the fake `bd` shim in `tests/` before opening an issue or pull request.

## Development workflow

Each clone or Git worktree must own its virtual environment:

```bash
python3 scripts/worktree_env.py
. .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python scripts/validate.py
```

The validation script is the canonical entry point. It checks formatting, then lint, then the full test
suite, and exits at the first failure. It also verifies that the editable `embeadify` import resolves to
the current checkout.

Before opening a pull request:

- run `python scripts/validate.py` from the checkout-local environment;
- keep behavior changes covered by synthetic tests;
- update `docs/` when the grammar, exit codes, or JSON output change; and
- confirm `git status --short` contains no decisions, undo, report, or private artifact.
