"""ACP client prototype against a MOCK agent subprocess (no grok, no model, no network)."""
import sys
import time
from pathlib import Path

import pytest

from grok_bot_build_bridge.acp_client import AcpClient, AcpError, AcpLimits, close_agent, spawn_agent

MOCK = Path(__file__).parent / "fixtures" / "acp_mock_agent.py"


def _run(mode, limits=AcpLimits(request_timeout_sec=10, cancel_grace_sec=2), prompt_timeout=10):
    p = spawn_agent([sys.executable, str(MOCK), mode])
    c = AcpClient(p.stdin, p.stdout, limits)
    try:
        init = c.initialize()
        sid = c.new_session("/tmp/gbbb-ask-MockDir0001")
        return init, c.prompt(sid, "Antworte nur LOCAL_BRIDGE_OK", timeout=prompt_timeout)
    finally:
        close_agent(p, grace=3)
        assert p.poll() is not None  # no process left behind


def test_text_turn_streams_and_advertises_no_fs_terminal():
    init, t = _run("ok")
    assert init["_seen_caps"] == {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}
    assert t.stop_reason == "end_turn" and t.text == "LOCAL_BRIDGE_OK" and t.tool_calls == 0


def test_permission_always_rejected():
    _, t = _run("permission")
    assert t.text == "DENIED_OK" and t.permission_requests == t.permission_rejected == 1
    assert t.tool_calls == 1 and t.tool_statuses == {"pending": 1, "failed": 1}


def test_permission_without_reject_option_is_cancelled():
    assert _run("permission_no_reject")[1].text == "CANCELLED_OK"


def test_client_side_fs_terminal_unknown_requests_refused():
    _, t = _run("client_requests")
    assert t.text == "REFUSED_OK" and t.refused_client_requests == 4


@pytest.mark.parametrize("mode,code", [
    ("contaminate", "stdout_contamination"), ("contaminate_early", "stdout_contamination"),
    ("huge", "line_too_large"), ("unknown_id", "unexpected_response"), ("crash", "peer_closed"),
])
def test_fail_closed_protocol_violations(mode, code):
    t0 = time.monotonic()
    with pytest.raises(AcpError) as e:
        _run(mode)
    assert e.value.code == code and time.monotonic() - t0 < 15


def test_streamed_text_bounded():
    _, t = _run("flood_text", AcpLimits(max_text_chars=5000, request_timeout_sec=10))
    assert len(t.text) == 5000 and t.text_truncated and t.stop_reason == "end_turn"


def test_deadline_sends_cancel_and_accepts_cancelled():
    _, t = _run("stall_cancel", prompt_timeout=0.5)
    assert t.cancelled_by_client and t.stop_reason == "cancelled" and t.text == "partial"


def test_cancel_ignored_is_timeout_uncertain_and_process_cleaned():
    t0 = time.monotonic()
    with pytest.raises(AcpError) as e:
        _run("stall_ignore_cancel", AcpLimits(request_timeout_sec=10, cancel_grace_sec=0.5), prompt_timeout=0.5)
    assert e.value.code == "timeout_uncertain" and time.monotonic() - t0 < 15
