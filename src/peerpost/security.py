"""Output sanitation for untrusted peer messages."""

from __future__ import annotations

import re


ANSI_RE = re.compile(
    r"(?:\x1b\[[0-?]*[ -/]*[@-~])"
    r"|(?:\x1b\][^\x07\x1b]*(?:\x07|\x1b\\))"
    r"|(?:\x1b[@-_])"
)


def strip_control_chars(value: str) -> str:
    """Remove terminal control sequences while preserving text newlines and tabs."""
    value = ANSI_RE.sub("", value)
    cleaned: list[str] = []
    for char in value:
        code = ord(char)
        if char in ("\n", "\t"):
            cleaned.append(char)
        elif code < 32 or code == 127 or 0x80 <= code <= 0x9F:
            continue
        else:
            cleaned.append(char)
    return "".join(cleaned)


def indent_body(body: str, prefix: str = "  ") -> str:
    body = strip_control_chars(body)
    return ("\n" + prefix).join(body.splitlines()) if body else ""
