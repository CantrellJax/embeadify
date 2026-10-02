"""Text hygiene shared by `create` and the scribe: control characters out, marker lines defanged."""

from __future__ import annotations

import re

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}")
# C0/C1 controls (keeping tab, newline), line separators, bidi overrides, zero-width characters.
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0d\x0e-\x1f\x7f-\x9f​-‏  ‪-‮⁦-⁩﻿]")
_MARKER_WORD = re.compile(r"(?i)embeadify-(candidate|provenance):")
MARKER_LINE = re.compile(r"^embeadify-(candidate|provenance): (" + ID.pattern + r")(?: .*)?$", re.M)


def is_id(value: object) -> bool:
    return isinstance(value, str) and ID.fullmatch(value) is not None


def defang(text: str) -> str:
    """A producer's text can never contain a line the reconciler would read as a marker."""
    return _MARKER_WORD.sub(lambda m: f"embeadify_{m.group(1).lower()}:", text)


def clean_line(text: str, limit: int) -> str:
    text = text.replace("\r", "\n")
    text = _CTRL.sub("", text)
    text = " ".join(defang(text).split())
    return text[:limit].rstrip()


def clean_block(text: str, limit: int) -> tuple[str, bool]:
    """Return (text, truncated). Newlines and tabs survive; every other control character does not."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = defang(_CTRL.sub("", text)).strip()
    if len(text) > limit:
        return text[:limit].rstrip(), True
    return text, False


def marker_line(kind: str, candidate_id: str) -> str:
    return f"embeadify-{kind}: {candidate_id}"


def find_markers(*texts: object) -> set[str]:
    """Markers as `candidate:ID` / `provenance:ID` found in description or notes text."""
    found: set[str] = set()
    for text in texts:
        if isinstance(text, str):
            found.update(f"{m.group(1)}:{m.group(2)}" for m in MARKER_LINE.finditer(text))
    return found
