"""A support bot whose manifest and imports suggest retrieval it never does.

It answers from a fixed table. chromadb is listed as a dependency and imported only for type
checking; an agent framework appears only in a comment; faiss is used only by a test.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import chromadb  # noqa: F401 - type hints for a retriever that was never wired in

# from langchain.agents import AgentExecutor  (planned, not implemented)

ANSWERS = {"refund": "Refunds are available within 30 days of purchase."}


def main() -> None:
    request = json.load(sys.stdin)
    question = str(request.get("input", "")).lower()
    answer = next((a for k, a in ANSWERS.items() if k in question), "Please contact support.")
    json.dump({"answer": answer}, sys.stdout)


if __name__ == "__main__":
    main()
