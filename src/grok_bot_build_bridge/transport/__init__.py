"""Outbound transports (webhook). Secrets never live in the repo or outbox DB."""

from .webhook import (
    WebhookConfig,
    WebhookTransport,
    TransportResult,
    validate_webhook_url,
)

__all__ = [
    "TransportResult",
    "WebhookConfig",
    "WebhookTransport",
    "validate_webhook_url",
]
