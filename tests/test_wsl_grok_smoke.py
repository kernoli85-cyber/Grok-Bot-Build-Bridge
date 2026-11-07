import json
import pytest
from grok_bot_build_bridge import cli
from grok_bot_build_bridge.runner.plan import PlanError, build_wsl_grok_smoke_plan

KW = dict(task_id="s1", distro="Ubuntu", user="okern", grok_path="/home/okern/.local/bin/grok", cwd="/home/okern")


def test_smoke_plan_fixed_safe_flags():
    p = build_wsl_grok_smoke_plan(**KW)
    a = p.argv
    assert a[:8] == ("wsl.exe", "-d", "Ubuntu", "-u", "okern", "--cd", "/home/okern", "--exec")
    assert a[a.index("--max-turns") + 1] == "1"
    assert a[a.index("--sandbox") + 1] == "read-only"
    assert "Agent" in a[a.index("--disallowed-tools") + 1]
    joined = " ".join(a).lower()
    assert "--always-approve" not in joined and "bypasspermissions" not in joined and "--yolo" not in joined
    assert p.execution_kind == "grok_smoke_one_shot"


@pytest.mark.parametrize("bad", [{"user": "root"}, {"grok_path": "grok"}, {"grok_path": "/bin/sh"}, {"cwd": "x"}])
def test_smoke_plan_rejects(bad):
    with pytest.raises(PlanError):
        build_wsl_grok_smoke_plan(**{**KW, **bad})


def test_cli_smoke_dry_run_and_execute_gated(tmp_path, capsys):
    base = ["runner-run", "--state-db", str(tmp_path / "s.db"), "--task-id", "s1", "--cwd", "/home/okern",
            "--wsl-grok-smoke", "--wsl-distro", "Ubuntu", "--wsl-user", "okern",
            "--executable", "/home/okern/.local/bin/grok"]
    assert cli.main(base) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["grok_executed"] is False and d["limits"]["max_turns_is_cost_cap"] is False
    assert "<redacted_prompt>" in d["argv_preview"]
    assert cli.main(base + ["--execute"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "model_call_gated"
