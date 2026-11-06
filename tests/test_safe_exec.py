from __future__ import annotations

from pathlib import Path

from grok_bot_build_bridge.safe_exec import decode_cli_bytes, run_argv

FIX = Path(__file__).parent / "fixtures"


def test_decode_utf16_bom():
    data = (FIX / "wsl_list_utf16le.bin").read_bytes()
    text = decode_cli_bytes(data)
    assert "\x00" not in text
    assert "Ubuntu" in text
    assert "Running" in text


def test_decode_utf16_nobom():
    data = (FIX / "wsl_list_utf16le_nobom.bin").read_bytes()
    text = decode_cli_bytes(data)
    assert "Ubuntu" in text
    assert "Stopped" in text


def test_run_argv_timeout(monkeypatch):
    import subprocess
    from grok_bot_build_bridge import safe_exec

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd=["sleep"], timeout=0.1)

    monkeypatch.setattr(safe_exec.subprocess, "run", boom)
    r = run_argv(["sleep", "9"], timeout_sec=0.1)
    assert r.timed_out is True
    assert r.error == "timeout"


def test_run_argv_missing_exe():
    r = run_argv(["definitely-not-a-real-bridge-binary-zzz"], timeout_sec=2)
    assert r.error == "executable_not_found"


def test_run_argv_no_shell_echo():
    # Ensure we can run a real argv without shell
    r = run_argv(["python3", "-c", "print('ok')"], timeout_sec=5)
    assert r.returncode == 0
    assert "ok" in r.stdout
