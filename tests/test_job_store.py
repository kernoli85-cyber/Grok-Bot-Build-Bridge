from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from grok_bot_build_bridge.state.clock import Clock, Jitter
from grok_bot_build_bridge.state.store import JobStore, StoreError
from grok_bot_build_bridge.state.types import DeliveryState, JobStatus


def make_store(tmp_path: Path, *, t0: float = 1_000.0) -> JobStore:
    clock = Clock()
    clock.set(t0)
    return JobStore(
        tmp_path / "state.sqlite",
        clock=clock,
        jitter=Jitter(seed=42),
        base_retry_sec=1.0,
        max_retry_sec=8.0,
    )


def test_enqueue_idempotent(tmp_path: Path):
    store = make_store(tmp_path)
    a = store.enqueue_job(task_id="t1", backend="wsl", cwd="/home/u/p")
    b = store.enqueue_job(task_id="t1", backend="native", cwd="/other")
    assert a.task_id == b.task_id
    assert b.status == JobStatus.QUEUED
    assert b.backend == "wsl"  # first write wins
    events = store.list_outbox()
    assert len([e for e in events if e.event_type == "job.queued"]) == 1


def test_happy_path_and_http_vs_bot(tmp_path: Path):
    store = make_store(tmp_path)
    store.enqueue_job(task_id="t1", correlation_id="c1")
    store.claim_job("t1", owner="w1")
    store.mark_running("t1", owner="w1")
    store.mark_succeeded("t1")
    job = store.get_job("t1")
    assert job is not None
    assert job.status == JobStatus.SUCCEEDED

    due = store.due_outbox()
    assert due
    tick = store.simulate_outbox_tick(transport_ok=True, bot_process=False)
    assert tick[0]["delivery"] == DeliveryState.HTTP_ACCEPTED.value
    assert "not_bot_processed" in tick[0]["note"]

    # bot processed separate
    remaining = [
        e
        for e in store.list_outbox()
        if e.delivery == DeliveryState.HTTP_ACCEPTED
    ]
    assert remaining
    store.ack_bot_processed(remaining[0].event_id)
    assert store.list_outbox()[0].delivery in {
        DeliveryState.BOT_PROCESSED,
        DeliveryState.HTTP_ACCEPTED,
    }


def test_no_replay_completed(tmp_path: Path):
    store = make_store(tmp_path)
    store.enqueue_job(task_id="t1")
    store.claim_job("t1", owner="w1")
    store.mark_running("t1", owner="w1")
    store.mark_succeeded("t1")
    with pytest.raises(StoreError) as ei:
        store.claim_job("t1", owner="w2")
    assert ei.value.code == "terminal_job"


def test_cancel_state_only(tmp_path: Path):
    store = make_store(tmp_path)
    store.enqueue_job(task_id="t1")
    job = store.cancel("t1")
    assert job.status == JobStatus.CANCELLED
    ev = [e for e in store.list_outbox() if e.event_type == "job.cancelled"][0]
    assert ev.payload.get("note") == "state_only_no_process_kill"


def test_fail_retry_then_deadletter(tmp_path: Path):
    store = make_store(tmp_path)
    store.enqueue_job(task_id="t1", max_attempts=2)
    store.claim_job("t1", owner="w1")
    store.mark_running("t1", owner="w1")
    job = store.mark_failed("t1", error_code="boom", retry=True)
    assert job.status == JobStatus.QUEUED
    assert job.attempt == 1

    store.claim_job("t1", owner="w1")
    store.mark_running("t1", owner="w1")
    job2 = store.mark_failed("t1", error_code="boom", retry=True)
    assert job2.status == JobStatus.DEAD_LETTERED
    assert job2.attempt == 2


def test_lease_expiry_reclaim(tmp_path: Path):
    store = make_store(tmp_path, t0=1000.0)
    store.enqueue_job(task_id="t1")
    store.claim_job("t1", owner="w1", lease_sec=10.0)
    store.clock.advance(11.0)
    job = store.claim_job("t1", owner="w2", lease_sec=10.0)
    assert job.status == JobStatus.CLAIMED
    assert job.lease_owner == "w2"


