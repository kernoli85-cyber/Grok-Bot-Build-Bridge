"""Atomic result+status+outbox commit under faults, races, cancellation, lease loss."""
import json
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from grok_bot_build_bridge.runner import build_approved_plan, execute_one_shot
from grok_bot_build_bridge.runner.grok_result import parse_grok_json_result
from grok_bot_build_bridge.safe_exec import RunResult
from grok_bot_build_bridge.state import JobStatus, JobStore
from grok_bot_build_bridge.state.clock import Clock
from grok_bot_build_bridge.state.store import StoreError

FIX = Path(__file__).parent / "fixtures"
OK = '{"text":"BRIDGE_OK","stopReason":"end_turn","sessionId":"sess-SECRET1"}'


def _plan(tid):
    exe = str(FIX / "bridge-dummy-grokjson.py")
    p = build_approved_plan(task_id=tid, backend="wsl", cwd="/tmp", executable=exe,
                            extra_args=(), prompt=None)
    return replace(p, parse_grok_json=True, expected_text="BRIDGE_OK")


def _exec(stdout=OK, rc=0, hook=None, calls=None):
    def fake(argv, **kw):
        if calls is not None:
            calls.append(1)
        if hook:
            hook()
        return RunResult(tuple(argv), rc, stdout, "")
    return fake


def _outbox_types(store, tid):
    return [e.event_type for e in store.list_outbox() if e.task_id == tid]


def _no_leak(store):
    blob = json.dumps([getattr(e, "payload_json", str(e)) for e in store.list_outbox()], default=str)
    assert "sess-SECRET1" not in blob and "BRIDGE_OK" not in blob


def test_success_commits_result_status_outbox_together(tmp_path):
    st = JobStore(tmp_path / "s.db")
    calls = []
    o = execute_one_shot(st, _plan("a"), owner="w", dry_run=False, executor=_exec(calls=calls))
    assert o.status == "succeeded" and len(calls) == 1
    st2 = JobStore(tmp_path / "s.db")  # reopen
    assert st2.get_job("a").status is JobStatus.SUCCEEDED
    assert st2.get_job_result("a")["session_id"] == "sess-SECRET1"
    assert "job.succeeded" in _outbox_types(st2, "a")
    _no_leak(st2)


@pytest.mark.parametrize("where", ["before_commit", "after_commit"])
def test_sqlite_failure_never_invents_success(tmp_path, monkeypatch, where):
    st = JobStore(tmp_path / "s.db")
    real = st.commit_job_result

    def boom(*a, **k):
        if where == "after_commit":
            real(*a, **k)
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(st, "commit_job_result", boom)
    calls = []
    o = execute_one_shot(st, _plan("b"), owner="w", dry_run=False, executor=_exec(calls=calls))
    assert len(calls) == 1  # never re-executed
    job = JobStore(tmp_path / "s.db").get_job("b")
    if where == "before_commit":
        assert o.status == "needs_review" and o.error_code == "result_persist_failed"
        assert job.status is JobStatus.NEEDS_REVIEW
        assert JobStore(tmp_path / "s.db").get_job_result("b") is None
    else:
        # commit actually happened; outcome reports exactly what DB shows
        assert job.status is JobStatus.SUCCEEDED and o.status == "succeeded"
        assert JobStore(tmp_path / "s.db").get_job_result("b") is not None


def test_db_unavailable_reports_status_unknown(tmp_path, monkeypatch):
    st = JobStore(tmp_path / "s.db")

    def dead(*a, **k):
        raise sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr(st, "commit_job_result", dead)
    monkeypatch.setattr(st, "mark_needs_review", dead)
    orig_get = st.get_job
    state = {"broken": False}
    monkeypatch.setattr(st, "get_job", lambda t: dead() if state["broken"] else orig_get(t))

    def hook():
        state["broken"] = True
    o = execute_one_shot(st, _plan("c"), owner="w", dry_run=False, executor=_exec(hook=hook))
    assert o.status == "status_unknown" and o.result_summary is None


def test_cancel_during_run_blocks_result_and_terminal(tmp_path):
    st = JobStore(tmp_path / "s.db")
    o = execute_one_shot(st, _plan("d"), owner="w", dry_run=False,
                         executor=_exec(hook=lambda: st.cancel("d")))
    assert o.status == "cancelled" and o.result_summary is None
    assert st.get_job_result("d") is None
    assert "job.succeeded" not in _outbox_types(st, "d")


def test_stale_owner_cannot_commit(tmp_path):
    st = JobStore(tmp_path / "s.db")
    s = parse_grok_json_result(OK, expected_text="BRIDGE_OK").to_dict()

    def steal():
        with pytest.raises(StoreError) as e:
            st.commit_job_result("e", owner="intruder", summary=s, terminal=JobStatus.SUCCEEDED)
        assert e.value.code == "lease_mismatch"
    o = execute_one_shot(st, _plan("e"), owner="w", dry_run=False, executor=_exec(hook=steal))
    assert o.status == "succeeded"  # only the real owner's single commit landed


def test_lease_expired_during_run_needs_review_without_result(tmp_path):
    clock = Clock()
    clock.set(1000.0)
    st = JobStore(tmp_path / "s.db", clock=clock)
    o = execute_one_shot(st, _plan("f"), owner="w", dry_run=False, lease_sec=10,
                         heartbeat_interval_sec=3600, executor=_exec(hook=lambda: clock.set(5000.0)))
    assert o.status == "needs_review" and o.result_summary is None
    assert st.get_job_result("f") is None and st.get_job("f").status is JobStatus.NEEDS_REVIEW


def test_concurrent_commits_single_winner(tmp_path):
    st = JobStore(tmp_path / "s.db")
    st.enqueue_job(task_id="g", backend="wsl", cwd="/tmp")
    st.claim_job("g", owner="w", lease_sec=300) if hasattr(st, "claim_job") else None
    st.mark_running("g", owner="w")
    s = parse_grok_json_result(OK, expected_text="BRIDGE_OK").to_dict()
    results = []

    def go():
        try:
            st.commit_job_result("g", owner="w", summary=s, terminal=JobStatus.SUCCEEDED)
            results.append("ok")
        except StoreError as e:
            results.append(e.code)
        except sqlite3.OperationalError:
            results.append("locked")
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results.count("ok") == 1
    assert _outbox_types(st, "g").count("job.succeeded") == 1


def test_timeout_is_uncertain_no_result_no_rerun(tmp_path):
    st = JobStore(tmp_path / "s.db")
    calls = []

    def fake(argv, **kw):
        calls.append(1)
        return RunResult(tuple(argv), None, OK, "", timed_out=True, error="timeout")
    o = execute_one_shot(st, _plan("h"), owner="w", dry_run=False, executor=fake)
    assert o.status == "needs_review" and o.error_code == "timeout_uncertain"
    assert st.get_job_result("h") is None and len(calls) == 1


def test_truncated_exit0_needs_review(tmp_path):
    st = JobStore(tmp_path / "s.db")

    def fake(argv, **kw):
        return RunResult(tuple(argv), 0, OK, "", stdout_truncated=True)
    o = execute_one_shot(st, _plan("i"), owner="w", dry_run=False, executor=fake)
    assert o.status == "needs_review" and o.error_code == "success_exit_invalid_result"
    assert st.get_job_result("i")["parse_status"] == "truncated"
