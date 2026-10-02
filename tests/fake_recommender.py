"""A synthetic LLM recommender: reads one JSON on stdin, prints a typed recommendation.

FAKE_REC_FILE maps candidate_id -> recommendation object (candidate_id is filled in). FAKE_REC_RAW_FILE prints
that file verbatim instead. FAKE_REC_FAIL exits 1. Every stdin payload is appended to FAKE_REC_SEEN.
FAKE_REC_SLEEP: seconds to sleep; FAKE_REC_SLEEP_MAP: JSON file {candidate_id: seconds} (wins over it).
FAKE_REC_TIMES: each call appends {"id", "start", "end"} (wall-clock) so a test can measure overlap.
"""

import json
import os
import sys
import time
from pathlib import Path

payload = json.load(sys.stdin)
if os.environ.get("FAKE_REC_SEEN"):
    with open(os.environ["FAKE_REC_SEEN"], "a") as handle:
        handle.write(json.dumps(payload) + "\n")
started = time.time()
sleep = float(os.environ.get("FAKE_REC_SLEEP", "0"))
if os.environ.get("FAKE_REC_SLEEP_MAP"):
    sleep = json.loads(Path(os.environ["FAKE_REC_SLEEP_MAP"]).read_text()).get(
        payload["candidate"]["candidate_id"], sleep
    )
time.sleep(sleep)
if os.environ.get("FAKE_REC_TIMES"):
    with open(os.environ["FAKE_REC_TIMES"], "a") as handle:
        handle.write(
            json.dumps({"id": payload["candidate"]["candidate_id"], "start": started, "end": time.time()})
            + "\n"
        )
if os.environ.get("FAKE_REC_FAIL"):
    sys.exit(1)
if os.environ.get("FAKE_REC_RAW_FILE"):
    sys.stdout.write(Path(os.environ["FAKE_REC_RAW_FILE"]).read_text())
    sys.exit(0)
cid = payload["candidate"]["candidate_id"]
table = json.loads(Path(os.environ["FAKE_REC_FILE"]).read_text())
rec = table.get(cid, {"action": "create", "confidence": 0.5, "evidence": []})
print(json.dumps({"candidate_id": cid, "policy_version": "fake-llm-1", **rec}))
