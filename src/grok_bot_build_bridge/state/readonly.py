"""Strictly read-only access to an existing bridge state DB (CLI status + MCP).

Never creates, migrates or writes the main DB. ``JobStore(...)`` must NOT be used for
reads because its constructor runs CREATE TABLE IF NOT EXISTS (a write/migration).

WAL tradeoff (SQLite >= 3.22, verified empirically on 3.46):
- DB in WAL mode with NO ``-wal`` sidecar present (clean shutdown, the normal at-rest
  state): opened with ``mode=ro&immutable=1`` -> no sidecar files are created and no
  locks taken. Risk: if a writer starts concurrently, this read may see a stale or
  torn snapshot; errors are reported as ``state_db_unreadable``, never as data.
- ``-wal`` sidecar present (a writer is active or crashed): opened with ``mode=ro``.
  The main DB is not written, but SQLite may update the EXISTING ``-shm`` index and
  may create a missing ``-shm``. This is documented, not hidden.
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
    access_mode: str  # "immutable" | "ro_with_existing_wal"
    has_results_table: bool


class ReadOnlyState:
    def __init__(self, db_path: Path | str) -> None:
        self._path = Path(db_path)
        self._conn: sqlite3.Connection | None = None
        self.info: OpenInfo | None = None

    def __enter__(self) -> "ReadOnlyState":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._conn is not None:
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
        is_wal = header[18] == 2 or header[19] == 2
        wal_present = os.path.exists(str(p) + "-wal")
        uri = p.resolve().as_uri() + "?mode=ro"
        mode = "ro_with_existing_wal" if (is_wal and wal_present) else "immutable"
        if mode == "immutable":
            uri += "&immutable=1"
        try:
            conn = sqlite3.connect(uri, uri=True, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "jobs" not in tables or "outbox" not in tables:
                conn.close()
                raise ReadOnlyStateError("state_db_schema_unsupported", "jobs/outbox tables missing")
            jcols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
            ocols = {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
            if not (_REQUIRED_JOB_COLS <= jcols and _REQUIRED_OUTBOX_COLS <= ocols):
                conn.close()
                raise ReadOnlyStateError("state_db_schema_unsupported", "unexpected jobs/outbox schema")
        except sqlite3.Error:
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
