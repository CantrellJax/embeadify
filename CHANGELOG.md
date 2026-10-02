# Changelog

## Unreleased

Initial scaffold.

- `embeadify plan`: commented-out decisions template from an emBEADings schema-v1 `orphans`,
  `mentions`, or `triage` report, with optional `.embeadify.toml` filters.
- `embeadify apply`: dry run by default; one snapshot, validation, undo file first, bounded
  parallel `bd` execution, drift skips, failure reporting, `--json` summary.
- `embeadify undo`: replays an undo file through the same engine.
- `create CANDIDATE_ID ...` decisions op: idempotent by a marker line, reconciled after ambiguous writes,
  undo is a close.
- `embeadify scribe submit|status|receipts|run|report`: bead-intake scribe, stage 1 (shadow mode by
  default, one executor, create as the fallback). See `docs/scribe.md`.
- Scribe placement: created beads are placed by hint, source bead, similar neighbor, `type_parent`, then
  `default_parent`, else flagged `unplaced` (listed by `scribe report`).
- `embeadify-recommend`: opt-in reference LLM recommender (stdlib only, backend via `EMBEADIFY_LLM_CMD`,
  strict validation, falls back to create). Example policy in `examples/scribe-policy.toml`.
- Scribe trainee loop: `scribe replay` (shadow replay of existing beads with temporal fairness), `judge-pack`,
  `label` (append-only `labels.jsonl`), `metrics`, `tune` (offline threshold grid, never edits policy). Shadow log
  rows now also record `candidate`, `recommender`, `thresholds`. See `docs/scribe-trainee.md`.
- `embeadify doctor`: `bd` presence, version, workspace, and the redacted write target.
- Scribe owner rules: the executor never folds, drops or dups candidates or targets that touch prod data,
  money, privacy or security, are claimed or owner-held, or close only on evidence; a closed neighbor needs
  a dated owner quote; question candidates are routed (`kind`, `scribe routes`); the scribe refuses its own
  events at submit; `guard_forced_create` is counted per guard. See `docs/scribe.md` "Owner rules".
