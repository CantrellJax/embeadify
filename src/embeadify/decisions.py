"""The decisions-file grammar: one operation per line."""

from __future__ import annotations

import re
import shlex
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
    "create": "text",
}
CREATE_TYPES = ("task", "bug", "feature", "chore", "epic")
CREATE_KEYS = ("type", "priority", "parent", "title", "body-file")
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


def create_fields(rest: str) -> dict[str, str]:
    """Parse `key=value` words of a `create` line (shell-style quoting, no shell). Raises ValueError."""
    try:
        words = shlex.split(rest, posix=True)
    except ValueError as error:
        raise ValueError(f"bad quoting ({error})") from error
    fields: dict[str, str] = {}
    for word in words:
        key, sep, value = word.partition("=")
        if not sep or key not in CREATE_KEYS:
            raise ValueError(f"expected one of {', '.join(k + '=' for k in CREATE_KEYS)}, got {word!r}")
        if key in fields:
            raise ValueError(f"{key}= given twice")
        if not value or PLACEHOLDER.match(value):
            raise ValueError(f"{key}= needs a real value, got {value!r}")
        fields[key] = value
    if "title" not in fields:
        raise ValueError("`create` needs title=")
    fields.setdefault("type", "task")
    fields.setdefault("priority", "2")
    if fields["type"] not in CREATE_TYPES:
        raise ValueError(f"type= must be one of {', '.join(CREATE_TYPES)}")
    if not re.fullmatch(r"[Pp]?[0-4]", fields["priority"]):
        raise ValueError("priority= must be 0-4 or P0-P4")
    fields["priority"] = fields["priority"][-1]
    return fields


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
    if kind == "create":
        from .sanitize import is_id

        if not is_id(ident):
            raise ValueError(
                f"line {number}: CANDIDATE_ID {ident!r} must be letters, digits, . _ : - (max 96)"
            )
        try:
            create_fields(rest)
        except ValueError as error:
            raise ValueError(f"line {number}: {error}") from error
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
