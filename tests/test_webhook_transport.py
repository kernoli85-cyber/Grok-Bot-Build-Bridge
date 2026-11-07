from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from grok_bot_build_bridge.state.clock import Clock, Jitter
from grok_bot_build_bridge.state.store import JobStore
from grok_bot_build_bridge.state.types import DeliveryState
from grok_bot_build_bridge.transport.pump import pump_outbox
from grok_bot_build_bridge.transport.webhook import (
    NoRedirect,
    WebhookConfig,
    WebhookTransport,
    classify_http_status,
    load_webhook_config,
    parse_retry_after,
    sanitize_error,
    save_webhook_config,
    validate_webhook_url,
)


def test_url_https_only():
    with pytest.raises(ValueError, match="url_must_be_https"):
        validate_webhook_url("http://example.com/hook")


def test_url_rejects_userinfo():
    with pytest.raises(ValueError, match="url_userinfo_forbidden"):
        validate_webhook_url("https://user:pass@example.com/hook")


def test_classify_status():
    assert classify_http_status(204) == "accepted"
    assert classify_http_status(401) == "permanent"
    assert classify_http_status(403) == "permanent"
    assert classify_http_status(429) == "transient"
    assert classify_http_status(503) == "transient"


def test_sanitize_error_no_url_or_token_leak():
    s = sanitize_error("failed Bearer supersecrettoken123456 https://hooks.example/x?k=1")
    assert "supersecrettoken123456" not in s
    assert "hooks.example" in s or "<redacted>" in s
    assert "Bearer <redacted>" in s


def test_retry_after_capped():
    assert parse_retry_after({"Retry-After": "999"}, cap=60.0) == 60.0
    assert parse_retry_after({"Retry-After": "12"}, cap=60.0) == 12.0


def test_config_roundtrip_permissions(tmp_path: Path):
    path = tmp_path / "webhook.json"
    cfg = WebhookConfig(url="https://example.com/hook", bearer_token="tok_abc")
    meta = save_webhook_config(path, cfg)
    assert path.exists()
    loaded = load_webhook_config(path)
    assert loaded.url == cfg.url
    assert loaded.bearer_token == cfg.bearer_token
    assert "permission_guarantee" in meta
    # On Linux box chmod should apply
    import sys

    if not sys.platform.startswith("win"):
        assert (path.stat().st_mode & 0o777) == 0o600
        assert "windows_no_acl_claim" not in meta["permission_guarantee"]


class _FakeResp:
    def __init__(self, status: int, body: bytes = b"ok", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    def getcode(self):
        return self.status

    def read(self, n: int = -1):
        if n < 0:
            return self._body
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _transport_with_opener(opener, **kw):
    cfg = WebhookConfig(url="https://example.com/hook", bearer_token="secret-token-value")
    return WebhookTransport(cfg, opener=opener, **kw)


def test_2xx_accepted_not_processed_semantics():
    opener = MagicMock()
    opener.open.return_value = _FakeResp(200, b"{}")
    tr = _transport_with_opener(opener)
    result = tr.send_event_payload({"event_id": "e1", "event_type": "job.queued"})
    assert result.kind == "accepted"
    assert result.status_code == 200
    # Authorization present on request but not in result
    assert result.sanitized_error is None
    req = opener.open.call_args[0][0]
    assert req.get_header("Authorization") == "Bearer secret-token-value"
    # ensure result stringification wouldn't be used — check no token in dataclass fields
    assert "secret-token-value" not in str(result)


def test_401_permanent():
    import urllib.error

    opener = MagicMock()

    def raise_401(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://example.com/hook", 401, "Unauthorized", hdrs={}, fp=io.BytesIO(b"no")
        )

    opener.open.side_effect = raise_401
    result = _transport_with_opener(opener).send_event_payload({"event_id": "e"})
    assert result.kind == "permanent"
    assert result.status_code == 401


def test_429_transient_retry_after():
    import urllib.error
    from email.message import Message

    hdrs = Message()
    hdrs["Retry-After"] = "15"
    opener = MagicMock()

    def raise_429(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://example.com/hook", 429, "Too Many", hdrs=hdrs, fp=io.BytesIO(b"x")
        )

    opener.open.side_effect = raise_429
    result = _transport_with_opener(opener).send_event_payload({"event_id": "e"})
    assert result.kind == "transient"
    assert result.retry_after_sec == 15.0


def test_redirect_refused_permanent():
    import urllib.error

    opener = MagicMock()

    def raise_redirect(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://example.com/hook",
            302,
            "redirect_refused:302",
            hdrs={},
            fp=io.BytesIO(b""),
        )

    opener.open.side_effect = raise_redirect
    result = _transport_with_opener(opener).send_event_payload({"event_id": "e"})
    assert result.kind == "permanent"
    assert "redirect" in (result.sanitized_error or "")


def test_oversized_body_transient():
    opener = MagicMock()
    opener.open.return_value = _FakeResp(200, body=b"x" * 1000)
    result = _transport_with_opener(opener, max_body=100).send_event_payload({"event_id": "e"})
    assert result.kind == "transient"
    assert result.sanitized_error == "response_body_too_large"


def test_timeout_transient():
    import urllib.error

    opener = MagicMock()
    opener.open.side_effect = urllib.error.URLError(TimeoutError("timed out"))
    result = _transport_with_opener(opener).send_event_payload({"event_id": "e"})
    assert result.kind == "transient"
    assert result.sanitized_error == "timeout"


def test_pump_dry_run_no_network(tmp_path: Path):
    store = JobStore(tmp_path / "s.sqlite", clock=Clock(), jitter=Jitter(seed=1))
    store.clock.set(1.0)
    store.enqueue_job(task_id="t1")
    opener = MagicMock()
    transport = _transport_with_opener(opener)
    results = pump_outbox(store, transport, dry_run=True, limit=10)
    assert results[0]["dry_run"] is True
    opener.open.assert_not_called()
    # still pending
    assert store.list_outbox()[0].delivery == DeliveryState.PENDING


def test_pump_send_2xx_marks_http_accepted(tmp_path: Path):
    store = JobStore(tmp_path / "s.sqlite", clock=Clock(), jitter=Jitter(seed=1))
    store.clock.set(1.0)
    store.enqueue_job(task_id="t1")
    opener = MagicMock()
    opener.open.return_value = _FakeResp(202, b"ok")
    transport = _transport_with_opener(opener)
    results = pump_outbox(store, transport, dry_run=False, limit=10)
    assert results[0]["delivery"] == DeliveryState.HTTP_ACCEPTED.value
    assert results[0]["note"] == "http_accepted_not_bot_processed"
    assert store.list_outbox()[0].delivery == DeliveryState.HTTP_ACCEPTED


def test_pump_401_dead_letters(tmp_path: Path):
    import urllib.error

    store = JobStore(tmp_path / "s.sqlite", clock=Clock(), jitter=Jitter(seed=1))
    store.clock.set(1.0)
    store.enqueue_job(task_id="t1")
    opener = MagicMock()

    def raise_401(req, timeout=None):
        raise urllib.error.HTTPError(
            "https://example.com/hook", 401, "no", hdrs={}, fp=io.BytesIO(b"")
        )

    opener.open.side_effect = raise_401
    transport = _transport_with_opener(opener)
    results = pump_outbox(store, transport, dry_run=False)
    assert results[0]["delivery"] == DeliveryState.DEAD_LETTERED.value
    assert results[0]["transport_kind"] == "permanent"


def test_no_redirect_handler_class_present():
    # sanity: handler refuses 3xx
    assert hasattr(NoRedirect, "http_response")
