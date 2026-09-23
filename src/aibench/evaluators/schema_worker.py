"""JSON Schema worker: `python -m aibench.evaluators.schema_worker`.

Reads `{"schema": ..., "instance": ...}` on stdin and prints the validation errors as JSON.
Used for schemas containing regex keywords (`pattern`, `patternProperties`): Python's `re`
cannot be interrupted, so an untrusted pattern with catastrophic backtracking must run in a
process that can be killed (`validation.validate_untrusted`).
"""

from __future__ import annotations

import json
import sys

from aibench.evaluators.validation import instance_errors


def main() -> int:
    request = json.load(sys.stdin)
    json.dump(instance_errors(request["schema"], request["instance"]), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
