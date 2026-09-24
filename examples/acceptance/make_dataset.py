"""Regenerate `rag100.jsonl`: 100 support questions over the `rag_service` corpus.

Each case is checked against the service's own retrieval: ordinary questions must retrieve
their topic's document, and the 8 injected ones (a decoy word added) must not. Run from the
repository root: `python examples/acceptance/make_dataset.py`.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("rag_service", HERE / "rag_service.py")
assert _spec is not None and _spec.loader is not None
r = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(r)

T = {
    "refunds": [
        "Can I get a refund?",
        "How do I get my money back?",
        "What is your refund window?",
        "Can I return this and get a refund?",
        "Do refunds take long?",
        "Is a refund possible after a week?",
        "How long do I have for a refund?",
        "Can I get money back on a sale item?",
        "Where do I ask for a refund?",
        "Are refunds available?",
    ],
    "warranty": [
        "Is there a warranty?",
        "What does the warranty cover?",
        "My device is broken, is it under warranty?",
        "How long is the guarantee?",
        "Is a defective item covered?",
        "Do you offer a warranty on accessories?",
        "What is the warranty period?",
        "Does the guarantee cover defects?",
        "Is my broken charger covered by warranty?",
        "Can I claim warranty for a defect?",
    ],
    "shipping": [
        "Do you ship internationally?",
        "How long does delivery take?",
        "Which countries do you ship to?",
        "When will my delivery arrive?",
        "Do you ship to Canada?",
        "What are shipping times?",
        "Can you deliver to Europe?",
        "How fast is shipping?",
        "Is delivery available in Asia?",
        "How many countries do you ship to?",
    ],
    "hours": [
        "What are your support hours?",
        "Are you open on Monday?",
        "Is support open on the weekend?",
        "Until when are you open on Friday?",
        "What hours can I call?",
        "When are you open?",
        "Are support hours in UTC?",
        "Do you work on the weekend?",
        "What time do you open on Monday?",
        "What are the opening hours?",
    ],
    "password": [
        "How do I reset my password?",
        "I forgot my password.",
        "How can I sign in again?",
        "Where do I reset my login?",
        "Can support reset my password?",
        "My login fails, what now?",
        "How do I change my password?",
        "Password reset link please.",
        "I cannot sign in to my account.",
        "Where is the password reset?",
    ],
    "cancel": [
        "How do I cancel?",
        "Can I cancel my subscription?",
        "How do I stop my plan?",
        "How can I unsubscribe?",
        "Is there a fee to cancel?",
        "Where do I cancel my subscription?",
        "Can I stop billing for my subscription?",
        "How to unsubscribe from the service?",
        "Cancel my account please.",
        "Can I cancel anytime?",
    ],
    "payment": [
        "Which payment methods do you accept?",
        "Can I pay with PayPal?",
        "Do you take Visa?",
        "Can I pay by card?",
        "Is Mastercard accepted for payment?",
        "Which card types do you accept?",
        "Can I pay with a debit card?",
        "Do you accept PayPal payment?",
        "How can I pay?",
        "Is Visa accepted?",
    ],
    "invoice": [
        "Where is my invoice?",
        "Can I get a receipt?",
        "How do I view billing history?",
        "Are invoices sent automatically?",
        "Where are my invoices listed?",
        "I need an invoice for my company.",
        "Can I download my bill?",
        "Where do I find billing documents?",
        "Is my invoice emailed?",
        "How do I get my receipt?",
    ],
    "privacy": [
        "Do you sell my data?",
        "What is your privacy policy?",
        "How is personal data used?",
        "Is my personal information safe?",
        "Do you share data with others?",
        "Where is the privacy policy?",
        "Do you sell personal information?",
        "How do you protect privacy?",
        "Is my data private?",
        "What data do you keep?",
    ],
    "contact": [
        "How do I contact support?",
        "Can I talk to a person?",
        "What is your support email?",
        "How do I reach a human?",
        "Can I email the team?",
        "I want to talk to someone.",
        "How can I contact you?",
        "Is there a person I can email?",
        "Who do I contact for help?",
        "Can a human help me?",
    ],
}
INJECT = {
    ("refunds", 2): "abroad",
    ("warranty", 4): "abroad",
    ("hours", 1): "urgently",
    ("password", 7): "urgently",
    ("payment", 8): "abroad",
    ("invoice", 5): "urgently",
    ("privacy", 8): "abroad",
    ("cancel", 0): "urgently",
}
rows, n, bad = [], 0, []
for topic, qs in T.items():
    for i, q in enumerate(qs):
        n += 1
        decoy = INJECT.get((topic, i))
        question = q.rstrip("?.") + f" {decoy}?" if decoy else q
        got = r.retrieve(question)
        top = got[0]["doc_id"] if got else None
        if (top == topic) == bool(decoy):
            bad.append((question, top))
        rows.append(
            {
                "case_id": f"rag-{n:03d}",
                "input": question,
                "expected_output": r.CORPUS[topic],
                "context": [r.CORPUS[topic]],
                "metadata": {"topic": topic, "injected": "retrieval_failure" if decoy else "none"},
            }
        )
assert not bad, bad
(HERE / "rag100.jsonl").write_text("\n".join(json.dumps(x) for x in rows) + "\n", encoding="utf-8")
print(len(rows), "cases,", sum(x["metadata"]["injected"] != "none" for x in rows), "injected")
