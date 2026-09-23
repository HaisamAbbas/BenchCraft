"""Quickstart fixture: a small support assistant with retrieval (standard library only).

Protocol (aibench CLI runner): one JSON request on stdin, `{"case_id": ..., "input": ...}`;
one JSON response on stdout, `{"answer": ..., "retrieved": [...]}`. It retrieves the
best-matching passages from a built-in knowledge base by word overlap and answers from the
top passage, so what it retrieved is observable evidence.

Two behaviours are deliberate, so the first report has something real to show:
- a question about the warranty retrieves the shipping passage (a retrieval failure: the
  answer is wrong, and the retrieved passages show why);
- a question about an order's status fails: the "order service" is unavailable, so the app
  exits with an error (an application failure, not a low score).
"""

from __future__ import annotations

import json
import re
import sys

KNOWLEDGE = {
    "refunds": (
        "Refunds are available within 30 days of purchase.",
        "refund refunds money back return returns purchase days",
    ),
    "shipping": (
        "We ship to over 40 countries; delivery takes 5-10 days.",
        "ship shipping deliver delivery international countries warranty",
    ),
    "warranty": (
        "All products carry a one-year limited warranty.",
        "guarantee defect defective broken repair",
    ),
    "hours": (
        "Support is open Monday to Friday, 9am to 5pm.",
        "hours open support monday friday weekend time",
    ),
    "password": (
        "Reset your password from the sign-in page with 'Forgot password'.",
        "password reset sign login account forgot",
    ),
    "cancel": (
        "You can cancel a subscription at any time from Account settings.",
        "cancel cancellation subscription stop account settings",
    ),
    "payment": (
        "We accept Visa, Mastercard and PayPal.",
        "pay payment card visa mastercard paypal accept",
    ),
    "invoice": (
        "Invoices are emailed after each payment and listed under Billing.",
        "invoice invoices receipt billing email",
    ),
    "contact": (
        "Email support@example.com to reach a person.",
        "contact email person human reach talk",
    ),
}


def retrieve(question: str, k: int = 2) -> list[str]:
    words = set(re.findall(r"[a-z]+", question.lower()))
    scored = sorted(
        ((len(words & set(keys.split())), topic) for topic, (_, keys) in KNOWLEDGE.items()),
        key=lambda item: (-item[0], item[1]),
    )
    return [KNOWLEDGE[topic][0] for score, topic in scored[:k] if score > 0]


def main() -> int:
    request = json.load(sys.stdin)
    question = str(request.get("input", ""))
    if "order" in question.lower() and "status" in question.lower():
        print("order service unavailable: cannot look up order status", file=sys.stderr)
        return 2
    passages = retrieve(question)
    answer = passages[0] if passages else "I don't know."
    print(json.dumps({"answer": answer, "retrieved": passages}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
