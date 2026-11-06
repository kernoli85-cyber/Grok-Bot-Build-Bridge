from __future__ import annotations

import sys
from pathlib import Path

import pytest

from grok_bot_build_bridge.runner.plan import PlanError, build_approved_plan, build_native_invocation
from grok_bot_build_bridge.runner.worker import execute_one_shot
from grok_bot_build_bridge.safe_exec import run_argv
from grok_bot_build_bridge.state.clock import Clock, Jitter
from grok_bot_build_bridge.state.store import JobStore, StoreError
from grok_bot_build_bridge.state.types import JobStatus

FIX = Path(__file__).parent / "fixtures"
DUMMY = FIX / "bridge-dummy"


def store(tmp_path: Path) -> JobStore:
    s = JobStore(tmp_path / "s.sqlite", clock=Clock(), jitter=Jitter(1))
    s.clock.set(1000.0)
    return s


def test_forbidden_always_approve():
    with pytest.raises(PlanError) as ei:
        build_approved_plan(
            task_id="t",
            backend="wsl",
            cwd="/tmp",
            executable=str(DUMMY),
            extra_args=["--always-approve"],
        )
    assert ei.value.code == "forbidden_flag"


def test_forbidden_permission_mode():
    with pytest.raises(PlanError) as ei:
        build_approved_plan(
            task_id="t",
            backend="wsl",
            cwd="/tmp",
            executable=str(DUMMY),
            permission_mode="bypassPermissions",
        )
    assert ei.value.code == "permission_mode_forbidden"


def test_missing_executable_fail_closed():
    with pytest.raises(PlanError) as ei:
        build_approved_plan(
            task_id="t",
            backend="wsl",
            cwd="/tmp",
            executable="/no/such/grok",
        )
    assert ei.value.code == "executable_not_found"


def test_dry_run_default_no_spawn(tmp_path: Path):
    s = store(tmp_path)
    plan = build_approved_plan(
        task_id="t1",
        backend="wsl",
        cwd=str(tmp_path),
        executable=str(DUMMY),
        prompt="hello",
    )
    out = execute_one_shot(s, plan, owner="w1", dry_run=True)
    assert out.dry_run is True
    assert s.get_job("t1") is None


def test_execute_success_dummy(tmp_path: Path):
    s = store(tmp_path)
    plan = build_approved_plan(
        task_id="t1",
        backend="wsl",
        cwd=str(tmp_path),
        executable=str(DUMMY),
        prompt="secret-prompt-should-not-be-logged",
        prompt_via_stdin=True,
        timeout_sec=10,
    )
    out = execute_one_shot(s, plan, owner="w1", dry_run=False)
    assert out.status == JobStatus.SUCCEEDED.value
    assert out.returncode == 0
    job = s.get_job("t1")
    assert job is not None and job.status == JobStatus.SUCCEEDED
    # outcome must not echo prompt
    assert "secret-prompt" not in out.note


def test_execute_nonzero_no_auto_requeue(tmp_path: Path):
    s = store(tmp_path)
    plan = build_approved_plan(
        task_id="t1",
        backend="wsl",
        cwd=str(tmp_path),
        executable=str(DUMMY),
        prompt="",
        prompt_via_stdin=True,
        extra_args=["--fail"],
    )
    out = execute_one_shot(s, plan, owner="w1", dry_run=False)
    assert out.status == JobStatus.FAILED.value
    job = s.get_job("t1")
    assert job.status == JobStatus.FAILED
    # explicit retry moves to queued
    job2 = s.explicit_retry("t1")
    assert job2.status == JobStatus.QUEUED


def test_timeout_needs_review(tmp_path: Path):
    s = store(tmp_path)
    plan = build_approved_plan(
        task_id="t1",
        backend="wsl",
        cwd=str(tmp_path),
        executable=str(DUMMY),
        prompt="",
        extra_args=["--sleep", "2"],
        timeout_sec=0.3,
    )
    out = execute_one_shot(s, plan, owner="w1", dry_run=False)
    assert out.timed_out is True
    assert out.status == JobStatus.NEEDS_REVIEW.value
    job = s.get_job("t1")
    assert job.status == JobStatus.NEEDS_REVIEW
    # cannot claim without explicit retry
    with pytest.raises(StoreError):
        s.claim_job("t1", owner="w2")


def test_lease_expired_while_running_needs_review(tmp_path: Path):
    s = store(tmp_path)
    s.enqueue_job(task_id="t1")
    s.claim_job("t1", owner="w1", lease_sec=5)
    s.mark_running("t1", owner="w1")
    s.clock.advance(6)
    with pytest.raises(StoreError) as ei:
        s.claim_job("t1", owner="w2")
    assert ei.value.code == "needs_review"
    assert s.get_job("t1").status == JobStatus.NEEDS_REVIEW


def test_stale_owner_cannot_succeed(tmp_path: Path):
    s = store(tmp_path)
    s.enqueue_job(task_id="t1")
    s.claim_job("t1", owner="w1", lease_sec=30)
    s.mark_running("t1", owner="w1")
    with pytest.raises(StoreError) as ei:
        s.mark_succeeded("t1", owner="other")
    assert ei.value.code == "lease_mismatch"


def test_bounded_stdout_while_reading():
    r = run_argv(
        [sys.executable, str(DUMMY), "--huge"],
        timeout_sec=10,
        max_stdout_bytes=1000,
    )
    assert r.returncode == 0
    assert r.stdout_truncated is True
    assert len(r.stdout.encode()) <= 1000 + 16  # decode expansion margin


def test_native_ps1_invocation_shape():
    # Don't require powershell present — only argv shape
    ps1 = tmp = FIX / "bridge-dummy.ps1"
    # create empty ps1 for path validation
    ps1.write_text("# dummy\n", encoding="utf-8")
    argv = build_native_invocation(str(ps1), ["--cwd", "C:\\\\x"])
    assert argv[0] == "powershell.exe"
    assert "-File" in argv
    assert str(ps1) in argv
    assert "--always-approve" not in argv
