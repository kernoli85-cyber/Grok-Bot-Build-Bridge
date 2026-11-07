"""Persistent job state + outbox (own app SQLite only)."""

from .paths import StorePaths
from .store import JobStore, StoreError
from .types import (
    EVENT_SCHEMA_VERSION,
    DeliveryState,
    JobRecord,
    JobStatus,
    OutboxEvent,
)

__all__ = [
    "EVENT_SCHEMA_VERSION",
    "DeliveryState",
    "JobRecord",
    "JobStatus",
    "JobStore",
    "OutboxEvent",
    "StoreError",
    "StorePaths",
]
