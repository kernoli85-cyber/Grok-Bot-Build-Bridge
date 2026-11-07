"""Every SQLite connection opened by JobStore / ReadOnlyState is closed, on success and failure."""
import gc
import os
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import grok_bot_build_bridge.state.readonly as ro_mod
import grok_bot_build_bridge.state.store as store_mod
from grok_bot_build_bridge.runner import build_approved_plan, execute_one_shot
from grok_bot_build_bridge.safe_exec import RunResult
from grok_bot_build_bridge.state import JobStatus, JobStore
from grok_bot_build_bridge.state.readonly import ReadOnlyState, ReadOnlyStateError
from grok_bot_build_bridge.state.store import StoreError

FIX = Path(__file__).parent / "fixtures"
OUT = '{"text":"x","stopReason":"end_turn"}'


class _Spy:
    def __init__(self):
        self.open = set()
        self.lock = threading.Lock()
        self.total = 0


@pytest.fixture
def spy(monkeypatch):
    s = _Spy()
    real = sqlite3.connect

    class Tracked(sqlite3.Connection):
        def close(self):
            with s.lock:
                s.open.discard(id(self))
            super().close()

    def connect(*a, **k):
        k["factory"] = Tracked
        c = real(*a, **k)
        with s.lock:
            s.open.add(id(c))
            s.total += 1
        return c
    monkeypatch.setattr(store_mod.sqlite3, "connect", connect)
    monkeypatch.setattr(ro_mod.sqlite3, "connect", connect)
    return s


def _plan(tid):
    p = build_approved_plan(task_id=tid, backend="wsl", cwd="/tmp",
                            executable=str(FIX / "bridge-dummy-grokjson.py"), extra_args=(), prompt=None)
    return replace(p, parse_grok_json=True)


def test_all_store_paths_close(spy, tmp_path):
    db = tmp_path / "s.db"
    st = JobStore(db)
    st.enqueue_job(task_id="a", backend="wsl", cwd="/tmp")
    st.list_jobs(); st.get_job("a"); st.list_outbox(); st.get_job_result("a")
    with pytest.raises(StoreError):
        st.mark_running("a", owner="nobody")      # failing tx
    with pytest.raises(StoreError):
        st.commit_job_result("a", owner="w", summary={"bad": 1}, terminal=JobStatus.SUCCEEDED)
    execute_one_shot(st, _plan("b"), owner="w", dry_run=False, heartbeat_interval_sec=0.01,
                     executor=lambda a, **k: RunResult(tuple(a), 0, OUT, ""))
    st.simulate_outbox_tick() if hasattr(st, "simulate_outbox_tick") else None
    assert spy.total > 5 and spy.open == set()


def test_exception_inside_tx_rolls_back_and_closes(spy, tmp_path):
    st = JobStore(tmp_path / "s.db")
    with pytest.raises(RuntimeError):
        with st._tx() as conn:
            conn.execute("INSERT INTO meta(key,value) VALUES('k','v')")
            raise RuntimeError("boom")
    assert spy.open == set()
    with st._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM meta WHERE key='k'").fetchone()[0] == 0


def test_readonly_paths_close(spy, tmp_path):
    db = tmp_path / "s.db"
    JobStore(db).enqueue_job(task_id="a", backend="wsl", cwd="/tmp")
    with ReadOnlyState(db) as r:
        r.list_jobs(); r.get_result("a")
    bad = tmp_path / "bad.sqlite"
    with sqlite3.connect(bad) as c:  # not via spy-patched module? patched globally -> close below
        c.execute("CREATE TABLE other(x)")
    c.close()
    with pytest.raises(ReadOnlyStateError):
        ReadOnlyState(bad).open()
    assert spy.open == set()


def test_db_can_be_renamed_and_deleted_after_use(tmp_path):
    """On Windows an open handle would block this."""
    db = tmp_path / "s.db"
    st = JobStore(db)
    st.enqueue_job(task_id="a", backend="wsl", cwd="/tmp")
    execute_one_shot(st, _plan("b"), owner="w", dry_run=False,
                     executor=lambda a, **k: RunResult(tuple(a), 0, OUT, ""))
    with ReadOnlyState(db) as r:
        r.get_job("a")
    moved = tmp_path / "moved.db"
    os.replace(db, moved)
    for side in ("-wal", "-shm"):
        if os.path.exists(str(db) + side):
            os.replace(str(db) + side, str(moved) + side)
    os.remove(moved)


def test_wal_sidecars_removed_after_last_close_without_gc(tmp_path):
    """No forced checkpoint: SQLite cleans WAL files when the last connection closes."""
    gc.disable()
    try:
        db = tmp_path / "s.db"
        st = JobStore(db)
        st.enqueue_job(task_id="a", backend="wsl", cwd="/tmp")
        st.get_job("a")
        assert sorted(p.name for p in tmp_path.iterdir()) == ["s.db"]
    finally:
        gc.enable()
