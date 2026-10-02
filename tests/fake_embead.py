"""A synthetic `embead match` for offline scribe tests.

Knobs (environment): FAKE_EMBEAD_FIXTURE (JSON file {candidate_id: [neighbor, ...]}), else
FAKE_EMBEAD_TITLE_CONTAINS + FAKE_EMBEAD_SIM (every issue in FAKE_BD_DB whose title contains the text
is returned at that similarity), FAKE_EMBEAD_FAIL (exit 3), FAKE_EMBEAD_GARBAGE (print non-JSON).
"""

import json
import os
import sys
from pathlib import Path


def neighbor(issue, similarity, rank):
    closed = issue["status"] == "closed"
    return {
        "issue_id": issue["id"],
        "status": issue["status"],
        "issue_type": issue.get("issue_type", "task"),
        "priority": issue.get("priority"),
        "title": issue["title"],
        "similarity": similarity,
        "rank": rank,
        "is_closed": closed,
        "parent_id": issue.get("parent_id"),
        "parent_status": "live" if issue.get("parent_id") else None,
        "resolution_evidence": {"kind": "close_reason", "text": issue.get("close_reason")}
        if closed and issue.get("close_reason")
        else None,
    }


def main():
    args = sys.argv[1:]
    if os.environ.get("FAKE_EMBEAD_FAIL"):
        print("embead: model unavailable", file=sys.stderr)
        return 3
    if os.environ.get("FAKE_EMBEAD_GARBAGE"):
        print("not json")
        return 0
    path = args[args.index("--candidates-file") + 1]
    candidates = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    fixture = os.environ.get("FAKE_EMBEAD_FIXTURE")
    table = json.loads(Path(fixture).read_text()) if fixture else {}
    issues = json.loads(Path(os.environ["FAKE_BD_DB"]).read_text())
    needle = os.environ.get("FAKE_EMBEAD_TITLE_CONTAINS")
    out = []
    for c in candidates:
        if c["candidate_id"] in table:
            found = table[c["candidate_id"]]
        elif needle:
            sim = float(os.environ.get("FAKE_EMBEAD_SIM", "0.97"))
            found = [neighbor(i, sim, n) for n, i in enumerate(i for i in issues if needle in i["title"])]
        else:
            found = []
        out.append(
            {
                "candidate_id": c["candidate_id"],
                "content_hash": "0" * 64,
                "status": "matches" if found else "no-match",
                "no_match_reason": None if found else "below-min-similarity",
                "records_compared": len(issues),
                "neighbors": found,
            }
        )
    report = {
        "schema_version": 1,
        "report_type": "match",
        "policy": {"read_only": True, "tracker_mutation_allowed": False, "creates_records": False},
        "summary": {
            "candidate_count": len(out),
            "candidates_with_matches": 0,
            "records_compared": len(issues),
        },
        "candidates": out,
    }
    print(json.dumps(report))
    return 0


raise SystemExit(main())
