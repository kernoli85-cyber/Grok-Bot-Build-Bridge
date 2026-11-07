"""SQLite transactional job state + outbox.

Own app DB only. Does not store prompts/transcripts/secrets by default.
HTTP accepted ≠ bot processed (no false exactly-once).
Cancellation marks state only — does not kill a process tree (see process_control).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .clock import Clock, Jitter
from .paths import StorePaths, ensure_private_db_file
from .types import (
    ALLOWED_TRANSITIONS,
    EVENT_SCHEMA_VERSION,
    TERMINAL_JOB,
    UNCERTAIN_JOB,
    DeliveryState,
    JobRecord,
    JobStatus,
    OutboxEvent,
)

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  task_id TEXT PRIMARY KEY,
  correlation_id TEXT NOT NULL,
  status TEXT NOT NULL,
  backend TEXT,
  cwd TEXT,
  attempt INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 5,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  lease_owner TEXT,
  lease_until REAL,
  last_error_code TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_corr ON jobs(correlation_id);

CREATE TABLE IF NOT EXISTS outbox (
  event_id TEXT PRIMARY KEY,
  correlation_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  schema_version INTEGER NOT NULL,
  delivery TEXT NOT NULL,
  attempt INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 8,
  created_at REAL NOT NULL,
  next_attempt_at REAL NOT NULL,
  payload_json TEXT NOT NULL,
  last_error TEXT,
  dedupe_key TEXT,
  UNIQUE(task_id, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(delivery, next_attempt_at);

-- Added v0.16.3: bounded, allowlisted structured run result (never raw stdout).
CREATE TABLE IF NOT EXISTS job_results (
  task_id TEXT PRIMARY KEY,
  summary_json TEXT NOT NULL,
  created_at REAL NOT NULL
);

-- Added v0.16.4: strict versioned results, one per (task, attempt), written only
-- in the same transaction as the live lease owner's terminal transition.
-- The v0.16.3 job_results table is left untouched and never read (unvalidated).
CREATE TABLE IF NOT EXISTS job_results_v2 (
  task_id TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  schema TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (task_id, attempt)
);
"""


