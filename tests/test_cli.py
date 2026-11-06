from __future__ import annotations

import json

from grok_bot_build_bridge.cli import main


def test_cli_version(capsys):
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == "0.1.0"


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
