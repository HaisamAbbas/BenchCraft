"""Synthetic known-objective callable for the controlled experiments example."""

from __future__ import annotations

import os
from typing import Any

def respond(payload: dict[str, Any]) -> dict[str, str]:
    mode = os.environ.get("AIBENCH_TUNABLE_ANSWER_MODE", "baseline")
    if mode == "accurate":
        question = str(payload.get("input", ""))
        prefix = "answer "
        return {"output": question[len(prefix) :] if question.startswith(prefix) else "unknown"}
    return {"output": "unknown"}
