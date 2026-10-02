"""A synthetic LLM recommender: reads one JSON on stdin, prints a typed recommendation.

FAKE_REC_FILE maps candidate_id -> recommendation object (candidate_id is filled in). FAKE_REC_RAW prints
that text verbatim instead. FAKE_REC_FAIL exits 1. Every stdin payload is appended to FAKE_REC_SEEN.
"""

import json
import os
import sys
from pathlib import Path

payload = json.load(sys.stdin)
if os.environ.get("FAKE_REC_SEEN"):
    with open(os.environ["FAKE_REC_SEEN"], "a") as handle:
        handle.write(json.dumps(payload) + "\n")
if os.environ.get("FAKE_REC_FAIL"):
    sys.exit(1)
if os.environ.get("FAKE_REC_RAW") is not None:
    print(os.environ["FAKE_REC_RAW"])
    sys.exit(0)
cid = payload["candidate"]["candidate_id"]
table = json.loads(Path(os.environ["FAKE_REC_FILE"]).read_text())
rec = table.get(cid, {"action": "create", "confidence": 0.5, "evidence": []})
print(json.dumps({"candidate_id": cid, "policy_version": "fake-llm-1", **rec}))
