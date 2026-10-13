"""Live JSONL and diagnostic output for durable run events."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

RUN_EVENT_SCHEMA = "aibench.run-event/1"


class EventLogLock:
    """Prevent concurrent processes from appending to the same event log."""

    def __init__(self, path: Path) -> None:
        self.path = path.with_name(path.name + ".lock")
        self._file: BinaryIO | None = None

    @property
    def is_held(self) -> bool:
        return self._file is not None

    def acquire(self, *, blocking: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+b")
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if not blocking:
                            raise
                        time.sleep(0.05)
            else:
                import fcntl

                fcntl_api: Any = fcntl
                flock = fcntl_api.flock
                lock_mode = fcntl_api.LOCK_EX
                if not blocking:
                    lock_mode |= fcntl_api.LOCK_NB
                flock(lock_file.fileno(), lock_mode)
        except (ImportError, OSError) as exc:
            lock_file.close()
            raise ValueError(f"event log is already in use by another writer: {self.path}") from exc
        self._file = lock_file

    def release(self) -> None:
        lock_file = self._file
        if lock_file is None:
            return
        self._file = None
        try:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl_api: Any = fcntl
                fcntl_api.flock(lock_file.fileno(), fcntl_api.LOCK_UN)
        finally:
            lock_file.close()


def event_document(run_id: str, event: dict[str, Any]) -> dict[str, Any]:
    """Stable standalone representation used by JSONL exports and live logs."""
    return {
        "schema": RUN_EVENT_SCHEMA,
        "run_id": run_id,
        "sequence": event["sequence"],
        "event_type": event["event_type"],
        "created_at": event["created_at"],
        "payload": event["payload"],
    }


def last_logged_sequence(path: Path, run_id: str) -> int:
    """Return the latest record for this run, refusing to append to an unknown file."""
    if not path.exists():
        return 0
    if not path.is_file():
        raise ValueError(f"event log path is not a regular file: {path}")
    latest = 0
    previous_by_run: dict[str, int] = {}
    with path.open("r", encoding="utf-8") as existing:
        for line_number, line in enumerate(existing, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"event log line {line_number} is not valid JSON") from exc
            if (
                not isinstance(record, dict)
                or record.get("schema") != RUN_EVENT_SCHEMA
                or not isinstance(record.get("run_id"), str)
                or not isinstance(record.get("event_type"), str)
                or not isinstance(record.get("created_at"), str)
                or not isinstance(record.get("payload"), dict)
                or type(record.get("sequence")) is not int
                or record["sequence"] < 1
            ):
                raise ValueError(f"event log line {line_number} is not a BenchCraft run event")
            previous = previous_by_run.get(record["run_id"], 0)
            if record["sequence"] <= previous:
                raise ValueError(f"event log line {line_number} is out of sequence")
            previous_by_run[record["run_id"]] = record["sequence"]
            if record["run_id"] == run_id:
                latest = max(latest, record["sequence"])
    if path.stat().st_size:
        with path.open("rb") as existing_bytes:
            existing_bytes.seek(-1, 2)
            if existing_bytes.read(1) != b"\n":
                raise ValueError("event log must end with a newline before it can be appended")
    return latest


def latest_stored_sequence(workspace: Workspace, run_id: str) -> int:
    """Read the committed high-water mark without loading the full run event history."""
    db = Database.open_workspace(workspace)
    try:
        row = db.connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM run_events WHERE run_id = ?", (run_id,)
        ).fetchone()
        return int(row[0])
    finally:
        db.close()


class RunEventStream:
    """Tail one run from an independent SQLite connection while its worker executes."""

    def __init__(
        self,
        workspace: Path,
        run_id: str,
        *,
        log_file: Path | None = None,
        verbose: bool = False,
        log_lock: EventLogLock | None = None,
        wait_for_log_lock: bool = False,
    ) -> None:
        self.workspace = Workspace.at(workspace)
        self.run_id = run_id
        self.verbose = verbose
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._file: TextIO | None = None
        self._log_lock: EventLogLock | None = None
        self._error: str | None = None
        self._sequence = 0
        if log_file is not None:
            target = log_file.expanduser().resolve()
            if target == self.workspace.db_path.resolve():
                raise ValueError("--log-file cannot target the workspace database")
            self._log_lock = log_lock or EventLogLock(target)
            if self._log_lock.path.resolve() != target.with_name(target.name + ".lock").resolve():
                raise ValueError("event log lock does not match the selected log file")
            if not self._log_lock.is_held:
                self._log_lock.acquire(blocking=wait_for_log_lock)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                self._sequence = last_logged_sequence(target, run_id)
                stored_sequence = latest_stored_sequence(self.workspace, run_id)
                if self._sequence > stored_sequence:
                    raise ValueError("event log sequence is newer than the workspace run history")
                self._file = target.open("a", encoding="utf-8", newline="\n")
            except Exception:
                self._log_lock.release()
                raise

    def start(self) -> None:
        if self._file is None and not self.verbose:
            return
        self._thread = threading.Thread(target=self._tail, name="aibench-run-events", daemon=True)
        try:
            self._thread.start()
        except RuntimeError:
            if self._file is not None:
                self._file.close()
            if self._log_lock is not None:
                self._log_lock.release()
            raise

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self._file is not None:
            try:
                self._file.flush()
            except OSError as exc:
                self._error = self._error or str(exc)
            try:
                self._file.close()
            except OSError as exc:
                self._error = self._error or str(exc)
        if self._log_lock is not None:
            self._log_lock.release()
        if self._error:
            sys.stderr.write(f"warning: run event logging stopped: {self._error}\n")
            sys.stderr.flush()

    def _tail(self) -> None:
        db = None
        try:
            db = Database.open_workspace(self.workspace)
            storage = Storage(db)
            sequence = self._sequence
            while True:
                events = storage.list_run_events(self.run_id, after=sequence)
                for event in events:
                    sequence = int(event["sequence"])
                    document = event_document(self.run_id, event)
                    if self._file is not None:
                        self._file.write(
                            json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n"
                        )
                        self._file.flush()
                    if self.verbose:
                        sys.stderr.write(
                            json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n"
                        )
                        sys.stderr.flush()
                if self._stop.is_set() and not events:
                    break
                self._stop.wait(0.1)
        except Exception as exc:  # noqa: BLE001 - logging must never fail the benchmark run
            self._error = str(exc)
        finally:
            if db is not None:
                db.close()
