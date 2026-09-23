"""Example black-box legacy CLI app: plain-text answer on stdout, nothing else.

Used with `output_mode: "text"`. Only its output text, exit status and wall time are
observable; everything else (retrieval, tools, usage, cost) must be reported as unknown.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    request = json.load(sys.stdin)
    question = str(request.get("input", ""))
    words = len(question.split())
    print(f"You asked a {words}-word question. Our team will follow up by email.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
