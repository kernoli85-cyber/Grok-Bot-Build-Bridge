from __future__ import annotations

import json

from grok_bot_build_bridge.cli import main


def test_cli_version(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "0.3.0"


def test_cli_discover_demo(capsys):
    assert main(["discover-demo"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["kind"] == "wsl_only"
    assert data["default_backend"] == "wsl"


def test_cli_plan_demo(capsys):
    assert main(["plan-demo"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["backend"] == "wsl"
    assert data["argv"][0] == "grok"
    assert data["argv"][1] == "--single"
    assert "note" in data


def test_cli_discover_live(capsys):
    # On Linux CI/box, tools are typically missing → kind none; must not crash
    assert main(["discover"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["kind"] in {"none", "native_only", "wsl_only", "both"}
    assert "diagnostics" in data
    assert "host_note" in data


def test_cli_jobs_demo(tmp_path, capsys):
    db = tmp_path / "s.sqlite"
    assert main(["jobs-demo", "--state-db", str(db), "--task-id", "cli-t1"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["simulated"] is True
    assert data["job"]["status"] == "succeeded"
    assert main(["jobs-status", "--state-db", str(db)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["jobs"][0]["task_id"] == "cli-t1"
