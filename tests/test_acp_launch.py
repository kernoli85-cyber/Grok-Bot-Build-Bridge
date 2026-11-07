"""ACP launch gate: stays closed on the observed 1.0.77 agent help (no --deny/--max-turns)."""
from pathlib import Path

import pytest

from grok_bot_build_bridge import acp_launch as L

HELP = (Path(__file__).parent / "fixtures" / "grok_1077_agent_help.txt").read_text()
CWD = "/tmp/gbbb-ask-MockDir0001"
KW = dict(distro="Ubuntu", user="okern", grok_path="/home/okern/.local/bin/grok", cwd=CWD,
          model="LM-Studio", profile_path=CWD + "/gbbb-profile.md")


def test_observed_1077_help_keeps_live_acp_closed():
    p = L.build_acp_launch_plan(agent_help=HELP, **KW)
    assert p.live_allowed is False and p.gaps == ("deny_rule_layer", "turn_cap")
    a = p.argv
    assert a[a.index("agent"):] == ("agent", "--no-leader", "--model", "LM-Studio", "--agent-profile",
                                    CWD + "/gbbb-profile.md", "stdio")
    assert "GROK_SANDBOX=strict" in a and "/usr/bin/timeout" in a
    for bad in ("--always-approve", "--yolo", "bypassPermissions"):
        assert bad not in a


def test_gate_opens_only_when_help_lists_missing_flags():
    more = HELP + "      --deny <RULE>\n          x\n      --max-turns <N>\n          y\n"
    p = L.build_acp_launch_plan(agent_help=more, **KW)
    assert p.live_allowed and p.gaps == ()
    assert p.argv.count("--deny") == 7 and p.argv[p.argv.index("--max-turns") + 1] == "1"
    assert p.argv[-1] == "stdio"


def test_empty_or_foreign_help_fails_closed():
    assert not L.build_acp_launch_plan(agent_help="", **KW).live_allowed


def test_profile_is_text_only_and_uses_documented_fields():
    t = L.profile_text()
    assert t.startswith("---\nname: gbbb-text-only\n") and "tools: []" in t
    assert "permissionMode: dontAsk" in t and "agentsMd: false" in t
    for tool in ("use_tool", "search_tool", "write", "image_gen", "read_file", "run_terminal_cmd"):
        assert f"  - {tool}\n" in t
    assert "bypass" not in t and "yolo" not in t.lower()


@pytest.mark.parametrize("kw", [
    {"cwd": "/tmp/other"}, {"profile_path": "/tmp/gbbb-profile.md"},
    {"profile_path": "/tmp/gbbb-ask-OtherDir0001/gbbb-profile.md"}, {"model": "--yolo"},
    {"grok_path": "grok"}, {"guest_timeout_sec": 5000},
])
def test_launch_inputs_validated(kw):
    with pytest.raises(Exception):
        L.build_acp_launch_plan(agent_help=HELP, **{**KW, **kw})
