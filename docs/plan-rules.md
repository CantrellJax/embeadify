# Plan rules (optional)

`embeadify plan` reads `./.embeadify.toml` when present (or `--rules FILE`; `--no-rules` ignores it) and
appends commented-out `close` proposals for matching issues. Rules only affect `plan`; `apply` never
reads them, and policy is never built in. Rules read one `bd list` snapshot (or `--snapshot-file`).

```toml
# Review residue: leftover review follow-ups nobody touched.
[[filter]]
name = "review-residue"
title_regex = "^Review: "          # Python regex, searched in the title
statuses = ["open"]                # default: ["open"]
max_priority = 2                   # only priority P2 or LESS urgent (bd: 0 is most urgent)
require_no_dependents = true       # nothing is blocked by it and it has no children
require_no_comments = true         # comment_count must be present and 0
reason = "review residue: no longer needed"
```

Every proposal is a `# close ID reason   # evidence: ...` line. A human uncomments the ones they approve,
then runs `embeadify apply`. A condition that cannot be evaluated (for example a missing comment count)
never matches.
