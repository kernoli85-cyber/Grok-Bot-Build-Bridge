"""One-shot worker: dry-run default; heartbeat; no daemon; no auto-requeue on uncertainty."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from ..safe_exec import RunResult, run_argv
from ..state.store import JobStore, StoreError
from ..state.types import JobStatus
from .plan import ApprovedExecutionPlan, redact_argv_preview

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
    heartbeats: int = 0
    lease_lost: bool = False
    plan_backend: str | None = None
    execution_transport: str | None = None
    execution_kind: str | None = None
    result_summary: dict | None = None


def _heartbeat_loop(
    store: JobStore,
    task_id: str,
    owner: str,
    lease_sec: float,
    interval: float,
    stop: threading.Event,
    lost: threading.Event,
    counter: list[int],
) -> None:
    """Renew lease until stop. On failure set lost; never resurrect terminal jobs."""
    # Initial wait so we don't race mark_running
    while not stop.wait(interval):
        try:
            store.renew_lease(task_id, owner=owner, lease_sec=lease_sec)
            counter[0] += 1
        except StoreError:
            lost.set()
            return


def _execute_one_shot_inner(
    store: JobStore,
    plan: ApprovedExecutionPlan,
    *,
    owner: str,
    dry_run: bool = True,
    executor: Executor = run_argv,
    lease_sec: float = 300.0,
    heartbeat_interval_sec: float | None = None,
) -> RunOutcome:
    """Claim → running → execute once (with lease heartbeat) → commit.

    dry_run=True: validate/claim path optional preview without process spawn.
    Uncertain outcomes (timeout, lease loss) => needs_review, never auto-requeue.
    Notification/outbox retries remain separate from job reexecution.
    """
    if not owner.strip():
        raise StoreError("owner_empty", "owner required")

    argv_preview = redact_argv_preview(plan.argv)
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

    job = store.get_job(plan.task_id)
    if job is None:
        store.enqueue_job(
            task_id=plan.task_id,
            backend=plan.backend,
            cwd=plan.cwd,
        )
    store.claim_job(plan.task_id, owner=owner, lease_sec=lease_sec)
    store.mark_running(plan.task_id, owner=owner)

    interval = heartbeat_interval_sec
    if interval is None:
        interval = max(0.05, min(lease_sec / 3.0, 15.0))
    stop = threading.Event()
    lost = threading.Event()
    hb_count = [0]
    hb_thread = threading.Thread(
        target=_heartbeat_loop,
        args=(store, plan.task_id, owner, lease_sec, interval, stop, lost, hb_count),
        name=f"lease-hb-{plan.task_id}",
        daemon=True,
    )
    hb_thread.start()

    try:
        result = executor(
            plan.argv,
            timeout_sec=plan.timeout_sec,
            cwd=None,
            env=plan.env,
            stdin_bytes=plan.stdin_bytes,
        )
    finally:
        stop.set()
        hb_thread.join(timeout=max(1.0, interval + 1.0))

    summary = None
    if plan.parse_grok_json and not (result.timed_out or result.error):
        from .grok_result import classify, parse_grok_json_result

        s = parse_grok_json_result(
            result.stdout, truncated=result.stdout_truncated, expected_text=plan.expected_text
        )
        summary = s.to_dict()
        try:
            store.put_job_result(plan.task_id, summary)
        except StoreError:
            summary = {"parse_status": "store_failed", "model_ok": False}
        if result.returncode == 0:
            code = classify(s, require_expected=plan.expected_text is not None)
            if code is not None:
                try:
                    store.mark_needs_review(plan.task_id, error_code=code, owner=owner)
                except StoreError:
                    pass
                cur = store.get_job(plan.task_id)
                return RunOutcome(
                    dry_run=False, task_id=plan.task_id,
                    status=cur.status.value if cur else JobStatus.NEEDS_REVIEW.value,
                    returncode=0, timed_out=False, error_code=code,
                    stdout_truncated=result.stdout_truncated,
                    stderr_truncated=result.stderr_truncated,
                    note="exit0_but_result_not_accepted_needs_review_no_auto_requeue",
                    argv_preview=argv_preview, heartbeats=hb_count[0],
                    result_summary=summary,
                )
    from dataclasses import replace as _replace
    return _replace(_commit_result(
        store,
        plan,
        owner=owner,
        result=result,
        argv_preview=argv_preview,
        heartbeats=hb_count[0],
        lease_lost_flag=lost.is_set(),
    ), result_summary=summary)


def _commit_result(
    store: JobStore,
    plan: ApprovedExecutionPlan,
    *,
    owner: str,
    result: RunResult,
    argv_preview: tuple[str, ...],
    heartbeats: int,
    lease_lost_flag: bool,
) -> RunOutcome:
    """Commit terminal outcome only if ownership still live; never overwrite cancel."""
    current = store.get_job(plan.task_id)
    if current is not None and current.status == JobStatus.CANCELLED:
        return RunOutcome(
            dry_run=False,
            task_id=plan.task_id,
            status=JobStatus.CANCELLED.value,
            returncode=result.returncode,
            timed_out=result.timed_out,
            error_code="cancelled",
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            note="cancelled_state_only_no_process_kill; worker did not overwrite",
            argv_preview=argv_preview,
            heartbeats=heartbeats,
            lease_lost=True,
        )
    if current is not None and current.status == JobStatus.NEEDS_REVIEW:
        return RunOutcome(
            dry_run=False,
            task_id=plan.task_id,
            status=JobStatus.NEEDS_REVIEW.value,
            returncode=result.returncode,
            timed_out=result.timed_out,
            error_code=current.last_error_code or "needs_review",
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            note="already_needs_review_no_auto_requeue; stale commit skipped",
            argv_preview=argv_preview,
            heartbeats=heartbeats,
            lease_lost=True,
        )

    if lease_lost_flag:
        try:
            store.mark_needs_review(
                plan.task_id, error_code="lease_lost_during_run", owner=owner
            )
        except StoreError as exc:
            if exc.code not in {"lease_mismatch", "lease_lost", "needs_review", "terminal_job"}:
                raise
        return RunOutcome(
            dry_run=False,
            task_id=plan.task_id,
            status=JobStatus.NEEDS_REVIEW.value,
            returncode=result.returncode,
            timed_out=result.timed_out,
            error_code="lease_lost_during_run",
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            note="lease_lost_during_run_needs_review_no_auto_requeue",
            argv_preview=argv_preview,
            heartbeats=heartbeats,
            lease_lost=True,
        )

    def _safe_mark_review(code: str) -> None:
        try:
            store.mark_needs_review(plan.task_id, error_code=code, owner=owner)
        except StoreError as exc:
            if exc.code not in {"lease_mismatch", "lease_lost", "needs_review", "terminal_job"}:
                raise

    def _safe_mark_failed(code: str) -> None:
        try:
            store.mark_failed(plan.task_id, error_code=code, retry=False, owner=owner)
        except StoreError as exc:
            if exc.code in {"lease_mismatch", "lease_lost", "needs_review", "terminal_job"}:
                return
            raise

    def _safe_mark_ok() -> bool:
        try:
            store.mark_succeeded(plan.task_id, owner=owner)
            return True
        except StoreError as exc:
            if exc.code in {"lease_mismatch", "lease_lost", "needs_review", "terminal_job"}:
                return False
            raise

    if result.timed_out or result.error == "timeout":
        _safe_mark_review("timeout_uncertain")
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
            heartbeats=heartbeats,
        )

    if result.error == "executable_not_found":
        _safe_mark_failed("executable_not_found")
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
            heartbeats=heartbeats,
        )

    if result.returncode == 0:
        if _safe_mark_ok():
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
                heartbeats=heartbeats,
            )
        return RunOutcome(
            dry_run=False,
            task_id=plan.task_id,
            status=JobStatus.NEEDS_REVIEW.value,
            returncode=0,
            timed_out=False,
            error_code="lease_lost_after_success_exit",
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            note="exit0_but_lease_lost_needs_review_no_auto_requeue",
            argv_preview=argv_preview,
            heartbeats=heartbeats,
            lease_lost=True,
        )

    if result.returncode is not None and result.returncode < 0:
        code = f"signal_or_abort_{result.returncode}"
        _safe_mark_review(code)
        return RunOutcome(
            dry_run=False,
            task_id=plan.task_id,
            status=JobStatus.NEEDS_REVIEW.value,
            returncode=result.returncode,
            timed_out=False,
            error_code=code,
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            note="negative_exit_uncertain_needs_review_no_auto_requeue",
            argv_preview=argv_preview,
            heartbeats=heartbeats,
        )

    code = f"exit_{result.returncode}"
    _safe_mark_failed(code)
    return RunOutcome(
        dry_run=False,
        task_id=plan.task_id,
        status=JobStatus.FAILED.value,
        returncode=result.returncode,
        timed_out=False,
        error_code=code,
        stdout_truncated=result.stdout_truncated,
        stderr_truncated=result.stderr_truncated,
        note="nonzero_exit_no_auto_requeue; use explicit_retry",
        argv_preview=argv_preview,
        heartbeats=heartbeats,
    )


def execute_one_shot(
    store: JobStore,
    plan: ApprovedExecutionPlan,
    *,
    owner: str,
    dry_run: bool = True,
    executor: Executor = run_argv,
    lease_sec: float = 300.0,
    heartbeat_interval_sec: float | None = None,
) -> RunOutcome:
    """See _execute_one_shot_inner; adds truthful backend/transport labels."""
    from dataclasses import replace

    out = _execute_one_shot_inner(
        store,
        plan,
        owner=owner,
        dry_run=dry_run,
        executor=executor,
        lease_sec=lease_sec,
        heartbeat_interval_sec=heartbeat_interval_sec,
    )
    return replace(
        out,
        plan_backend=plan.backend,
        execution_transport=plan.execution_transport,
        execution_kind=plan.execution_kind,
    )
