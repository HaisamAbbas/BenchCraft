"""Manifest worker: `python -m aibench.registry.worker module:attribute`.

Runs in its own process (see `discovery.load_manifests`). Imports the plugin entry point,
which must be an `Evaluator` subclass or a sequence of them, and prints their manifests as
JSON. Nothing is evaluated.
"""

from __future__ import annotations

import importlib
import json
import sys

from aibench.evaluators.protocol import Evaluator


def main(argv: list[str]) -> int:
    if len(argv) != 1 or ":" not in argv[0]:
        print("usage: python -m aibench.registry.worker module:attribute", file=sys.stderr)
        return 64
    module_name, _, attribute = argv[0].partition(":")
    target = getattr(importlib.import_module(module_name), attribute)
    factories = target if isinstance(target, (list, tuple)) else [target]
    manifests = []
    for factory in factories:
        if not (isinstance(factory, type) and issubclass(factory, Evaluator)):
            print(f"{factory!r} is not an Evaluator subclass", file=sys.stderr)
            return 65
        manifests.append(factory.manifest.model_dump(mode="json"))
    json.dump(manifests, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
