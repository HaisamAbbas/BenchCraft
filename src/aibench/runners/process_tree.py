"""Process-tree containment for one CLI invocation (03-T2: "process-tree cleanup on
supported platforms").

Invariant: no process started by an invocation outlives that invocation.

- POSIX: the child starts in a new session (`start_new_session=True`), so it leads its own
  process group; cleanup sends SIGKILL to the whole group. A descendant that calls
  `setsid()` itself leaves the group and is not contained.
- Windows: the child is assigned to a Job Object created with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`; `TerminateJobObject` kills every process in the job,
  including descendants whose parent already exited. A descendant spawned in the instant
  between process creation and job assignment is not contained.

This is cleanup, not isolation: a subprocess is not a sandbox (§16).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import IO, Any

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL


def spawn_kwargs() -> dict[str, Any]:
    """Extra `create_subprocess_exec` arguments that make the child a tree root."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


class ProcessTree:
    """Kill handle for a spawned child and all of its descendants. When `contained` is
    False (Windows job assignment failed) this kills nothing; the caller must kill the
    direct child itself."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.contained = False
        self._closed = False
        self._job: int | None = None
        if sys.platform == "win32":
            self._attach_job()
        else:
            self.contained = True  # the child leads its own process group

    def _attach_job(self) -> None:
        # An explicit platform block (not an early return) so type checkers on POSIX
        # treat the Windows API calls below as unreachable.
        if sys.platform == "win32":
            job = _k32.CreateJobObjectW(None, None)
            if not job:
                return
            info = _ExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            process = _k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, self.pid)
            ok = bool(process) and bool(
                _k32.SetInformationJobObject(
                    job,
                    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                )
            )
            ok = ok and bool(_k32.AssignProcessToJobObject(job, process))
            if process:
                _k32.CloseHandle(process)
            if ok:
                self._job = job
                self.contained = True
            else:
                _k32.CloseHandle(job)

    def kill(self) -> None:
        """Kill the whole tree now. Safe to call repeatedly; a no-op after `close()`.
        Windows kills through the job handle, so a recycled PID is never targeted. POSIX
        signals the process group by number: if the child has already been reaped and its
        group is empty, a new group could in principle reuse that ID in the brief window
        before `close()` — an accepted, documented limit (ADR 0002)."""
        if self._closed or not self.contained:
            return
        if sys.platform == "win32":
            if self._job is not None:
                _k32.TerminateJobObject(self._job, 1)
        else:
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass  # the group is already gone

    def close(self) -> None:
        """End of invocation: kill any descendant still running, release handles."""
        self.kill()
        self._closed = True
        if sys.platform == "win32" and self._job is not None:
            _k32.CloseHandle(self._job)
            self._job = None


_CONTAINED_READ_CHUNK = 65_536
_CONTAINED_POLL_SECONDS = 0.05
_CONTAINED_JOIN_SECONDS = 2.0


@dataclass(frozen=True)
class ContainedResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    truncated: bool


def run_contained(
    argv: Sequence[str],
    *,
    timeout: float,
    env: Mapping[str, str],
    input_bytes: bytes = b"",
    max_output_bytes: int = 1_048_576,
    cwd: str | None = None,
) -> ContainedResult:
    """Run a short-lived helper process under hard limits: wall-clock `timeout`, and at
    most `max_output_bytes` kept per stream, enforced *while it runs* — once either stream
    exceeds the cap the whole tree is killed and the excess is discarded, never buffered or
    spooled. The tree is always killed at the end. Pipe readers are joined with a bounded
    wait, so a descendant outside containment that keeps a pipe open cannot block us."""
    proc = subprocess.Popen(  # argv list, never a shell
        list(argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
        **spawn_kwargs(),
    )
    tree = ProcessTree(proc.pid)
    overflow = threading.Event()
    kept: list[bytearray] = [bytearray(), bytearray()]

    def drain(stream: IO[bytes], sink: bytearray) -> None:
        total = 0
        try:
            while chunk := stream.read1(_CONTAINED_READ_CHUNK):  # type: ignore[attr-defined]
                total += len(chunk)
                room = max_output_bytes - len(sink)
                if room > 0:
                    sink.extend(chunk[:room])
                if total > max_output_bytes:
                    overflow.set()  # keep reading (and discarding) so the writer never blocks
        except (OSError, ValueError):
            pass  # pipe closed underneath us during cleanup

    def feed() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(input_bytes)
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    assert proc.stdout is not None and proc.stderr is not None
    threads = [
        threading.Thread(target=drain, args=(proc.stdout, kept[0]), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, kept[1]), daemon=True),
        threading.Thread(target=feed, daemon=True),
    ]
    for thread in threads:
        thread.start()

    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while proc.poll() is None and not overflow.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            overflow.wait(min(remaining, _CONTAINED_POLL_SECONDS))
    finally:
        tree.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for thread in threads:
            thread.join(timeout=_CONTAINED_JOIN_SECONDS)
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:
                pass
    return ContainedResult(
        None if timed_out else proc.returncode,
        bytes(kept[0]),
        bytes(kept[1]),
        timed_out,
        overflow.is_set(),
    )
