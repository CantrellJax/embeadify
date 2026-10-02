# Changelog

## Unreleased

Initial scaffold.

- `embeadify plan`: commented-out decisions template from an emBEADings schema-v1 `orphans`,
  `mentions`, or `triage` report, with optional `.embeadify.toml` filters.
- `embeadify apply`: dry run by default; one snapshot, validation, undo file first, bounded
  parallel `bd` execution, drift skips, failure reporting, `--json` summary.
- `embeadify undo`: replays an undo file through the same engine.
- `embeadify doctor`: `bd` presence, version, workspace, and the redacted write target.
