"""SQLite connection and workspace management (§14). One `Database` instance is the sole
writer for a workspace within this process — the "single writer strategy" for MVP: local,
synchronous CLI usage, not a multi-process server. WAL mode and a busy timeout are enabled as
a backstop against accidental concurrent access, not as a substitute for that convention.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from aibench.storage.migrations import apply_migrations

BUSY_TIMEOUT_MS = 5_000


@dataclass(frozen=True)
class Workspace:
    """The `.aibench/` workspace layout (§14): metadata DB and content-addressed artifacts
    live under a configurable root, kept out of the project's own version-controlled tree by
    convention (see `.gitignore`)."""

    root: Path

    @classmethod
    def at(cls, project_root: Path) -> Workspace:
        return cls(root=(project_root / ".aibench").resolve())

    @property
    def db_path(self) -> Path:
        return self.root / "aibench.db"

    @property
    def artifacts_dir(self) -> Path:
        return self.root / "artifacts"

    def ensure_directories(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)


class Database:
    """Owns the one writer connection for a workspace. `close()`/context-manager exit closes
    the connection; reopening (a fresh `Database.open(...)` call) re-applies migrations
    idempotently and reads back whatever was durably committed — the restart-safety contract
    02-G1/02-G4 exercise."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @classmethod
    def open(cls, db_path: Path) -> Database:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.row_factory = sqlite3.Row
        apply_migrations(conn)
        return cls(conn)

    @classmethod
    def open_workspace(cls, workspace: Workspace) -> Database:
        workspace.ensure_directories()
        return cls.open(workspace.db_path)

    @classmethod
    def open_readonly(cls, db_path: Path) -> Database:
        """Open an existing workspace without creating files or applying migrations.

        Reporting commands use this path so their storage/artifact setup is genuinely
        read-only.  ``query_only`` is set before any repository call; callers must not
        use this connection for lifecycle writes.
        """
        if not db_path.is_file():
            raise FileNotFoundError(db_path)
        conn = sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None
        )
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.row_factory = sqlite3.Row
        return cls(conn)

    @classmethod
    def open_in_memory(cls) -> Database:
        """An in-memory database: no filesystem I/O at all, not even a temp directory. For
        tests that exercise pure repository/migration logic and have no need to prove
        restart-safety (an in-memory database cannot survive a close/reopen — use
        `open`/`open_workspace` against a real path for that)."""
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        apply_migrations(conn)
        return cls(conn)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
