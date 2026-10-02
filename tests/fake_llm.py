"""A synthetic LLM backend for `embeadify-recommend`: reads a prompt on stdin, prints text.

FAKE_LLM_REPLY_FILE: text to print verbatim. FAKE_LLM_ENVELOPE=1 wraps it like `claude -p --output-format
json`. FAKE_LLM_USAGE: JSON usage object added to the envelope. FAKE_LLM_SLEEP: seconds to sleep first
(timeout test). FAKE_LLM_FAIL: exit 1.
FAKE_LLM_SEEN: every prompt is appended there (JSON line) so tests can assert on what the model was shown.
"""

import json
import os
import sys
import time
from pathlib import Path

from locked_append import append_line

prompt = sys.stdin.read()
if os.environ.get("FAKE_LLM_SEEN"):
    append_line(os.environ["FAKE_LLM_SEEN"], json.dumps(prompt))
if os.environ.get("FAKE_LLM_SLEEP"):
    time.sleep(float(os.environ["FAKE_LLM_SLEEP"]))
if os.environ.get("FAKE_LLM_FAIL"):
    sys.exit(1)
text = Path(os.environ["FAKE_LLM_REPLY_FILE"]).read_text(encoding="utf-8")
if os.environ.get("FAKE_LLM_ENVELOPE"):
    envelope = {"type": "result", "subtype": "success", "is_error": False, "result": text}
    if os.environ.get("FAKE_LLM_USAGE"):  # synthetic usage, like `claude -p --output-format json`
        envelope["usage"] = json.loads(os.environ["FAKE_LLM_USAGE"])
        envelope["total_cost_usd"] = 0.01
    text = json.dumps(envelope)
sys.stdout.write(text)
