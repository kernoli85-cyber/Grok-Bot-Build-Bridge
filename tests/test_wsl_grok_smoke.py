import json
import pytest
from grok_bot_build_bridge import cli
from grok_bot_build_bridge.runner.plan import PlanError, build_wsl_grok_smoke_plan
from grok_bot_build_bridge.safe_exec import RunResult

CWD = "/tmp/gbbb-smoke-AbC123xyz"
KW = dict(task_id="s1", distro="Ubuntu", user="gbbbdemo", grok_path="/home/gbbbdemo/.local/bin/grok", cwd=CWD)
RUNNING = "  NAME STATE VERSION\n* Ubuntu    Running         2\n"


def test_smoke_plan_fixed_flags():
    a = build_wsl_grok_smoke_plan(**KW).argv
    assert a[:9] == ("wsl.exe", "-d", "Ubuntu", "-u", "gbbbdemo", "--cd", CWD, "--exec", "/usr/bin/env")
    assert "GROK_MEMORY=0" in a
    assert a[a.index("--max-turns") + 1] == "1"
    assert a[a.index("--sandbox") + 1] == "strict"
    assert a[a.index("--permission-mode") + 1] == "dontAsk"
    denies = [a[i + 1] for i, x in enumerate(a) if x == "--deny"]
    assert set(denies) >= {"MCPTool", "Bash", "Read", "Write", "Edit", "Grep", "WebFetch"}
    j = " ".join(a).lower()
    for bad in ("--always-approve", "bypasspermissions", "--yolo", "read-only", "--trust", "off"):
        assert bad not in j.split() and bad not in j.replace("--sandbox strict", "")


@pytest.mark.parametrize("bad", [{"cwd": "/home/gbbbdemo"}, {"cwd": "/tmp"}, {"cwd": "/tmp/gbbb-smoke-a/../x"},
                                 {"user": "root"}, {"grok_path": "/bin/sh"}])
def test_smoke_plan_rejects(bad):
    with pytest.raises(PlanError):
        build_wsl_grok_smoke_plan(**{**KW, **bad})


def _base(tmp_path):
    return ["runner-run", "--state-db", str(tmp_path / "s.db"), "--task-id", "s1", "--cwd", CWD,
            "--wsl-grok-smoke", "--wsl-distro", "Ubuntu", "--wsl-user", "gbbbdemo",
            "--executable", "/home/gbbbdemo/.local/bin/grok"]


def test_cli_dry_run_and_gate_default_off(tmp_path, capsys, monkeypatch):
    called = []
    monkeypatch.setattr(cli, "_wsl_list_reader", lambda: (called.append(1), (RUNNING, None))[1])
    assert cli.main(_base(tmp_path)) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["grok_executed"] is False and "<redacted_prompt>" in d["argv_preview"]
    assert cli.main(_base(tmp_path) + ["--execute"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "model_call_gated"
    assert not called


def test_cli_allowed_call_uses_wsl_exe_mocked(tmp_path, capsys, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_wsl_list_reader", lambda: (RUNNING, None))
    import grok_bot_build_bridge.runner.worker as w

    def fake(argv, **kw):
        seen["argv"] = tuple(argv)
        return RunResult(tuple(argv), 0, '{"text":"BRIDGE_OK","stopReason":"end_turn"}', "")
    monkeypatch.setattr(w, "run_argv", fake)
    orig = w.execute_one_shot
    monkeypatch.setattr(cli, "execute_one_shot", lambda s, p, **k: orig(s, p, executor=fake, **k))
    assert cli.main(_base(tmp_path) + ["--execute", "--allow-model-call"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["grok_executed"] is True and d["status"] == "succeeded"
    assert seen["argv"][0] == "wsl.exe"
    rs = d["result_summary"]
    assert d["process_exit_ok"] is True and rs["model_ok"] and rs["matched_text"] == "BRIDGE_OK"
    assert rs["cost_status"] == "unknown" and rs["total_cost_usd"] is None


def test_cli_exit0_unparseable_is_needs_review_not_success(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_wsl_list_reader", lambda: (RUNNING, None))
    import grok_bot_build_bridge.runner.worker as w
    calls = []

    def fake(argv, **kw):
        calls.append(1)
        return RunResult(tuple(argv), 0, "BRIDGE_OK", "")  # plain text, not JSON
    orig = w.execute_one_shot
    monkeypatch.setattr(cli, "execute_one_shot", lambda s, p, **k: orig(s, p, executor=fake, **k))
    assert cli.main(_base(tmp_path) + ["--execute", "--allow-model-call"]) != 0
    d = json.loads(capsys.readouterr().out)
    assert d["process_exit_ok"] is True and d["status"] == "needs_review"
    assert d["error_code"] == "success_exit_invalid_result"
    assert d["result_summary"]["matched_text"] is None and len(calls) == 1  # no rerun


def test_cli_allowed_call_refuses_stopped(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "_wsl_list_reader", lambda: ("* Ubuntu    Stopped         2\n", None))
    assert cli.main(_base(tmp_path) + ["--execute", "--allow-model-call"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "wsl_distro_not_running"
