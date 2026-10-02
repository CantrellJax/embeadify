## Summary

Describe the user-visible outcome and why it belongs in the write-side executor.

## Validation

- [ ] I ran `python scripts/validate.py` from this checkout's isolated environment.
- [ ] Behavior changes have synthetic regression coverage (fake `bd` shim).
- [ ] Grammar, exit-code, or JSON changes update `docs/`.
- [ ] Nothing is applied without `--apply`, and `plan` output is still fully commented out.

## Privacy and repository hygiene

- [ ] This PR contains no private issue text, ids, hostnames, tokens, decisions or undo files, or logs.
- [ ] `git status --short` contains only intentional files.

## Compatibility

Note any CLI, exit-code, or `bd`-version impact. Write "None" when unchanged.
