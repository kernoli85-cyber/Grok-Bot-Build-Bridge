from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from grok_bot_build_bridge.runner.plan import build_approved_plan, redact_argv_preview
from grok_bot_build_bridge.runner.worker import execute_one_shot
from grok_bot_build_bridge.safe_exec import RunResult
from grok_bot_build_bridge.state.clock import Clock, Jitter
from grok_bot_build_bridge.state.store import JobStore, StoreError
from grok_bot_build_bridge.state.types import JobStatus

FIX = Path(__file__).parent / "fixtures"
DUMMY = FIX / "bridge-dummy.py"


def store(tmp_path: Path) -> JobStore:
    s = JobStore(tmp_path / "s.sqlite", clock=Clock(), jitter=Jitter(1))
    s.clock.set(1000.0)
    return s


def test_renew_lease_extends(tmp_path: Path):
    s = store(tmp_path)
    s.enqueue_job(task_id="t1")
    s.claim_job("t1", owner="w1", lease_sec=10)
    s.mark_running("t1", owner="w1")
    before = s.get_job("t1").lease_until
    s.clock.advance(4)
    job = s.renew_lease("t1", owner="w1", lease_sec=10)
    assert job.lease_until == pytest.approx(1000.0 + 4 + 10)
    assert job.lease_until > before


def test_renew_rejects_wrong_owner(tmp_path: Path):
    s = store(tmp_path)
    s.enqueue_job(task_id="t1")
    s.claim_job("t1", owner="w1", lease_sec=30)
    s.mark_running("t1", owner="w1")
    with pytest.raises(StoreError) as ei:
        s.renew_lease("t1", owner="evil", lease_sec=30)
    assert ei.value.code == "lease_mismatch"


def test_renew_does_not_resurrect_cancelled(tmp_path: Path):
    s = store(tmp_path)
    s.enqueue_job(task_id="t1")
    s.claim_job("t1", owner="w1", lease_sec=30)
    s.mark_running("t1", owner="w1")
    s.cancel("t1")
    with pytest.raises(StoreError) as ei:
        s.renew_lease("t1", owner="w1", lease_sec=30)
    assert ei.value.code == "terminal_job"
    assert s.get_job("t1").status == JobStatus.CANCELLED


def test_renew_does_not_resurrect_needs_review(tmp_path: Path):
    s = store(tmp_path)
    s.enqueue_job(task_id="t1")
    s.claim_job("t1", owner="w1", lease_sec=30)
    s.mark_running("t1", owner="w1")
    s.mark_needs_review("t1", error_code="x", owner="w1")
    with pytest.raises(StoreError) as ei:
        s.renew_lease("t1", owner="w1", lease_sec=30)
    assert ei.value.code == "needs_review"


def test_renew_expired_running_marks_needs_review(tmp_path: Path):
    s = store(tmp_path)
    s.enqueue_job(task_id="t1")
    s.claim_job("t1", owner="w1", lease_sec=5)
    s.mark_running("t1", owner="w1")
    s.clock.advance(6)
    with pytest.raises(StoreError) as ei:
        s.renew_lease("t1", owner="w1", lease_sec=5)
    assert ei.value.code == "lease_lost"
    assert s.get_job("t1").status == JobStatus.NEEDS_REVIEW


def test_expired_lease_cannot_succeed(tmp_path: Path):
    s = store(tmp_path)
    s.enqueue_job(task_id="t1")
    s.claim_job("t1", owner="w1", lease_sec=5)
    s.mark_running("t1", owner="w1")
    s.clock.advance(6)
    with pytest.raises(StoreError) as ei:
        s.mark_succeeded("t1", owner="w1")
    assert ei.value.code == "lease_lost"
    assert s.get_job("t1").status == JobStatus.NEEDS_REVIEW


def test_worker_heartbeat_keeps_lease_alive(tmp_path: Path):
    s = store(tmp_path)

    def slow_ok(argv, **kwargs):
        # Advance clock in steps smaller than lease; heartbeats renew ahead of expiry
        for _ in range(8):
            time.sleep(0.06)
            s.clock.advance(0.8)
        return RunResult(tuple(argv), 0, "ok", "", timed_out=False)

    plan = build_approved_plan(
        task_id="t1",
        backend="wsl",
        cwd=str(tmp_path),
        executable=str(DUMMY),
        timeout_sec=30,
    )
    out = execute_one_shot(
        s,
        plan,
        owner="w1",
        dry_run=False,
        executor=slow_ok,
        lease_sec=5.0,
        heartbeat_interval_sec=0.05,
    )
    assert out.status == JobStatus.SUCCEEDED.value
    assert out.heartbeats >= 1
    assert out.lease_lost is False


def test_worker_lease_lost_mid_run_needs_review(tmp_path: Path):
    s = store(tmp_path)
    started = threading.Event()

    def steal_lease(argv, **kwargs):
        started.wait(timeout=2)
        # Expire lease and have another claimer force needs_review
        s.clock.advance(100)
        with pytest.raises(StoreError):
            s.claim_job("t1", owner="w2", lease_sec=10)
        assert s.get_job("t1").status == JobStatus.NEEDS_REVIEW
        return RunResult(tuple(argv), 0, "ok", "", timed_out=False)

    plan = build_approved_plan(
        task_id="t1",
        backend="wsl",
        cwd=str(tmp_path),
        executable=str(DUMMY),
        timeout_sec=30,
    )

    # Use a custom executor that signals after claim/running — execute_one_shot
    # claims before executor, so set started at beginning of executor.
    def exec2(argv, **kwargs):
        started.set()
        time.sleep(0.05)
        return steal_lease(argv, **kwargs)

    out = execute_one_shot(
        s,
        plan,
        owner="w1",
        dry_run=False,
        executor=exec2,
        lease_sec=1.0,
        heartbeat_interval_sec=10.0,  # intentionally slow so steal wins
    )
    assert out.status == JobStatus.NEEDS_REVIEW.value
    assert out.lease_lost is True
    assert s.get_job("t1").status == JobStatus.NEEDS_REVIEW


def test_cancel_mid_run_not_overwritten(tmp_path: Path):
    s = store(tmp_path)

    def cancel_then_ok(argv, **kwargs):
        s.cancel("t1")
        return RunResult(tuple(argv), 0, "ok", "", timed_out=False)

    plan = build_approved_plan(
        task_id="t1",
        backend="wsl",
        cwd=str(tmp_path),
        executable=str(DUMMY),
    )
    out = execute_one_shot(
        s, plan, owner="w1", dry_run=False, executor=cancel_then_ok, lease_sec=30
    )
    assert out.status == JobStatus.CANCELLED.value
    assert s.get_job("t1").status == JobStatus.CANCELLED


def test_redact_single_prompt():
    prev = redact_argv_preview(("grok", "--single", "SUPERSECRET", "--cwd", "/tmp"))
    assert "SUPERSECRET" not in prev
    assert "<redacted_prompt>" in prev


def test_force_prompt_argv_redacted_in_outcome(tmp_path: Path):
    s = store(tmp_path)
    plan = build_approved_plan(
        task_id="t1",
        backend="wsl",
        cwd=str(tmp_path),
        executable=str(DUMMY),
        prompt="TOPSECRET_PROMPT_VALUE",
        prompt_via_stdin=False,
        force_prompt_argv=True,
    )
    out = execute_one_shot(s, plan, owner="w1", dry_run=True)
    assert "TOPSECRET_PROMPT_VALUE" not in out.argv_preview
    assert "<redacted_prompt>" in out.argv_preview
