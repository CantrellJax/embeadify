# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |

Upgrade to the newest published technical preview before reporting a defect when practical.

## Reporting a vulnerability

Use GitHub's private vulnerability-reporting flow when the **Report a vulnerability** button is
available on the repository's Security page. Do not include sensitive tracker content, credentials,
hostnames, private issue IDs, decisions or undo files, or reproduction archives in a public issue.

If private reporting is unavailable, open a minimal public issue requesting a private contact channel.
Include only the affected emBEADify version and a high-level component name. A maintainer will arrange a
private exchange before requesting reproduction details.

## Security posture

emBEADify writes to a tracker, so its guarantees are about restraint: dry run by default, no write
without `--apply`, validation of every op before the first write, an undo file before the first write,
and no bypass of `bd`'s own guards. It makes no network calls of its own and sends no telemetry; the
caller's environment (including `BEADS_DOLT_*` credentials) is passed to `bd` unchanged and never printed.

Security-sensitive areas include subprocess argument handling (arguments are passed as a list, never
through a shell), secret redaction in messages and `doctor`, undo-file correctness, drift detection,
and the refusal to write when `bd` cannot report its target. Decisions, undo files, and `--json`
summaries name real issues: treat them as potentially sensitive project data.
