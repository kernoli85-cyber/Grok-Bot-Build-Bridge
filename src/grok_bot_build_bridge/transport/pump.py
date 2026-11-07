"""Outbox pump: dry-run or explicit send via webhook transport (no daemon)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..state.store import JobStore
from ..state.types import DeliveryState, OutboxEvent
from .webhook import TransportResult, WebhookTransport, build_outbox_post_body


@dataclass(frozen=True)
class PumpItemResult:
    event_id: str
    task_id: str
    dry_run: bool
    delivery: str | None
    transport_kind: str | None
    status_code: int | None
    sanitized_error: str | None
    note: str


def _soft_claim(store: JobStore, ev: OutboxEvent, *, lease_sec: float) -> bool:
    """Move next_attempt_at forward under a write lock to reduce duplicate sends."""
    now = store.clock.time()
    with store._tx() as conn:
        row = conn.execute(
            "SELECT delivery, next_attempt_at FROM outbox WHERE event_id = ?",
            (ev.event_id,),
        ).fetchone()
        if not row:
            return False
        if row["delivery"] not in {
            DeliveryState.PENDING.value,
            DeliveryState.FAILED.value,
        }:
            return False
        if float(row["next_attempt_at"]) > now:
            return False
        conn.execute(
            "UPDATE outbox SET next_attempt_at = ? WHERE event_id = ?",
            (now + lease_sec, ev.event_id),
        )
        return True


def pump_outbox(
    store: JobStore,
    transport: WebhookTransport | None,
    *,
    dry_run: bool = True,
    limit: int = 20,
    send_lease_sec: float = 30.0,
) -> list[dict[str, Any]]:
    """Process due outbox events once.

    dry_run=True (default): no network — report what would be sent.
    dry_run=False: requires transport; POST; 2xx => http_accepted (not bot_processed).
    """
    if limit < 1 or limit > 100:
        raise ValueError("limit_out_of_bounds")
    results: list[dict[str, Any]] = []
    due = store.due_outbox(limit=limit)
    for ev in due:
        body = build_outbox_post_body(ev)
        if dry_run:
            results.append(
                {
                    "event_id": ev.event_id,
                    "task_id": ev.task_id,
                    "dry_run": True,
                    "would_post": {
                        "scheme_host": transport.config.redacted()["url_scheme_host"]
                        if transport
                        else "<no_transport>",
                        "event_type": ev.event_type,
                        "body_keys": sorted(body.keys()),
                    },
                    "note": "dry_run_no_network",
                }
            )
            continue

        if transport is None:
            raise ValueError("transport_required_for_send")

        if not _soft_claim(store, ev, lease_sec=send_lease_sec):
            results.append(
                {
                    "event_id": ev.event_id,
                    "task_id": ev.task_id,
                    "dry_run": False,
                    "delivery": None,
                    "transport_kind": "skipped",
                    "note": "lease_or_state_conflict_no_duplicate_send",
                }
            )
            continue

        tr: TransportResult = transport.send_event_payload(body)
        if tr.kind == "accepted":
            updated = store.ack_http_accepted(ev.event_id)
            results.append(
                {
                    "event_id": ev.event_id,
                    "task_id": ev.task_id,
                    "dry_run": False,
                    "delivery": updated.delivery.value,
                    "transport_kind": tr.kind,
                    "status_code": tr.status_code,
                    "note": "http_accepted_not_bot_processed",
                }
            )
        elif tr.kind == "permanent":
            updated = store.mark_outbox_dead_letter(
                ev.event_id, error=tr.sanitized_error or "permanent"
            )
            results.append(
                {
                    "event_id": ev.event_id,
                    "task_id": ev.task_id,
                    "dry_run": False,
                    "delivery": updated.delivery.value,
                    "transport_kind": tr.kind,
                    "status_code": tr.status_code,
                    "sanitized_error": tr.sanitized_error,
                    "note": "permanent_failure_dead_letter",
                }
            )
        else:
            # transient / error
            if tr.retry_after_sec is not None:
                # align next attempt using fail_delivery then override next_attempt_at
                updated = store.fail_delivery(
                    ev.event_id, error=tr.sanitized_error or "transient"
                )
                with store._tx() as conn:
                    conn.execute(
                        "UPDATE outbox SET next_attempt_at = ? WHERE event_id = ?",
                        (store.clock.time() + float(tr.retry_after_sec), ev.event_id),
                    )
                updated = [
                    e for e in store.list_outbox() if e.event_id == ev.event_id
                ][0]
            else:
                updated = store.fail_delivery(
                    ev.event_id, error=tr.sanitized_error or tr.kind
                )
            results.append(
                {
                    "event_id": ev.event_id,
                    "task_id": ev.task_id,
                    "dry_run": False,
                    "delivery": updated.delivery.value,
                    "transport_kind": tr.kind,
                    "status_code": tr.status_code,
                    "sanitized_error": tr.sanitized_error,
                    "note": "transient_or_error_scheduled_retry",
                }
            )
    return results
