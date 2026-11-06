"""One-shot worker: dry-run default; no daemon; no auto-requeue on uncertainty."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..safe_exec import RunResult, run_argv
from ..state.store import JobStore, StoreError
from ..state.types import JobStatus
from .plan import ApprovedExecutionPlan

Executor = Callable[..., RunResult]


@dataclass(frozen=True)
class RunOutcome:
    dry_run: bool
    task_id: str
    status: str
    returncode: int | None
    timed_out: bool
    error_code: str | None
    stdout_truncated: bool
    stderr_truncated: bool
    note: str
    argv_preview: tuple[str, ...]


def execute_one_shot(
    store: JobStore,
    plan: ApprovedExecutionPlan,
    *,
    owner: str,
    dry_run: bool = True,
    executor: Executor = run_argv,
    lease_sec: float = 300.0,
) -> RunOutcome:
    """Claim → running → execute once → commit.

    dry_run=True: validate/claim path optional preview without process spawn.
    Uncertain outcomes (timeout) => needs_review, never auto-requeue.
    Notification/outbox retries remain separate from job reexecution.
    """
    if not owner.strip():
        raise StoreError("owner_empty", "owner required")

    argv_preview = plan.argv
    if dry_run:
        return RunOutcome(
            dry_run=True,
            task_id=plan.task_id,
            status="dry_run",
            returncode=None,
            timed_out=False,
            error_code=None,
            stdout_truncated=False,
            stderr_truncated=False,
            note="dry_run_no_process; pass execute=True/--execute to run",
            argv_preview=argv_preview,
        )

    # Ensure job exists
    job = store.get_job(plan.task_id)
    if job is None:
        store.enqueue_job(
            task_id=plan.task_id,
            backend=plan.backend,
            cwd=plan.cwd,
        )
    store.claim_job(plan.task_id, owner=owner, lease_sec=lease_sec)
    store.mark_running(plan.task_id, owner=owner)

    result = executor(
        plan.argv,
        timeout_sec=plan.timeout_sec,
        cwd=None,  # cwd already encoded in plan tokens when applicable; avoid double-chdir surprises
        env=plan.env,
        stdin_bytes=plan.stdin_bytes,
    )

    # Sanitize: do not return/store full stdout/stderr in outcome note
    if result.timed_out or result.error == "timeout":
        store.mark_needs_review(
            plan.task_id, error_code="timeout_uncertain", owner=owner
        )
        return RunOutcome(
            dry_run=False,
            task_id=plan.task_id,
            status=JobStatus.NEEDS_REVIEW.value,
            returncode=result.returncode,
            timed_out=True,
            error_code="timeout_uncertain",
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            note="timeout_needs_review_no_auto_requeue; process kill attempted but not proven",
            argv_preview=argv_preview,
        )

    if result.error == "executable_not_found":
        # Fail closed before meaningful side effects beyond spawn attempt
        store.mark_failed(
            plan.task_id, error_code="executable_not_found", retry=False, owner=owner
        )
        return RunOutcome(
            dry_run=False,
            task_id=plan.task_id,
            status=JobStatus.FAILED.value,
            returncode=result.returncode,
            timed_out=False,
            error_code="executable_not_found",
            stdout_truncated=False,
            stderr_truncated=False,
            note="fail_closed_missing_executable",
            argv_preview=argv_preview,
        )

    if result.returncode == 0:
        store.mark_succeeded(plan.task_id, owner=owner)
        return RunOutcome(
            dry_run=False,
            task_id=plan.task_id,
            status=JobStatus.SUCCEEDED.value,
            returncode=0,
            timed_out=False,
            error_code=None,
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            note="succeeded; transcripts not stored by default",
            argv_preview=argv_preview,
        )

    # Negative exit without timeout often means OS-level abort/start failure —
    # treat as uncertain (needs_review), not a clean failed attempt.
    if result.returncode is not None and result.returncode < 0:
        store.mark_needs_review(
            plan.task_id, error_code=f"signal_or_abort_{result.returncode}", owner=owner
        )
        return RunOutcome(
            dry_run=False,
            task_id=plan.task_id,
            status=JobStatus.NEEDS_REVIEW.value,
            returncode=result.returncode,
            timed_out=False,
            error_code=f"signal_or_abort_{result.returncode}",
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            note="negative_exit_uncertain_needs_review_no_auto_requeue",
            argv_preview=argv_preview,
        )

    # Positive nonzero: definitive failure of this attempt; no auto-requeue.
    store.mark_failed(
        plan.task_id,
        error_code=f"exit_{result.returncode}",
        retry=False,
        owner=owner,
    )
    return RunOutcome(
        dry_run=False,
        task_id=plan.task_id,
        status=JobStatus.FAILED.value,
        returncode=result.returncode,
        timed_out=False,
        error_code=f"exit_{result.returncode}",
        stdout_truncated=result.stdout_truncated,
        stderr_truncated=result.stderr_truncated,
        note="nonzero_exit_no_auto_requeue; use explicit_retry",
        argv_preview=argv_preview,
    )
