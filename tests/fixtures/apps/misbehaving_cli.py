"""Test-only CLI application with deliberate failure modes. Usage: misbehaving_cli.py MODE [ARG]

echo              output = {"stdin": <parsed stdin>, "env": environ, "argv": argv}
sleep SECONDS     sleep, then answer
spawn PIDFILE     start a long-lived grandchild, record its PID, then hang
orphan PIDFILE    start a long-lived grandchild that keeps our stdout open, then exit 0
flood BYTES       write BYTES of stdout without ever finishing a JSON document
stderr BYTES      write BYTES to stderr, then answer normally
exit CODE         write a diagnostic to stderr and exit with CODE
badjson           print text that is not JSON
wrongfield        print JSON without an "output" field
report            answer with retrieval/tool/usage/cost fields, some empty or malformed
hostile KIND      print hostile-but-parseable JSON: deep | bigint | nan | surrogate
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

GRANDCHILD = [sys.executable, "-c", "import time; time.sleep(60)"]


def answer(extra: dict[str, object] | None = None) -> None:
    json.dump({"output": "done", **(extra or {})}, sys.stdout)
    sys.stdout.flush()


def main() -> int:
    mode = sys.argv[1]
    arg = sys.argv[2] if len(sys.argv) > 2 else ""
    raw = sys.stdin.read()

    if mode == "echo":
        answer_payload = {"stdin": json.loads(raw), "env": dict(os.environ), "argv": sys.argv}
        json.dump({"output": answer_payload}, sys.stdout)
    elif mode == "sleep":
        time.sleep(float(arg))
        answer()
    elif mode == "spawn":
        child = subprocess.Popen(GRANDCHILD)
        with open(arg, "w", encoding="utf-8") as handle:
            handle.write(str(child.pid))
        time.sleep(60)
    elif mode == "orphan":
        child = subprocess.Popen(GRANDCHILD)  # inherits our stdout/stderr pipes
        with open(arg, "w", encoding="utf-8") as handle:
            handle.write(str(child.pid))
        answer()
    elif mode == "flood":
        chunk = "x" * 65_536
        remaining = int(arg)
        sys.stdout.write('{"output": "')
        while remaining > 0:
            sys.stdout.write(chunk[:remaining])
            remaining -= len(chunk)
        sys.stdout.flush()
        time.sleep(60)  # never finishes on its own
    elif mode == "stderr":
        sys.stderr.write("e" * int(arg))
        sys.stderr.flush()
        answer()
    elif mode == "exit":
        print("fatal: configured failure", file=sys.stderr)
        return int(arg)
    elif mode == "badjson":
        print("this is not json")
    elif mode == "wrongfield":
        json.dump({"answer": "done"}, sys.stdout)
    elif mode == "report":
        answer(
            {
                "docs": [{"text": "doc one"}, {"text": "doc two"}],
                "tools": [],
                "usage": "not-an-object",
            }
        )
    elif mode == "hostile":
        documents = {
            "deep": "[" * 200_000,
            "bigint": '{"output": ' + "1" * 5000 + "}",
            "nan": '{"output": NaN}',
            "overflow_float": '{"output":"ok","cost":1e999}',
            "surrogate": '{"output": "\\ud800"}',  # a valid JSON escape for a lone surrogate
        }
        sys.stdout.write(documents[arg])
    else:
        print(f"unknown mode {mode}", file=sys.stderr)
        return 64
    return 0


if __name__ == "__main__":
    sys.exit(main())
