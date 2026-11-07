"""Read-only access to an existing bridge state DB (CLI status + MCP).

Never creates, migrates or writes the MAIN DB file. ``JobStore(...)`` must NOT be used for
reads because its constructor runs CREATE TABLE IF NOT EXISTS (a write/migration).

Mode (default, always): SQLite ``mode=ro`` + ``PRAGMA query_only`` and ONE deferred read
transaction for the whole ReadOnlyState lifetime, so every query sees the same coherent
snapshot even while a writer commits. Honest filesystem effect: the DB uses WAL, so SQLite
may create or update the ``-shm``/``-wal`` sidecar files next to it (needed for WAL
readers); the main file is not modified. If the directory is not writable and no ``-shm``
exists, the open fails and is reported, not worked around.

``immutable=1`` (no sidecars, no locks) is used ONLY when the caller passes
``assume_static_snapshot=True`` -- meaning the operator has verified the file is a static
copy that no process will write. It is never chosen heuristically: per SQLite docs,
immutable on a file that changes can return wrong results.
Errors never echo filesystem paths or raw SQLite messages.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_LIST_LIMIT = 500
DEFAULT_LIST_LIMIT = 50
_REQUIRED_JOB_COLS = {"task_id", "correlation_id", "status", "attempt", "max_attempts",
                      "backend", "last_error_code"}
_REQUIRED_OUTBOX_COLS = {"event_id", "task_id", "event_type", "delivery", "attempt",
                         "schema_version", "created_at"}


class ReadOnlyStateError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OpenInfo:
    access_mode: str  # "ro_snapshot" | "immutable_operator_asserted"
    has_results_table: bool


class ReadOnlyState:
    def __init__(self, db_path: Path | str, *, assume_static_snapshot: bool = False) -> None:
        self._path = Path(db_path)
        self._static = bool(assume_static_snapshot)
        self._conn: sqlite3.Connection | None = None
        self.info: OpenInfo | None = None

    def __enter__(self) -> "ReadOnlyState":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
            try:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")  # read txn only; nothing to commit
            except sqlite3.Error:
                pass
            finally:
                self._conn.close()
                self._conn = None

    def open(self) -> OpenInfo:
        p = self._path
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            raise ReadOnlyStateError("state_db_absent", "state DB does not exist; not created") from None
        except OSError:
            raise ReadOnlyStateError("state_db_unreadable", "state DB cannot be accessed") from None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise ReadOnlyStateError("state_db_not_regular", "state DB must be a regular file (no symlink)")
        try:
            with open(p, "rb") as fh:
                header = fh.read(100)
        except OSError:
            raise ReadOnlyStateError("state_db_unreadable", "state DB cannot be read") from None
        if len(header) < 100 or not header.startswith(b"SQLite format 3\x00"):
            raise ReadOnlyStateError("state_db_invalid", "file is not a SQLite database")
        uri = p.resolve().as_uri() + "?mode=ro"
        mode = "immutable_operator_asserted" if self._static else "ro_snapshot"
        if self._static:
            uri += "&immutable=1"
        conn = None
        try:
            conn = sqlite3.connect(uri, uri=True, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")  # deferred: snapshot starts at first read below
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "jobs" not in tables or "outbox" not in tables:
                raise ReadOnlyStateError("state_db_schema_unsupported", "jobs/outbox tables missing")
            jcols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
            ocols = {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
            if not (_REQUIRED_JOB_COLS <= jcols and _REQUIRED_OUTBOX_COLS <= ocols):
                raise ReadOnlyStateError("state_db_schema_unsupported", "unexpected jobs/outbox schema")
        except ReadOnlyStateError:
            if conn is not None:
                conn.close()
            raise
        except sqlite3.Error:
            if conn is not None:
                conn.close()
            raise ReadOnlyStateError("state_db_unreadable", "state DB could not be read") from None
        self._conn = conn
        self.info = OpenInfo(access_mode=mode, has_results_table="job_results_v2" in tables)
        return self.info

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        assert self._conn is not None, "open() first"
        try:
            return self._conn.execute(sql, args).fetchall()
        except sqlite3.Error:
            raise ReadOnlyStateError("state_db_unreadable", "state DB query failed") from None

    @staticmethod
    def _job(r: sqlite3.Row) -> dict[str, Any]:
        return {"task_id": r["task_id"], "correlation_id": r["correlation_id"],
                "status": r["status"], "attempt": r["attempt"], "max_attempts": r["max_attempts"],
                "backend": r["backend"], "last_error_code": r["last_error_code"]}

    def get_job(self, task_id: str) -> dict[str, Any] | None:
        rows = self._q("SELECT * FROM jobs WHERE task_id = ?", (task_id,))
        return self._job(rows[0]) if rows else None

    def count_jobs(self) -> int:
        return int(self._q("SELECT COUNT(*) FROM jobs")[0][0])

    def list_jobs(self, *, limit: int = DEFAULT_LIST_LIMIT, offset: int = 0) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        offset = max(0, int(offset))
        rows = self._q("SELECT * FROM jobs ORDER BY rowid DESC LIMIT ? OFFSET ?", (limit, offset))
        return [self._job(r) for r in rows]

    def outbox_for(self, task_id: str | None, *, limit: int = DEFAULT_LIST_LIMIT) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), MAX_LIST_LIMIT))
        if task_id is None:
            rows = self._q("SELECT * FROM outbox ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))
        else:
            rows = self._q("SELECT * FROM outbox WHERE task_id = ? ORDER BY created_at, rowid LIMIT ?",
                           (task_id, limit))
        return [{"event_id": r["event_id"], "task_id": r["task_id"], "event_type": r["event_type"],
                 "delivery": r["delivery"], "attempt": r["attempt"],
                 "schema_version": r["schema_version"]} for r in rows]

    def get_result(self, task_id: str) -> dict[str, Any] | None:
        """Latest strictly validated summary; invalid/legacy rows yield None."""
        from ..runner.grok_result import validate_summary
        if not (self.info and self.info.has_results_table):
            return None
        rows = self._q("SELECT summary_json FROM job_results_v2 WHERE task_id = ?"
                       " ORDER BY attempt DESC LIMIT 1", (task_id,))
        if not rows:
            return None
        try:
            d = json.loads(rows[0][0])
        except (ValueError, TypeError):
            return None
        return d if validate_summary(d) else None
