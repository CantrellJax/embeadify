"""A synthetic LLM backend for `embeadify-recommend`: reads a prompt on stdin, prints text.

FAKE_LLM_REPLY_FILE: text to print verbatim. FAKE_LLM_ENVELOPE=1 wraps it like `claude -p --output-format
json`. FAKE_LLM_SLEEP: seconds to sleep first (timeout test). FAKE_LLM_FAIL: exit 1. FAKE_LLM_SEEN: every
prompt is appended there (JSON line) so tests can assert on what the model was shown.
"""

import json
import os
import sys
import time
from pathlib import Path

prompt = sys.stdin.read()
if os.environ.get("FAKE_LLM_SEEN"):
    with open(os.environ["FAKE_LLM_SEEN"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(prompt) + "\n")
if os.environ.get("FAKE_LLM_SLEEP"):
    time.sleep(float(os.environ["FAKE_LLM_SLEEP"]))
if os.environ.get("FAKE_LLM_FAIL"):
    sys.exit(1)
text = Path(os.environ["FAKE_LLM_REPLY_FILE"]).read_text(encoding="utf-8")
if os.environ.get("FAKE_LLM_ENVELOPE"):
    text = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text})
sys.stdout.write(text)
