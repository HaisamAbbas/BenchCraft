"""Example CLI chatbot for the aibench CLI protocol (JSON on stdin, JSON on stdout).

Deterministic and dependency-free: answers a few support questions by keyword. It reports
only an output — it has no retrieval, tools or model usage, so none are claimed.

    echo '{"case_id": "c1", "input": "What is your refund policy?"}' | python cli_chatbot.py
"""

from __future__ import annotations

import json
import sys

ANSWERS = (
    (("refund", "money back", "return"), "Refunds are available within 30 days of purchase."),
    (("ship", "shipping", "deliver"), "We ship to over 40 countries; delivery takes 5-10 days."),
    (("warranty", "guarantee"), "All products carry a one-year limited warranty."),
)
FALLBACK = "I'm not sure. Please contact support@example.com."


def answer(question: str) -> str:
    lowered = question.lower()
    for keywords, reply in ANSWERS:
        if any(keyword in lowered for keyword in keywords):
            return reply
    return FALLBACK


def main() -> int:
    try:
        request = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"invalid JSON on stdin: {exc}", file=sys.stderr)
        return 2
    question = request.get("input")
    if not isinstance(question, str):
        print("expected a string 'input' field", file=sys.stderr)
        return 2
    json.dump({"output": answer(question)}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
