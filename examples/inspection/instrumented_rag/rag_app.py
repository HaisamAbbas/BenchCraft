"""An instrumented RAG app: it reports the passages it actually used."""

from __future__ import annotations

import json
import sys

try:
    import chromadb  # the production retriever; optional here
except ImportError:
    chromadb = None

DOCS = {"refund": "Refunds may be requested within 30 days of purchase."}


def main() -> None:
    request = json.load(sys.stdin)
    question = str(request.get("input", "")).lower()
    retrieved = [text for key, text in DOCS.items() if key in question]
    answer = retrieved[0] if retrieved else "I could not find that."
    json.dump({"answer": answer, "retrieved": retrieved}, sys.stdout)


if __name__ == "__main__":
    main()
