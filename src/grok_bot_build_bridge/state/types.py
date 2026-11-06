from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


EVENT_SCHEMA_VERSION = 1


class JobStatus(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # definitive failure before/without uncertain execution
    NEEDS_REVIEW = "needs_review"  # started/uncertain — NO auto-requeue
    CANCELLED = "cancelled"
    DEAD_LETTERED = "dead_lettered"


TERMINAL_JOB = frozenset(
    {
        JobStatus.SUCCEEDED,
        JobStatus.CANCELLED,
        JobStatus.DEAD_LETTERED,
    }
)

# Not terminal, but must not be auto-reclaimed into QUEUED after side effects may exist
UNCERTAIN_JOB = frozenset({JobStatus.NEEDS_REVIEW})


class DeliveryState(str, Enum):
    PENDING = "pending"
    HTTP_ACCEPTED = "http_accepted"  # transport ack only — NOT bot-processed
    BOT_PROCESSED = "bot_processed"
    FAILED = "failed"
    DEAD_LETTERED = "dead_lettered"


ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset(
        {JobStatus.CLAIMED, JobStatus.CANCELLED, JobStatus.DEAD_LETTERED}
    ),
    JobStatus.CLAIMED: frozenset(
        {
            JobStatus.RUNNING,
            JobStatus.QUEUED,  # lease expired BEFORE execution started — safe
            JobStatus.CANCELLED,
            JobStatus.FAILED,
            JobStatus.DEAD_LETTERED,
        }
    ),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,  # definitive known failure (e.g. exit nonzero before timeout)
            JobStatus.NEEDS_REVIEW,  # timeout / lease while running / uncertain
            JobStatus.CANCELLED,  # state-only cancel request
            JobStatus.DEAD_LETTERED,
        }
    ),
    JobStatus.FAILED: frozenset(
        {
            JobStatus.QUEUED,  # explicit retry only
            JobStatus.DEAD_LETTERED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.NEEDS_REVIEW: frozenset(
        {
            JobStatus.QUEUED,  # explicit retry only — never automatic
            JobStatus.DEAD_LETTERED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
    JobStatus.DEAD_LETTERED: frozenset(),
}


@dataclass(frozen=True)
class JobRecord:
    task_id: str
    correlation_id: str
    status: JobStatus
    backend: str | None
    cwd: str | None
    attempt: int
    max_attempts: int
    created_at: float
    updated_at: float
    lease_owner: str | None = None
    lease_until: float | None = None
    last_error_code: str | None = None


@dataclass(frozen=True)
class OutboxEvent:
    event_id: str
    correlation_id: str
    task_id: str
    event_type: str
    schema_version: int
    delivery: DeliveryState
    attempt: int
    max_attempts: int
    created_at: float
    next_attempt_at: float
    payload: dict[str, Any]
    last_error: str | None = None
    dedupe_key: str | None = None
