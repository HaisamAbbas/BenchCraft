"""Answers that look like the application failing while reporting success.

An application often catches its own exceptions and returns them as an ordinary answer with
a success status (`{"answer": "Error: Invalid API Key"}`). BenchCraft records that as an
answer, and every metric then scores an error message: a whole run of zeros that says
nothing about the application. This flags such answers so a report can say so first. It is a
heuristic on the start of the answer, deliberately narrow, and only ever a warning.
"""

from __future__ import annotations

import re
from typing import Any

from aibench.core.models import deep_unfreeze

_HEAD_CHARS = 400
_PATTERNS = (
    re.compile(r"^\s*(?:error|exception|traceback|fatal)\b\s*[:(\-]", re.IGNORECASE),
    re.compile(r"^\s*traceback \(most recent call last\)", re.IGNORECASE),
    re.compile(r"\berror code:\s*\d{3}\b", re.IGNORECASE),
    re.compile(
        r"\b(?:invalid[_ ]api[_ ]key|incorrect api key|rate limit (?:reached|exceeded)"
        r"|too many requests)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:internal server error|bad gateway|service unavailable)\b", re.IGNORECASE),
)


def _text(output: Any) -> str:
    """The answer's text: a string, or the string values of a JSON answer, in order."""
    value = deep_unfreeze(output)
    if isinstance(value, str):
        return value
    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return " ".join(parts)


def looks_like_error(output: Any) -> bool:
    head = _text(output)[:_HEAD_CHARS]
    return any(pattern.search(head) for pattern in _PATTERNS)
