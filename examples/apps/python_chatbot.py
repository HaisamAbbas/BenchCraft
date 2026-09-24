"""Example Python callable for the aibench `python` runner (§7 "Python callable").

`respond` receives the app-visible input and returns a JSON document. aibench calls it in a
fresh interpreter per case through its shim; the module needs nothing from aibench.
"""

from __future__ import annotations

from typing import Any

ANSWERS = (
    (("refund", "money back", "return"), "Refunds are available within 30 days of purchase."),
    (("ship", "shipping", "deliver"), "We ship to over 40 countries; delivery takes 5-10 days."),
    (("warranty", "guarantee"), "All products carry a one-year limited warranty."),
)
FALLBACK = "I'm not sure. Please contact support@example.com."


def respond(payload: dict[str, Any]) -> dict[str, Any]:
    question = str(payload.get("input", "")).lower()
    for keywords, reply in ANSWERS:
        if any(keyword in question for keyword in keywords):
            return {"output": reply}
    return {"output": FALLBACK}


async def respond_async(payload: dict[str, Any]) -> str:
    """The same answer from a coroutine function, returned as a bare string."""
    return respond(payload)["output"]


def explode(payload: dict[str, Any]) -> dict[str, Any]:
    raise RuntimeError("the application failed on purpose")
