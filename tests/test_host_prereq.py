"""Host prereq helper: fixtures (Windows-shaped) + script packaging; no live host writes."""

from __future__ import annotations

import json
from pathlib import Path

from grok_bot_build_bridge.cli import main
from grok_bot_build_bridge.host_prereq import (
    fixture_host_prereq_windows,
    packaged_host_prereq_script_text,
)
from grok_bot_build_bridge.resources import fixture_text

ROOT = Path(__file__).resolve().parents[1]


def test_fixture_windows_running_no_companion_version():
    report = fixture_host_prereq_windows(
        wsl_list_text=fixture_text("wsl_list_v_running.txt"),
        companion_present=False,
    ).to_dict()
    assert report["schema_version"] == 1
    assert report["runtime_kind"] == "windows_host"
    assert report["companion_cli"]["status"] == "absent"
    assert report["companion_cli"]["version"] is None
    assert report["wsl"]["never_auto_start"] is True
    assert report["wsl"]["stopped_present"] is False
    assert any(d["state"] == "Running" for d in report["wsl"]["distros"])
    assert "secrets" in report
    blob = json.dumps(report)
    assert "Bearer" not in blob
    assert "token" not in blob.lower() or "not" in report["authority_note"].lower()


def test_fixture_windows_stopped_never_start_flag():
    text = fixture_text("wsl_list_v_stopped.txt")
    report = fixture_host_prereq_windows(wsl_list_text=text).to_dict()
    assert report["wsl"]["stopped_present"] is True
    assert report["wsl"]["never_auto_start"] is True
    stopped = [d for d in report["wsl"]["distros"] if d["state"] == "Stopped"]
    assert stopped
    assert stopped[0]["note"] == "stopped_ask_operator_before_start"


def test_cli_host_prereq_fixture(capsys):
    assert main(["host-prereq", "--fixture", "windows-stopped"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["check_kind"] == "host_prereq_fixture"
    assert data["wsl"]["stopped_present"] is True
    assert data["companion_cli"]["version"] is None


def test_cli_host_prereq_script_matches_repo(capsys):
    assert main(["host-prereq-script"]) == 0
    out = capsys.readouterr().out
    repo = (ROOT / "scripts" / "host-prereq-check.ps1").read_text(encoding="utf-8")
    assert out.strip() == repo.strip()
    assert "ConvertTo-Json" in out
    assert "never" in out.lower()
    # no secret-looking assignments
    assert "Bearer " not in out
    assert "ghp_" not in out


def test_packaged_script_resource_api():
    text = packaged_host_prereq_script_text()
    assert text.startswith("#Requires")
    assert "host_prereq_builtin" in text or "check_kind" in text


def test_fixture_not_live_host_evidence_marker():
    report = fixture_host_prereq_windows(
        wsl_list_text=fixture_text("wsl_list_v_running.txt")
    ).to_dict()
    assert "Fixture only" in " ".join(report["next_operator_steps"])