class StoreError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class JobStore:
    def __init__(
        self,
        db_path: Path | str,
        *,
        clock: Clock | None = None,
        jitter: Jitter | None = None,
        base_retry_sec: float = 1.0,
        max_retry_sec: float = 60.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.clock = clock or Clock()
        self.jitter = jitter or Jitter(seed=1)
        self.base_retry_sec = base_retry_sec
        self.max_retry_sec = max_retry_sec
        ensure_private_db_file(self.db_path)
        self._init_db()

    @classmethod
    def open_default(cls, **kwargs: Any) -> "JobStore":
        return cls(StorePaths.default().db_path, **kwargs)

    def _connect(self) -> sqlite3.Connection:
        ensure_private_db_file(self.db_path)
        conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            conn.close()
            raise
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(EVENT_SCHEMA_VERSION),),
            )
        ensure_private_db_file(self.db_path)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """Autocommit connection that is ALWAYS closed (``with sqlite3.connect()``
        alone only scopes a transaction and leaks the handle until GC)."""
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass  # original error wins; close() discards the txn anyway
                raise
        finally:
            conn.close()

    def _retry_delay(self, attempt: int) -> float:
        # attempt is 1-based for delay exponent
        exp = min(self.max_retry_sec, self.base_retry_sec * (2 ** max(0, attempt - 1)))
        # jitter in [0.5, 1.0] * exp
        return exp * self.jitter.uniform(0.5, 1.0)

    def _row_job(self, row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            task_id=row["task_id"],
            correlation_id=row["correlation_id"],
            status=JobStatus(row["status"]),
            backend=row["backend"],
            cwd=row["cwd"],
            attempt=int(row["attempt"]),
            max_attempts=int(row["max_attempts"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            lease_owner=row["lease_owner"],
            lease_until=row["lease_until"],
            last_error_code=row["last_error_code"],
        )

    def _row_event(self, row: sqlite3.Row) -> OutboxEvent:
        return OutboxEvent(
            event_id=row["event_id"],
            correlation_id=row["correlation_id"],
            task_id=row["task_id"],
            event_type=row["event_type"],
            schema_version=int(row["schema_version"]),
            delivery=DeliveryState(row["delivery"]),
            attempt=int(row["attempt"]),
            max_attempts=int(row["max_attempts"]),
            created_at=float(row["created_at"]),
            next_attempt_at=float(row["next_attempt_at"]),
            payload=json.loads(row["payload_json"]),
            last_error=row["last_error"],
            dedupe_key=row["dedupe_key"],
        )

    def enqueue_job(
        self,
        *,
        task_id: str,
        correlation_id: str | None = None,
        backend: str | None = None,
        cwd: str | None = None,
        max_attempts: int = 5,
    ) -> JobRecord:
        if not task_id or not task_id.strip():
            raise StoreError("task_id_empty", "task_id required")
        if max_attempts < 1:
            raise StoreError("max_attempts_invalid", "max_attempts must be >= 1")
        # Reject obvious secret-like cwd markers? keep cwd as path metadata only
        corr = (correlation_id or task_id).strip()
        now = self.clock.time()
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
            ).fetchone()
            if existing:
                # Idempotent: return existing; do not reset terminal/completed
                return self._row_job(existing)
            conn.execute(
                """
                INSERT INTO jobs(task_id, correlation_id, status, backend, cwd,
                  attempt, max_attempts, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (task_id, corr, JobStatus.QUEUED.value, backend, cwd, max_attempts, now, now),
            )
            self._insert_event(
                conn,
                task_id=task_id,
                correlation_id=corr,
                event_type="job.queued",
                payload={"status": JobStatus.QUEUED.value, "backend": backend},
                dedupe_key="queued",
            )
            row = conn.execute(
                "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
            ).fetchone()
            return self._row_job(row)

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str,
        correlation_id: str,
        event_type: str,
        payload: dict[str, Any],
        dedupe_key: str | None,
        max_attempts: int = 8,
    ) -> OutboxEvent | None:
        # Strip forbidden keys if somehow passed
        safe_payload = {
            k: v
            for k, v in payload.items()
            if k
            not in {
                "prompt",
                "transcript",
                "secret",
                "token",
                "key",
                "authorization",
                "webhook_key",
            }
        }
        now = self.clock.time()
        event_id = str(uuid.uuid4())
        try:
            conn.execute(
                """
                INSERT INTO outbox(
                  event_id, correlation_id, task_id, event_type, schema_version,
                  delivery, attempt, max_attempts, created_at, next_attempt_at,
                  payload_json, last_error, dedupe_key
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    event_id,
                    correlation_id,
                    task_id,
                    event_type,
                    EVENT_SCHEMA_VERSION,
                    DeliveryState.PENDING.value,
                    max_attempts,
                    now,
                    now,
                    json.dumps(safe_payload, separators=(",", ":")),
                    dedupe_key,
                ),
            )
        except sqlite3.IntegrityError:
            # Idempotent enqueue of same dedupe_key
            return None
        row = conn.execute(
            "SELECT * FROM outbox WHERE event_id = ?", (event_id,)
        ).fetchone()
        return self._row_event(row)

    MAX_RESULT_SUMMARY_BYTES = 4096

    def commit_job_result(
        self,
        task_id: str,
        *,
        owner: str,
        summary: dict[str, Any],
        terminal: JobStatus,
        error_code: str | None = None,
    ) -> JobRecord:
        """Atomically persist a validated result AND the terminal transition + outbox.

        Only the current, unexpired lease owner of a RUNNING job may commit. If the
        job is cancelled/terminal/needs_review or the lease is stale, nothing is
        written (an expired lease flips to needs_review without the result).
        """
        from ..runner.grok_result import SUMMARY_SCHEMA, validate_summary

        if terminal not in {JobStatus.SUCCEEDED, JobStatus.NEEDS_REVIEW, JobStatus.FAILED}:
            raise StoreError("illegal_transition", "result commit needs a terminal outcome")
        if terminal is not JobStatus.SUCCEEDED and not error_code:
            raise StoreError("invalid_result", "non-success commit needs error_code")
        if not validate_summary(summary):
            raise StoreError("invalid_result", "summary failed strict schema validation")
        if terminal is JobStatus.SUCCEEDED and summary["parse_status"] != "ok":
            raise StoreError("invalid_result", "success requires a parsed result")
        data = json.dumps(summary, sort_keys=True, allow_nan=False, separators=(",", ":"))
        if len(data.encode("utf-8")) > self.MAX_RESULT_SUMMARY_BYTES:
            raise StoreError("result_too_large", "result summary exceeds bound")
        post_error: StoreError | None = None
        out: JobRecord | None = None
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                raise StoreError("job_not_found", f"unknown task_id {task_id!r}")
            job = self._row_job(row)
            if job.status == JobStatus.CANCELLED:
                raise StoreError("cancelled", "job cancelled; result not committed")
            job, post_error = self._guard_live_owner(conn, job, owner, op="commit_result")
            if post_error is None:
                if job.status != JobStatus.RUNNING:
                    raise StoreError("illegal_transition", f"commit from {job.status.value}")
                try:
                    conn.execute(
                        "INSERT INTO job_results_v2(task_id, attempt, schema, summary_json, created_at)"
                        " VALUES (?,?,?,?,?)",
                        (task_id, job.attempt, SUMMARY_SCHEMA, data, self.clock.time()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise StoreError("result_exists", "result already committed") from exc
                kw: dict[str, Any] = dict(clear_lease=True, expect_status=JobStatus.RUNNING)
                if terminal is JobStatus.SUCCEEDED:
                    out = self._transition(conn, task_id, terminal, event_type="job.succeeded",
                                           dedupe_key="succeeded", **kw)
                elif terminal is JobStatus.NEEDS_REVIEW:
                    out = self._transition(
                        conn, task_id, terminal, error_code=error_code,
                        event_type="job.needs_review",
                        dedupe_key=f"needs_review:{error_code}:{job.attempt}",
                        extra_payload={"auto_requeue": False, "reason": error_code}, **kw)
                else:
                    out = self._transition(
                        conn, task_id, terminal, error_code=error_code, bump_attempt=True,
                        event_type="job.failed",
                        dedupe_key=f"failed:{job.attempt + 1}:{error_code}", **kw)
        if post_error is not None:
            raise post_error
        assert out is not None
        return out

    def get_job_result(self, task_id: str) -> dict[str, Any] | None:
        """Latest validated result for a task (v2 only); invalid rows are not returned."""
        from ..runner.grok_result import validate_summary

        with self._conn() as conn:
            row = conn.execute(
                "SELECT summary_json FROM job_results_v2 WHERE task_id = ?"
                " ORDER BY attempt DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        if not row:
            return None
        try:
            d = json.loads(row[0])
        except ValueError:
            return None
        return d if validate_summary(d) else None

    def get_job(self, task_id: str) -> JobRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
            ).fetchone()
            return self._row_job(row) if row else None

    def list_jobs(self, *, status: JobStatus | None = None) -> list[JobRecord]:
        with self._conn() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC",
                    (status.value,),
                ).fetchall()
            return [self._row_job(r) for r in rows]

    def _transition(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        new_status: JobStatus,
        *,
        error_code: str | None = None,
        lease_owner: str | None = None,
        lease_until: float | None = None,
        clear_lease: bool = False,
        bump_attempt: bool = False,
        event_type: str | None = None,
        dedupe_key: str | None = None,
        extra_payload: dict[str, Any] | None = None,
        expect_status: JobStatus | None = None,
    ) -> JobRecord:
        row = conn.execute(
            "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            raise StoreError("job_not_found", f"unknown task_id {task_id!r}")
        job = self._row_job(row)
        if job.status == new_status:
            return job  # idempotent no-op
        if job.status in TERMINAL_JOB:
            raise StoreError(
                "terminal_job",
                f"cannot transition from terminal status {job.status.value}",
            )
        allowed = ALLOWED_TRANSITIONS.get(job.status, frozenset())
        if new_status not in allowed:
            raise StoreError(
                "illegal_transition",
                f"{job.status.value} -> {new_status.value} not allowed",
            )
        now = self.clock.time()
        attempt = job.attempt + (1 if bump_attempt else 0)
        owner = None if clear_lease else (lease_owner if lease_owner is not None else job.lease_owner)
        until = None if clear_lease else (lease_until if lease_until is not None else job.lease_until)
        err = error_code if error_code is not None else job.last_error_code
        if expect_status is not None:
            cur = conn.execute(
                """
                UPDATE jobs SET status=?, attempt=?, updated_at=?, lease_owner=?,
                  lease_until=?, last_error_code=? WHERE task_id=? AND status=?
                """,
                (
                    new_status.value,
                    attempt,
                    now,
                    owner,
                    until,
                    err,
                    task_id,
                    expect_status.value,
                ),
            )
            if cur.rowcount != 1:
                raise StoreError(
                    "race_lost",
                    f"expected status {expect_status.value} for {task_id}; concurrent update",
                )
        else:
            conn.execute(
                """
                UPDATE jobs SET status=?, attempt=?, updated_at=?, lease_owner=?,
                  lease_until=?, last_error_code=? WHERE task_id=?
                """,
                (new_status.value, attempt, now, owner, until, err, task_id),
            )
        if event_type:
            payload = {"status": new_status.value, "attempt": attempt}
            if err:
                payload["error_code"] = err
            if extra_payload:
                payload.update(extra_payload)
            self._insert_event(
                conn,
                task_id=task_id,
                correlation_id=job.correlation_id,
                event_type=event_type,
                payload=payload,
                dedupe_key=dedupe_key or f"{event_type}:{attempt}:{new_status.value}",
            )
        row2 = conn.execute(
            "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
        ).fetchone()
        return self._row_job(row2)

    def claim_job(
        self, task_id: str, *, owner: str, lease_sec: float = 30.0
    ) -> JobRecord:
        if not owner or not owner.strip():
            raise StoreError("owner_empty", "lease owner required")
        now = self.clock.time()
        post_error: StoreError | None = None
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
            ).fetchone()
            if not row:
                raise StoreError("job_not_found", f"unknown task_id {task_id!r}")
            job = self._row_job(row)
            # Lease expiry policy:
            # - CLAIMED (not yet running): safe to return to QUEUED
            # - RUNNING: NEVER auto-requeue — needs_review (commit then signal)
            if job.lease_until is not None and job.lease_until <= now:
                if job.status == JobStatus.CLAIMED:
                    self._transition(
                        conn,
                        task_id,
                        JobStatus.QUEUED,
                        clear_lease=True,
                        event_type="job.lease_expired",
                        dedupe_key=f"lease_expired:{int(now)}",
                    )
                    job = self._row_job(
                        conn.execute(
                            "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
                        ).fetchone()
                    )
                elif job.status == JobStatus.RUNNING:
                    self._transition(
                        conn,
                        task_id,
                        JobStatus.NEEDS_REVIEW,
                        clear_lease=True,
                        error_code="lease_expired_while_running",
                        event_type="job.needs_review",
                        dedupe_key=f"needs_review:lease:{int(now)}",
                        extra_payload={
                            "reason": "lease_expired_while_running",
                            "auto_requeue": False,
                        },
                    )
                    post_error = StoreError(
                        "needs_review",
                        "lease expired while running; marked needs_review (no auto-requeue)",
                    )
            if post_error is None:
                if job.status in TERMINAL_JOB:
                    raise StoreError(
                        "terminal_job",
                        f"cannot transition from terminal status {job.status.value}",
                    )
                if job.status == JobStatus.NEEDS_REVIEW:
                    raise StoreError(
                        "needs_review",
                        "job needs_review; use explicit_retry (no auto-requeue)",
                    )
                if job.status != JobStatus.QUEUED:
                    raise StoreError(
                        "not_claimable",
                        f"job status {job.status.value} not claimable",
                    )
                return self._transition(
                    conn,
                    task_id,
                    JobStatus.CLAIMED,
                    lease_owner=owner,
                    lease_until=now + lease_sec,
                    event_type="job.claimed",
                    dedupe_key=f"claimed:{owner}:{int(now)}",
                    extra_payload={"lease_owner": owner},
                    expect_status=JobStatus.QUEUED,
                )
        if post_error is not None:
            raise post_error
        raise StoreError("claim_failed", "unreachable claim state")

    def renew_lease(
        self, task_id: str, *, owner: str, lease_sec: float = 30.0
    ) -> JobRecord:
        """Extend a live owner's lease. Never resurrects terminal/cancelled/needs_review.

        If the lease already expired while RUNNING, mark needs_review (no auto-requeue)
        and raise lease_lost. Does not re-queue CLAIMED on this path.
        """
        if not owner or not owner.strip():
            raise StoreError("owner_empty", "lease owner required")
        if lease_sec <= 0 or lease_sec > 86400:
            raise StoreError("lease_invalid", "lease_sec out of bounds")
        now = self.clock.time()
        post_error: StoreError | None = None
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
            ).fetchone()
            if not row:
                raise StoreError("job_not_found", f"unknown task_id {task_id!r}")
            job = self._row_job(row)
            if job.status in TERMINAL_JOB:
                raise StoreError(
                    "terminal_job",
                    f"cannot renew lease for terminal status {job.status.value}",
                )
            if job.status == JobStatus.NEEDS_REVIEW:
                raise StoreError(
                    "needs_review",
                    "cannot renew lease for needs_review; use explicit_retry",
                )
            if job.status not in {JobStatus.CLAIMED, JobStatus.RUNNING}:
                raise StoreError(
                    "not_renewable",
                    f"status {job.status.value} cannot renew lease",
                )
            if job.lease_owner != owner:
                raise StoreError("lease_mismatch", "heartbeat owner mismatch")
            if job.lease_until is not None and job.lease_until <= now:
                if job.status == JobStatus.RUNNING:
                    self._transition(
                        conn,
                        task_id,
                        JobStatus.NEEDS_REVIEW,
                        clear_lease=True,
                        error_code="lease_expired_while_running",
                        event_type="job.needs_review",
                        dedupe_key=f"needs_review:lease_hb:{int(now)}",
                        extra_payload={
                            "reason": "lease_expired_while_running",
                            "auto_requeue": False,
                            "via": "renew_lease",
                        },
                    )
                    post_error = StoreError(
                        "lease_lost",
                        "lease expired while running; marked needs_review (no auto-requeue)",
                    )
                else:
                    raise StoreError(
                        "lease_expired",
                        "lease expired before running; reclaim via claim_job only",
                    )
            else:
                until = now + lease_sec
                conn.execute(
                    "UPDATE jobs SET lease_until=?, updated_at=? WHERE task_id=?",
                    (until, now, task_id),
                )
                row2 = conn.execute(
                    "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
                ).fetchone()
                return self._row_job(row2)
        if post_error is not None:
            raise post_error
        raise StoreError("renew_failed", "unreachable renew state")

    def _guard_live_owner(
        self, conn, job: JobRecord, owner: str | None, *, op: str
    ) -> tuple[JobRecord, StoreError | None]:
        """Refuse commits from stale/lost owners.

        If lease expired while RUNNING, transition to needs_review inside the
        caller's transaction and return a post-commit StoreError (so the flip
        is not rolled back by raising inside the tx).
        """
        now = self.clock.time()
        if job.status in TERMINAL_JOB:
            raise StoreError(
                "terminal_job",
                f"cannot {op} from terminal status {job.status.value}",
            )
        if job.status == JobStatus.NEEDS_REVIEW:
            raise StoreError(
                "needs_review",
                f"cannot {op} job already needs_review",
            )
        if owner is not None:
            if not job.lease_owner or job.lease_owner != owner:
                raise StoreError("lease_mismatch", f"stale worker cannot {op}")
            if job.lease_until is not None and job.lease_until <= now:
                if job.status == JobStatus.RUNNING:
                    self._transition(
                        conn,
                        job.task_id,
                        JobStatus.NEEDS_REVIEW,
                        clear_lease=True,
                        error_code="lease_expired_on_commit",
                        event_type="job.needs_review",
                        dedupe_key=f"needs_review:commit:{op}:{int(now)}",
                        extra_payload={
                            "reason": "lease_expired_on_commit",
                            "auto_requeue": False,
                            "op": op,
                        },
                    )
                return job, StoreError(
                    "lease_lost",
                    f"lease expired; cannot {op} (no auto-requeue)",
                )
        return job, None

    def mark_running(self, task_id: str, *, owner: str) -> JobRecord:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
            ).fetchone()
            if not row:
                raise StoreError("job_not_found", f"unknown task_id {task_id!r}")
            job = self._row_job(row)
            if job.status != JobStatus.CLAIMED or job.lease_owner != owner:
                raise StoreError("lease_mismatch", "running requires matching claim owner")
            return self._transition(
                conn,
                task_id,
                JobStatus.RUNNING,
                event_type="job.running",
                dedupe_key=f"running:{job.attempt}",
            )

    def mark_succeeded(self, task_id: str, *, owner: str | None = None) -> JobRecord:
        post_error: StoreError | None = None
        result: JobRecord | None = None
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
            ).fetchone()
            if not row:
                raise StoreError("job_not_found", f"unknown task_id {task_id!r}")
            job, post_error = self._guard_live_owner(
                conn, self._row_job(row), owner, op="succeed"
            )
            if post_error is None:
                result = self._transition(
                    conn,
                    task_id,
                    JobStatus.SUCCEEDED,
                    clear_lease=True,
                    event_type="job.succeeded",
                    dedupe_key="succeeded",
                )
        if post_error is not None:
            raise post_error
        assert result is not None
        return result

    def mark_needs_review(
        self, task_id: str, *, error_code: str, owner: str | None = None
    ) -> JobRecord:
        """Uncertain outcome after execution may have started — never auto-requeues."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
            ).fetchone()
            if not row:
                raise StoreError("job_not_found", f"unknown task_id {task_id!r}")
            job = self._row_job(row)
            # Idempotent if already needs_review with same owner context
            if job.status == JobStatus.NEEDS_REVIEW:
                return job
            if job.status in TERMINAL_JOB:
                raise StoreError(
                    "terminal_job",
                    f"cannot needs_review from terminal {job.status.value}",
                )
            if owner is not None and job.lease_owner and job.lease_owner != owner:
                raise StoreError("lease_mismatch", "stale worker cannot commit needs_review")
            # Lease-expired owner may still mark needs_review for uncertainty (owned path)
            # but not if lease already cleared by another actor.
            return self._transition(
                conn,
                task_id,
                JobStatus.NEEDS_REVIEW,
                error_code=error_code,
                clear_lease=True,
                event_type="job.needs_review",
                dedupe_key=f"needs_review:{error_code}:{job.attempt}",
                extra_payload={"auto_requeue": False, "reason": error_code},
            )

    def explicit_retry(self, task_id: str) -> JobRecord:
        """Human/operator retry from FAILED or NEEDS_REVIEW only."""
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
            ).fetchone()
            if not row:
                raise StoreError("job_not_found", f"unknown task_id {task_id!r}")
            job = self._row_job(row)
            if job.status not in {JobStatus.FAILED, JobStatus.NEEDS_REVIEW}:
                raise StoreError(
                    "retry_not_allowed",
                    f"explicit retry only from failed/needs_review, not {job.status.value}",
                )
            if job.attempt >= job.max_attempts:
                return self._transition(
                    conn,
                    task_id,
                    JobStatus.DEAD_LETTERED,
                    clear_lease=True,
                    event_type="job.dead_lettered",
                    dedupe_key="dead_lettered:max_attempts",
                )
            return self._transition(
                conn,
                task_id,
                JobStatus.QUEUED,
                clear_lease=True,
                event_type="job.requeued",
                dedupe_key=f"explicit_requeued:{job.attempt}",
                extra_payload={"explicit_retry": True},
            )

    def mark_failed(
        self, task_id: str, *, error_code: str, retry: bool = False, owner: str | None = None
    ) -> JobRecord:
        post_error: StoreError | None = None
        result: JobRecord | None = None
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
            ).fetchone()
            if not row:
                raise StoreError("job_not_found", f"unknown task_id {task_id!r}")
            job, post_error = self._guard_live_owner(
                conn, self._row_job(row), owner, op="fail"
            )
            if post_error is not None:
                pass
            # From running/claimed → failed first
            elif job.status in {JobStatus.RUNNING, JobStatus.CLAIMED}:
                job = self._transition(
                    conn,
                    task_id,
                    JobStatus.FAILED,
                    error_code=error_code,
                    clear_lease=True,
                    bump_attempt=True,
                    event_type="job.failed",
                    dedupe_key=f"failed:{job.attempt + 1}:{error_code}",
                )
            elif job.status != JobStatus.FAILED:
                raise StoreError(
                    "illegal_transition",
                    f"cannot fail from {job.status.value}",
                )
            # Optional immediate requeue only when caller opts in (definitive pre-exec failures).
            # Uncertain execution paths must use mark_needs_review instead.
            if post_error is not None:
                result = None
            elif retry and job.attempt < job.max_attempts:
                result = self._transition(
                    conn,
                    task_id,
                    JobStatus.QUEUED,
                    clear_lease=True,
                    event_type="job.requeued",
                    dedupe_key=f"requeued:{job.attempt}",
                    extra_payload={"explicit_or_opt_in_retry": True},
                )
            elif retry and job.attempt >= job.max_attempts:
                result = self._transition(
                    conn,
                    task_id,
                    JobStatus.DEAD_LETTERED,
                    error_code=error_code,
                    clear_lease=True,
                    event_type="job.dead_lettered",
                    dedupe_key="dead_lettered",
                )
            else:
                result = job
        if post_error is not None:
            raise post_error
        assert result is not None
        return result

    def cancel(self, task_id: str) -> JobRecord:
        """Mark cancelled. STATE-ONLY — does NOT kill any process or process tree."""
        with self._tx() as conn:
            return self._transition(
                conn,
                task_id,
                JobStatus.CANCELLED,
                clear_lease=True,
                event_type="job.cancelled",
                dedupe_key="cancelled",
                extra_payload={"note": "state_only_no_process_kill"},
            )

    def list_outbox(
        self, *, delivery: DeliveryState | None = None
    ) -> list[OutboxEvent]:
        with self._conn() as conn:
            if delivery is None:
                rows = conn.execute(
                    "SELECT * FROM outbox ORDER BY created_at ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM outbox WHERE delivery = ? ORDER BY created_at ASC",
                    (delivery.value,),
                ).fetchall()
            return [self._row_event(r) for r in rows]

    def due_outbox(self, *, limit: int = 50) -> list[OutboxEvent]:
        now = self.clock.time()
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM outbox
                WHERE delivery IN (?, ?)
                  AND next_attempt_at <= ?
                ORDER BY next_attempt_at ASC
                LIMIT ?
                """,
                (
                    DeliveryState.PENDING.value,
                    DeliveryState.FAILED.value,
                    now,
                    limit,
                ),
            ).fetchall()
            return [self._row_event(r) for r in rows]

    def ack_http_accepted(self, event_id: str) -> OutboxEvent:
        """Transport-level ack only — not bot processed / not exactly-once."""
        return self._set_delivery(event_id, DeliveryState.HTTP_ACCEPTED)

    def ack_bot_processed(self, event_id: str) -> OutboxEvent:
        """Consumer processed ack. Still not a global exactly-once guarantee."""
        return self._set_delivery(event_id, DeliveryState.BOT_PROCESSED)

    def _set_delivery(self, event_id: str, state: DeliveryState) -> OutboxEvent:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            if not row:
                raise StoreError("event_not_found", f"unknown event_id {event_id!r}")
            ev = self._row_event(row)
            if ev.delivery == DeliveryState.BOT_PROCESSED:
                return ev  # terminal for delivery
            if (
                state == DeliveryState.BOT_PROCESSED
                and ev.delivery not in {DeliveryState.HTTP_ACCEPTED, DeliveryState.PENDING, DeliveryState.FAILED}
            ):
                # allow pending→processed in tests/sim; prefer http then processed
                pass
            conn.execute(
                "UPDATE outbox SET delivery=?, last_error=NULL WHERE event_id=?",
                (state.value, event_id),
            )
            row2 = conn.execute(
                "SELECT * FROM outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            return self._row_event(row2)

    def mark_outbox_dead_letter(self, event_id: str, *, error: str) -> OutboxEvent:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            if not row:
                raise StoreError("event_not_found", f"unknown event_id {event_id!r}")
            ev = self._row_event(row)
            if ev.delivery == DeliveryState.BOT_PROCESSED:
                raise StoreError("already_processed", "cannot dead-letter processed event")
            conn.execute(
                """
                UPDATE outbox SET delivery=?, attempt=?, last_error=?
                WHERE event_id=?
                """,
                (
                    DeliveryState.DEAD_LETTERED.value,
                    max(ev.attempt + 1, ev.max_attempts),
                    (error or "dead_letter")[:200],
                    event_id,
                ),
            )
            row2 = conn.execute(
                "SELECT * FROM outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            return self._row_event(row2)

    def fail_delivery(self, event_id: str, *, error: str) -> OutboxEvent:
        with self._tx() as conn:
            row = conn.execute(
                "SELECT * FROM outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            if not row:
                raise StoreError("event_not_found", f"unknown event_id {event_id!r}")
            ev = self._row_event(row)
            if ev.delivery == DeliveryState.BOT_PROCESSED:
                raise StoreError("already_processed", "cannot fail processed event")
            attempt = ev.attempt + 1
            if attempt >= ev.max_attempts:
                conn.execute(
                    """
                    UPDATE outbox SET delivery=?, attempt=?, last_error=?
                    WHERE event_id=?
                    """,
                    (DeliveryState.DEAD_LETTERED.value, attempt, error[:200], event_id),
                )
            else:
                delay = self._retry_delay(attempt)
                next_at = self.clock.time() + delay
                conn.execute(
                    """
                    UPDATE outbox SET delivery=?, attempt=?, next_attempt_at=?, last_error=?
                    WHERE event_id=?
                    """,
                    (
                        DeliveryState.FAILED.value,
                        attempt,
                        next_at,
                        error[:200],
                        event_id,
                    ),
                )
            row2 = conn.execute(
                "SELECT * FROM outbox WHERE event_id = ?", (event_id,)
            ).fetchone()
            return self._row_event(row2)

    def simulate_outbox_tick(
        self,
        *,
        transport_ok: bool = True,
        bot_process: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """One-shot simulated delivery pass — NOT a background service / NOT real HTTP.

        Labels results truthfully as simulated. Distinguishes http_accepted vs bot_processed.
        """
        results: list[dict[str, Any]] = []
        for ev in self.due_outbox(limit=limit):
            if not transport_ok:
                updated = self.fail_delivery(ev.event_id, error="simulated_transport_error")
                results.append(
                    {
                        "event_id": updated.event_id,
                        "simulated": True,
                        "delivery": updated.delivery.value,
                        "attempt": updated.attempt,
                    }
                )
                continue
            accepted = self.ack_http_accepted(ev.event_id)
            item: dict[str, Any] = {
                "event_id": accepted.event_id,
                "simulated": True,
                "delivery": accepted.delivery.value,
                "note": "http_accepted_is_not_bot_processed",
            }
            if bot_process:
                processed = self.ack_bot_processed(ev.event_id)
                item["delivery"] = processed.delivery.value
                item["note"] = "bot_processed_ack_simulated_not_exactly_once"
            results.append(item)
        return results