def test_outbox_retry_schedule_deterministic(tmp_path: Path):
    store = make_store(tmp_path, t0=5000.0)
    store.enqueue_job(task_id="t1")
    ev = store.due_outbox()[0]
    # fail twice and ensure next_attempt_at advances with seeded jitter
    e1 = store.fail_delivery(ev.event_id, error="x")
    assert e1.delivery == DeliveryState.FAILED
    assert e1.next_attempt_at > 5000.0
    first_next = e1.next_attempt_at
    store.clock.set(first_next)
    e2 = store.fail_delivery(ev.event_id, error="x")
    assert e2.attempt == 2
    assert e2.next_attempt_at > first_next


def test_outbox_deadletter_after_max(tmp_path: Path):
    store = JobStore(
        tmp_path / "s.sqlite",
        clock=Clock(),
        jitter=Jitter(seed=1),
        base_retry_sec=0.01,
        max_retry_sec=0.01,
    )
    store.clock.set(1.0)
    store.enqueue_job(task_id="t1")
    # override event max by failing many times — default max_attempts=8
    ev = store.list_outbox()[0]
    # manually lower max via SQL for speed
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE outbox SET max_attempts=2 WHERE event_id=?", (ev.event_id,)
        )
    store.fail_delivery(ev.event_id, error="a")
    store.clock.advance(1.0)
    final = store.fail_delivery(ev.event_id, error="b")
    assert final.delivery == DeliveryState.DEAD_LETTERED


def test_duplicate_event_dedupe(tmp_path: Path):
    store = make_store(tmp_path)
    store.enqueue_job(task_id="t1")
    # second enqueue should not add second queued event
    store.enqueue_job(task_id="t1")
    assert len(store.list_outbox()) == 1


def test_payload_strips_secrets(tmp_path: Path):
    store = make_store(tmp_path)
    store.enqueue_job(task_id="t1")
    with store._tx() as conn:
        store._insert_event(
            conn,
            task_id="t1",
            correlation_id="t1",
            event_type="job.progress",
            payload={
                "status": "running",
                "prompt": "SECRET_PROMPT",
                "token": "SECRET",
                "progress_pct": 10,
            },
            dedupe_key="progress:10",
        )
    ev = [e for e in store.list_outbox() if e.event_type == "job.progress"][0]
    assert "prompt" not in ev.payload
    assert "token" not in ev.payload
    assert ev.payload["progress_pct"] == 10


def test_malformed_empty_task_id(tmp_path: Path):
    store = make_store(tmp_path)
    with pytest.raises(StoreError) as ei:
        store.enqueue_job(task_id="  ")
    assert ei.value.code == "task_id_empty"


def test_illegal_transition(tmp_path: Path):
    store = make_store(tmp_path)
    store.enqueue_job(task_id="t1")
    with pytest.raises(StoreError) as ei:
        store.mark_running("t1", owner="w1")
    assert ei.value.code in {"illegal_transition", "lease_mismatch"}


def test_restart_recovery_same_db(tmp_path: Path):
    db = tmp_path / "persist.sqlite"
    s1 = JobStore(db, clock=Clock(), jitter=Jitter(seed=1))
    s1.clock.set(10.0)
    s1.enqueue_job(task_id="t1", correlation_id="c1")
    s1.claim_job("t1", owner="w1")
    # reopen
    s2 = JobStore(db, clock=Clock(), jitter=Jitter(seed=1))
    s2.clock.set(20.0)
    job = s2.get_job("t1")
    assert job is not None
    assert job.status == JobStatus.CLAIMED
    assert len(s2.list_outbox()) >= 2


def test_db_mode_private(tmp_path: Path):
    store = make_store(tmp_path)
    store.enqueue_job(task_id="t1")
    mode = store.db_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_simulated_transport_failure(tmp_path: Path):
    store = make_store(tmp_path)
    store.enqueue_job(task_id="t1")
    results = store.simulate_outbox_tick(transport_ok=False)
    assert results[0]["delivery"] in {
        DeliveryState.FAILED.value,
        DeliveryState.DEAD_LETTERED.value,
    }
    assert results[0]["simulated"] is True


def test_order_due_outbox(tmp_path: Path):
    store = make_store(tmp_path, t0=100.0)
    store.enqueue_job(task_id="a")
    store.clock.advance(1.0)
    store.enqueue_job(task_id="b")
    due = store.due_outbox()
    assert [e.task_id for e in due] == ["a", "b"]
