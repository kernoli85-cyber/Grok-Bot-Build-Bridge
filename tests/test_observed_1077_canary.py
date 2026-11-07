"""Regression evidence from two observed 1.0.77 local runs (redacted derived fixtures)."""
import json
from pathlib import Path

from grok_bot_build_bridge.runner import plan as P

FIX = Path(__file__).parent / "fixtures"


def _events(name):
    return [json.loads(l) for l in (FIX / name).read_text().splitlines() if l and not l.startswith("#")]


def test_canary_attempt_was_denied_not_executed():
    ev = _events("grok_stream_1077_canary_denied_redacted.jsonl")
    calls = [e for e in ev if e["type"] == "tool_call"]
    fails = [e for e in ev if e["type"] == "tool_call_update" and e["status"] == "failed"]
    assert len(calls) == 1 and calls[0]["rawInput"]["tool_name"] == "read_file"  # attempted via use_tool
    assert "Denied by permission policy" in fails[0]["content"][0]["content"]["text"]
    assert not [e for e in ev if e["type"] == "tool_call_update" and e["status"] == "completed"]
    assert ev[-1]["stopReason"] == "max_turn_requests"  # not success


def test_every_tool_offered_in_canary_run_is_now_disallowed():
    offered = _events("grok_stream_1077_canary_denied_redacted.jsonl")[0]["tools"]
    disallowed = set(P.GROK_DENY_BUILTIN_TOOLS.split(","))
    assert set(offered) <= disallowed
    assert _events("grok_stream_1077_text_no_tools_redacted.jsonl")[0]["tools"] == []


def test_ask_and_smoke_plans_carry_extended_denylist():
    p = P.build_wsl_grok_ask_plan(task_id="t", distro="Ubuntu", user="gbbbdemo", grok_path="/usr/bin/grok",
                                  cwd="/tmp/gbbb-ask-MockDir0001", prompt="x")
    v = p.argv[p.argv.index("--disallowed-tools") + 1]
    for t in ("use_tool", "search_tool", "write", "image_gen", "workflow"):
        assert t in v.split(",")
    assert "MCPTool" in p.argv and p.argv[p.argv.index("--permission-mode") + 1] == "dontAsk"
