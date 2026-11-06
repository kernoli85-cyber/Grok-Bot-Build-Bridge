"""Host prereq: Python helpers + PS1 drift lock + Windows CI script execution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from grok_bot_build_bridge.cli import main
from grok_bot_build_bridge.host_prereq import (
    fixture_host_prereq_windows,
    is_windowsapps_alias_path,
    packaged_host_prereq_script_text,
    parse_wsl_list_with_skips,
    version_meets_min,
)
from grok_bot_build_bridge.resources import fixture_text

ROOT = Path(__file__).resolve().parents[1]
PS1 = ROOT / "scripts" / "host-prereq-check.ps1"
PACKAGED_PS1 = (
    ROOT
    / "src"
    / "grok_bot_build_bridge"
    / "resources"
    / "scripts"
    / "host-prereq-check.ps1"
)


def test_repo_and_packaged_script_byte_identical():
    assert PS1.read_bytes() == PACKAGED_PS1.read_bytes()
    assert packaged_host_prereq_script_text() == PS1.read_text(encoding="utf-8")


def test_version_meets_min_enforces_311():
    assert version_meets_min("3.11.0")
    assert version_meets_min("3.12.1")
    assert not version_meets_min("3.10.9")
    assert not version_meets_min("2.7.18")
    assert not version_meets_min("nope")


def test_windowsapps_alias_detection():
    assert is_windowsapps_alias_path(
        r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\python.exe"
    )
    assert not is_windowsapps_alias_path(r"C:\Python312\python.exe")


def test_wsl_parse_skips_localized_and_spaced_names():
    loc = "  NAME      STATE           VERSION\n* Ubuntu    Wird ausgeführt         2\n"
    parsed = parse_wsl_list_with_skips(loc)
    assert parsed["distros"] == []
    assert parsed["skipped_lines"]

    space = "* My Distro    Running         2\n"
    parsed2 = parse_wsl_list_with_skips(space)
    assert parsed2["distros"] == []

    good = fixture_text("wsl_list_v_running.txt")
    parsed3 = parse_wsl_list_with_skips(good)
    assert parsed3["distros"][0]["state"] == "Running"
    assert parsed3["distros"][0]["readiness"] == "listed_running_not_probed"


def test_wsl_nul_stripped_english_row():
    # UTF-16-LE style NULs between chars after naive decode simulation
    text = "N\x00A\x00M\x00E\x00\n* Ubuntu    Running         2\n"
    parsed = parse_wsl_list_with_skips(text)
    assert any(d["name"] == "Ubuntu" for d in parsed["distros"])


def test_fixture_windows_running_no_companion_version():
    report = fixture_host_prereq_windows(
        wsl_list_text=fixture_text("wsl_list_v_running.txt"),
        companion_present=False,
    ).to_dict()
    assert report["python"]["meets_min"] is True
    assert report["companion_cli"]["status"] == "absent"
    assert report["companion_cli"]["version"] is None
    assert report["wsl"]["never_auto_start"] is True


def test_fixture_companion_unknown_vs_absent():
    absent = fixture_host_prereq_windows(
        wsl_list_text=fixture_text("wsl_list_v_running.txt"),
        companion_status="absent",
    ).to_dict()
    unknown = fixture_host_prereq_windows(
        wsl_list_text=fixture_text("wsl_list_v_running.txt"),
        companion_status="unknown",
    ).to_dict()
    assert absent["companion_cli"]["status"] == "absent"
    assert unknown["companion_cli"]["status"] == "unknown"
    assert unknown["companion_cli"]["version"] is None


def test_fixture_rejects_old_python_as_not_present():
    report = fixture_host_prereq_windows(
        wsl_list_text=fixture_text("wsl_list_v_running.txt"),
        python_version_text="Python 3.10.11",
    ).to_dict()
    assert report["python"]["present"] is False
    assert report["python"]["meets_min"] is False


def test_cli_host_prereq_fixture(capsys):
    assert main(["host-prereq", "--fixture", "windows-stopped"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["wsl"]["stopped_present"] is True
    assert data["companion_cli"]["version"] is None


def test_cli_host_prereq_script_matches_repo(capsys):
    assert main(["host-prereq-script"]) == 0
    out = capsys.readouterr().out
    assert out == PS1.read_text(encoding="utf-8") or out.strip() == PS1.read_text(
        encoding="utf-8"
    ).strip()


def _powershell() -> str | None:
    if sys.platform.startswith("win"):
        return "powershell.exe"
    # Optional on Linux agents — prefer pwsh when present for extra coverage
    for cand in ("pwsh", "powershell"):
        from shutil import which

        if which(cand):
            return cand
    return None


@pytest.mark.skipif(
    _powershell() is None, reason="PowerShell not available on this runner"
)
def test_ps1_selftest_mocks_wsl_localization_timeout_and_version_gate():
    exe = _powershell()
    assert exe
    r = subprocess.run(
        [
            exe,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PS1),
            "-SelfTest",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
    )
    assert r.returncode == 0, r.stdout + r.stderr
    data = json.loads(r.stdout.strip().splitlines()[-1])
    assert data["ok"] is True


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows CI live script")
def test_ps1_live_report_python_fields_on_windows_ci():
    """Real script against the CI interpreter — no companion version assert."""
    r = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(PS1),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        cwd=str(ROOT),
        env={k: v for k, v in os.environ.items() if k != "BRIDGE_HOST_PREREQ_MOCK"},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    data = json.loads(r.stdout)
    assert data["check_kind"] == "host_prereq_builtin"
    assert data["runtime_kind"] == "windows_host"
    # CI images ship real Python 3.12
    assert data["python"]["present"] is True
    assert data["python"]["meets_min"] is True
    assert version_meets_min(data["python"]["version"])
    exe = data["python"]["executable"]
    assert exe
    assert not is_windowsapps_alias_path(exe)
    assert Path(exe).exists()
    assert data["python"]["argv_prefix"]
    # venv must be probed via resolved executable, not bare py.exe without -3
    assert data["venv"]["present"] is True
    assert data["companion_cli"]["version"] is None
    assert data["companion_cli"]["status"] in {"absent", "present_on_path", "unknown"}
    assert data["wsl"]["never_auto_start"] is True
    # Do not invent readiness from skipped lines
    for d in data["wsl"].get("distros") or []:
        assert d.get("readiness") in {
            "listed_running_not_probed",
            "not_ready_from_list_alone",
            None,
        }


def test_python_probe_retains_py_dash3_prefix(monkeypatch):
    """Regression: venv/follow-up must not call bare py.exe without -3."""
    from grok_bot_build_bridge.host_prereq import collect_host_prereq
    from grok_bot_build_bridge.safe_exec import RunResult

    calls: list[tuple[str, ...]] = []

    def runner(argv, timeout_sec=8, **kwargs):
        args = tuple(argv)
        calls.append(args)
        if args[:2] == ("py", "-3") and args[-1] == "--version":
            return RunResult(args, 0, "Python 3.12.0\n", "")
        if args[:2] == ("py", "-3") and "-c" in args:
            code = args[args.index("-c") + 1]
            if "sys.executable" in code:
                return RunResult(
                    args,
                    0,
                    "3.12.0\nC:\\Python312\\python.exe\n",
                    "",
                )
            if "venv" in code:
                # Must be resolved exe, not ('py', '-c', ...)
                raise AssertionError(f"venv probe used launcher argv: {args}")
        if args[0].endswith("python.exe") and "-c" in args and "venv" in args[-1]:
            return RunResult(args, 0, "venv_ok\n", "")
        if args[0] in {"which", "powershell.exe"}:
            return RunResult(args, 1, "ABSENT\n", "")
        if args[0] in {"wsl", "wsl.exe"}:
            return RunResult(args, 0, fixture_text("wsl_list_v_running.txt"), "")
        return RunResult(args, -1, "", "", error="executable_not_found")

    report = collect_host_prereq(runner=runner).to_dict()
    assert report["python"]["argv_prefix"] == ["py", "-3"]
    assert report["python"]["executable"].endswith("python.exe")
    assert report["venv"]["present"] is True
    # Ensure a venv call used the resolved executable
    assert any(
        c[0].endswith("python.exe") and "venv" in c[-1] for c in calls if len(c) >= 2
    )
