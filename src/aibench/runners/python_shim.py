"""Entry point for the Python callable runner, run by path in the application's own
interpreter. Standard library only: the application's environment need not have aibench.

    python python_shim.py [--reset] [--path DIR ...] TARGET < input.json

TARGET is `module:function` or `path/to/file.py:function`. The input is read as JSON from
stdin and passed as the only argument (a coroutine function is run with asyncio). The
result is written to stdout as JSON: an object as it is, any other value as
`{"output": value}`. With --reset, the function receives the seed and its return value is
ignored. Exceptions go to stderr with exit code 1; bad usage exits 2.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import importlib.util
import inspect
import json
import sys
import traceback
from pathlib import Path
from typing import Any

# Run by path, Python puts this file's directory first on sys.path; the application must
# not import aibench's own modules by accident.
if sys.path and Path(sys.path[0]).resolve() == Path(__file__).resolve().parent:
    sys.path.pop(0)


def _load(target: str) -> Any:
    location, _, name = target.rpartition(":")
    if location.endswith(".py"):
        path = Path(location).resolve()
        spec = importlib.util.spec_from_file_location(path.stem, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[path.stem] = module
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(location)
    function = getattr(module, name)
    if not callable(function):
        raise TypeError(f"{target} is not callable")
    return function


def main(argv: list[str]) -> int:
    reset = False
    paths: list[str] = []
    args = list(argv)
    while args and args[0].startswith("--"):
        flag = args.pop(0)
        if flag == "--reset":
            reset = True
        elif flag == "--path" and args:
            paths.append(args.pop(0))
        else:
            print(f"unknown option {flag}", file=sys.stderr)
            return 2
    if len(args) != 1:
        print("usage: python_shim.py [--reset] [--path DIR ...] TARGET", file=sys.stderr)
        return 2
    sys.path[:0] = paths
    try:
        # UTF-8 on both pipes whatever the platform's locale encoding is.
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "null")
        # Whatever the callable prints is diagnostics: stdout carries only the result.
        with contextlib.redirect_stdout(sys.stderr):
            function = _load(args[0])
            result = function(payload)
            if inspect.isawaitable(result):
                result = asyncio.run(_await(result))
    except Exception:  # noqa: BLE001 - reported to the harness as an application failure
        traceback.print_exc()
        return 1
    if reset:
        return 0
    document = result if isinstance(result, dict) else {"output": result}
    try:
        encoded = json.dumps(document, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        print(f"the callable returned a value that is not JSON: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


async def _await(awaitable: Any) -> Any:
    return await awaitable


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
