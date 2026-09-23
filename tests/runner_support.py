"""Shared helpers for runner tests: real example apps, loopback servers, process probes."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import sys
import threading
import time
from collections.abc import Coroutine, Iterator
from http.server import HTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, TypeVar

from aibench.core.models import ApplicationSpec, BenchmarkCase, Fixture, ReferenceAnswer

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_APPS = REPO_ROOT / "examples" / "apps"
MISBEHAVING = REPO_ROOT / "tests" / "fixtures" / "apps" / "misbehaving_cli.py"
SENTINEL = "GOLDEN-SENTINEL-7f3a9c"

_T = TypeVar("_T")


def run(coro: Coroutine[Any, Any, _T]) -> _T:
    return asyncio.run(coro)


def load_example(name: str) -> ModuleType:
    path = EXAMPLE_APPS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"example_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def serving(server: HTTPServer) -> Iterator[str]:
    """Serve on a background thread; yields the base URL."""
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def cli_spec(argv: list[str], **transport: Any) -> ApplicationSpec:
    return ApplicationSpec.model_validate(
        {
            "application_id": "test-cli",
            "runner": "cli",
            "target": "test",
            "output_binding": transport.pop("output_binding", {}),
            "input_binding": transport.pop("input_binding", {}),
            "effects": transport.pop("effects", "none"),
            "transport": {"kind": "cli", "argv": argv, **transport},
        }
    )


def misbehaving(mode: str, *args: str, **transport: Any) -> ApplicationSpec:
    return cli_spec([sys.executable, str(MISBEHAVING), mode, *args], **transport)


def http_spec(url: str, **fields: Any) -> ApplicationSpec:
    transport = {"kind": "http", "url": url, **fields.pop("transport", {})}
    return ApplicationSpec.model_validate(
        {
            "application_id": fields.pop("application_id", "test-http"),
            "runner": "http",
            "target": url,
            "transport": transport,
            **fields,
        }
    )


def golden_case(
    case_id: str = "case-1", text: str = "What is your refund policy?"
) -> BenchmarkCase:
    """A case whose judge-only data (reference answer, reference context, hidden fixture)
    all carry the sentinel. Only `input` and the app-visible fixture may reach the app."""
    return BenchmarkCase(
        case_id=case_id,
        input=text,
        reference=ReferenceAnswer(answer=f"answer {SENTINEL}", context=(f"ctx {SENTINEL}",)),
        fixtures=(
            Fixture(name="hidden", content={"secret": SENTINEL}),
            Fixture(name="visible", content={"note": "shown to the app"}, app_visible=True),
        ),
        metadata={"grader_note": SENTINEL},
    )


def process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_until_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.05)
    return False


def read_pid(path: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return int(path.read_text(encoding="utf-8"))
        time.sleep(0.05)
    raise AssertionError(f"grandchild PID never written to {path}")
