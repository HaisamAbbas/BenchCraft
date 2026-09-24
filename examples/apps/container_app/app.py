"""Example application for the aibench `container` runner.

Reads a case as JSON on stdin and answers on stdout. It also reports what it could do
inside its container, so a run records evidence of the isolation it ran under.
"""

import json
import os
import socket
import sys


def attempt(action):
    try:
        action()
        return "allowed"
    except OSError as exc:
        return f"denied ({type(exc).__name__})"


def write(path):
    with open(path, "w") as handle:
        handle.write("x")


request = json.load(sys.stdin)
question = str(request.get("input", "")).lower()
answer = (
    "Refunds are available within 30 days of purchase."
    if "refund" in question
    else "I'm not sure. Please contact support@example.com."
)
environment = {
    "uid": os.getuid(),
    "write_app_dir": attempt(lambda: write("/app/probe")),
    "write_root_fs": attempt(lambda: write("/probe")),
    "write_tmp": attempt(lambda: write("/tmp/probe")),
    "network": attempt(lambda: socket.create_connection(("1.1.1.1", 53), timeout=2).close()),
}
json.dump({"output": answer, "environment": environment}, sys.stdout)
