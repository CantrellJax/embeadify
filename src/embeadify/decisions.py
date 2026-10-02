"""The decisions-file grammar: one operation per line."""

from __future__ import annotations

import re
from dataclasses import dataclass

# kind -> (minimum args after the id, takes free text)
KINDS = {
    "close": "text",
    "parent": "one",
    "dup": "one",
    "priority": "one",
    "label-add": "one",
    "label-rm": "one",
    "reopen": "none",
    "note": "text",
    "status": "one",
}
PLACEHOLDER = re.compile(r"^<[^<>]*>$")
# An inline comment starts at a `#` preceded by two or more spaces or a tab.
INLINE_COMMENT = re.compile(r"(?:[ ]{2,}|\t)#")


class GrammarError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Op:
    kind: str
    id: str
    arg: str = ""
    line: int = 0

    def render(self) -> str:
        parts = [self.kind, self.id]
        if self.arg != "" or self.kind in ("parent",):
            parts.append(self.arg if self.arg != "" else "-")
        return " ".join(parts)


def parse_line(text: str, number: int = 0) -> Op | None:
    stripped = text.strip()
    if not stripped or stripped.startswith("#"):
        return None
    stripped = INLINE_COMMENT.split(stripped, maxsplit=1)[0].strip()
    pieces = stripped.split(None, 2)
    kind = pieces[0].lower()
    if kind not in KINDS:
        raise ValueError(f"line {number}: unknown operation {pieces[0]!r}")
    if len(pieces) < 2:
        raise ValueError(f"line {number}: `{kind}` needs an issue id")
    ident = pieces[1]
    rest = pieces[2].strip() if len(pieces) > 2 else ""
    shape = KINDS[kind]
    if shape == "none" and rest:
        raise ValueError(f"line {number}: `{kind}` takes no arguments after the id")
    if shape == "one":
        if not rest:
            raise ValueError(f"line {number}: `{kind} {ident}` needs one argument")
        if len(rest.split()) != 1:
            raise ValueError(f"line {number}: `{kind}` takes exactly one argument, got {rest!r}")
    if shape == "text" and not rest:
        raise ValueError(f"line {number}: `{kind} {ident}` needs text (a reason or note)")
    if PLACEHOLDER.match(rest) or PLACEHOLDER.match(ident):
        raise ValueError(f"line {number}: placeholder {rest or ident!r} was not filled in")
    if kind == "parent" and rest == "-":
        rest = ""
    return Op(kind, ident, rest, number)


def parse_text(text: str) -> list[Op]:
    ops: list[Op] = []
    errors: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        try:
            op = parse_line(line, number)
        except ValueError as error:
            errors.append(str(error))
            continue
        if op is not None:
            ops.append(op)
    if errors:
        raise GrammarError(errors)
    return ops
