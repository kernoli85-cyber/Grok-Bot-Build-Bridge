from __future__ import annotations

from pathlib import Path

from grok_bot_build_bridge.adapter import (
    discover_host,
    discover_native,
    discover_wsl,
    parse_native_lookup,
    parse_passwd_nonroot,
)
from grok_bot_build_bridge.models import BackendState
from grok_bot_build_bridge.safe_exec import RunResult, decode_cli_bytes

FIX = Path(__file__).parent / "fixtures"


def _res(argv, code=0, out="", err="", timed_out=False, error=None):
    return RunResult(tuple(argv), code, out, err, timed_out=timed_out, error=error)


def test_parse_native_found_with_spaces():
    path, ctype = parse_native_lookup((FIX / "native_found.txt").read_text())
    assert path.endswith("grok.exe")
    assert "Program Files" in path
    assert ctype == "Application"


def test_parse_native_not_found():
    path, _ = parse_native_lookup((FIX / "native_not_found.txt").read_text())
    assert path is None


def test_parse_native_rejects_metachar_source():
    path, _ = parse_native_lookup((FIX / "native_bad_source.txt").read_text())
    assert path is None


def test_parse_passwd_skips_root_nobody_unsafe():
    users = parse_passwd_nonroot((FIX / "passwd_sample.txt").read_text())
    assert users == [("okern", 1000, "/home/okern")]


def test_native_only_matrix():
    calls = []

    def runner(argv, timeout_sec=15, **_k):
        calls.append(list(argv))
        if argv[0] in {"powershell.exe", "pwsh.exe", "powershell", "pwsh"}:
            return _res(argv, 0, (FIX / "native_found.txt").read_text())
        if str(argv[0]).lower().endswith("grok.exe"):
            return _res(argv, 0, "grok 1.0.70\n")
        if argv[0] in {"wsl.exe", "wsl"}:
            return _res(argv, error="executable_not_found")
        return _res(argv, error="executable_not_found")

    report, diags = discover_host(runner=runner)
    assert report.kind == "native_only"
    assert report.native.state == BackendState.READY
    assert report.native.version == "1.0.70"
    assert report.default_backend == "native"
    assert diags


def test_wsl_only_running_nonroot():
    def runner(argv, timeout_sec=15, **_k):
        a0 = argv[0]
        if a0 in {"powershell.exe", "pwsh.exe", "powershell", "pwsh"}:
            return _res(argv, error="executable_not_found")
        if a0 in {"wsl.exe", "wsl"} and argv[1:3] == ["-l", "-v"]:
            return _res(argv, 0, "  NAME      STATE           VERSION\n* Ubuntu    Running         2\n")
        if a0 in {"wsl.exe", "wsl"} and "getent" in argv:
            return _res(argv, 0, (FIX / "passwd_sample.txt").read_text())
        if a0 in {"wsl.exe", "wsl"} and "bash" in argv:
            return _res(
                argv,
                0,
                "/home/okern/.local/bin/grok\ngrok 1.0.70 (deadbeef) [stable]\n",
            )
        return _res(argv, error="executable_not_found")

    report, _ = discover_host(runner=runner)
    assert report.kind == "wsl_only"
    assert report.default_backend == "wsl"
    ready = [c for c in report.wsl if c.state == BackendState.READY]
    assert len(ready) == 1
    assert ready[0].user == "okern"
    assert ready[0].version == "1.0.70"


def test_stopped_distro_not_probed():
    probed_d = []

    def runner(argv, timeout_sec=15, **_k):
        if argv[0] in {"powershell.exe", "pwsh", "powershell", "pwsh.exe"}:
            return _res(argv, error="executable_not_found")
        if argv[0] in {"wsl.exe", "wsl"} and argv[1:3] == ["-l", "-v"]:
            return _res(argv, 0, "  NAME      STATE           VERSION\n* Ubuntu    Stopped         2\n")
        if argv[0] in {"wsl.exe", "wsl"} and "-d" in argv:
            probed_d.append(list(argv))
            return _res(argv, 0, "")
        return _res(argv, error="executable_not_found")

    report, _ = discover_host(runner=runner)
    assert report.kind == "none"
    assert report.wsl[0].state == BackendState.STOPPED
    assert probed_d == []  # must not start stopped distro


def test_both_backends():
    def runner(argv, timeout_sec=15, **_k):
        if argv[0] in {"powershell.exe", "pwsh.exe", "powershell", "pwsh"}:
            return _res(argv, 0, (FIX / "native_found.txt").read_text())
        if str(argv[0]).lower().endswith("grok.exe"):
            return _res(argv, 0, "grok 1.0.70\n")
        if argv[0] in {"wsl.exe", "wsl"} and argv[1:3] == ["-l", "-v"]:
            return _res(argv, 0, "  NAME      STATE           VERSION\n* Ubuntu    Running         2\n")
        if "getent" in argv:
            return _res(argv, 0, (FIX / "passwd_sample.txt").read_text())
        if "bash" in argv:
            return _res(argv, 0, "/home/okern/.local/bin/grok\ngrok 1.0.70\n")
        return _res(argv, error="executable_not_found")

    report, _ = discover_host(runner=runner)
    assert report.kind == "both"
    assert report.default_backend is None


def test_none_when_tools_missing():
    def runner(argv, timeout_sec=15, **_k):
        return _res(argv, error="executable_not_found")

    report, diags = discover_host(runner=runner)
    assert report.kind == "none"
    assert report.native.state == BackendState.NOT_FOUND
    assert diags


def test_wsl_list_timeout():
    def runner(argv, timeout_sec=15, **_k):
        if argv[0] in {"powershell.exe", "pwsh.exe", "powershell", "pwsh"}:
            return _res(argv, error="executable_not_found")
        if argv[1:3] == ["-l", "-v"]:
            return _res(argv, timed_out=True, error="timeout")
        return _res(argv, error="executable_not_found")

    cands, diags = discover_wsl(runner=runner)
    assert cands == ()
    assert any(d.timed_out for d in diags)


def test_wsl_list_nonzero_malformed():
    def runner(argv, timeout_sec=15, **_k):
        if argv[0] in {"powershell.exe", "pwsh.exe", "powershell", "pwsh"}:
            return _res(argv, error="executable_not_found")
        if argv[1:3] == ["-l", "-v"]:
            return _res(argv, 1, out="garbage\xffnot a list")
        return _res(argv, error="executable_not_found")

    report, _ = discover_host(runner=runner)
    assert report.kind == "none"


def test_utf16_wsl_list_via_decode_path():
    text = decode_cli_bytes((FIX / "wsl_list_utf16le.bin").read_bytes())

    def runner(argv, timeout_sec=15, **_k):
        if argv[0] in {"powershell.exe", "pwsh.exe", "powershell", "pwsh"}:
            return _res(argv, error="executable_not_found")
        if argv[1:3] == ["-l", "-v"]:
            return _res(argv, 0, out=text)
        if "getent" in argv:
            return _res(argv, 0, (FIX / "passwd_sample.txt").read_text())
        if "bash" in argv:
            return _res(argv, 0, "/home/okern/.local/bin/grok\ngrok 1.0.70\n")
        return _res(argv, error="executable_not_found")

    report, _ = discover_host(runner=runner)
    assert report.kind == "wsl_only"


def test_root_only_not_ready():
    def runner(argv, timeout_sec=15, **_k):
        if argv[0] in {"powershell.exe", "pwsh.exe", "powershell", "pwsh"}:
            return _res(argv, error="executable_not_found")
        if argv[1:3] == ["-l", "-v"]:
            return _res(argv, 0, "  NAME      STATE           VERSION\n* Ubuntu    Running         2\n")
        if "getent" in argv:
            return _res(argv, 0, "root:x:0:0:root:/root:/bin/bash\n")
        return _res(argv, error="executable_not_found")

    report, _ = discover_host(runner=runner)
    assert report.kind == "none"
    assert report.wsl[0].state == BackendState.NOT_READY
    assert "no_nonroot_users" in report.wsl[0].notes


def test_path_with_spaces_version_invocation():
    seen = []

    def runner(argv, timeout_sec=15, **_k):
        seen.append(list(argv))
        if argv[0] in {"powershell.exe", "pwsh.exe", "powershell", "pwsh"}:
            return _res(argv, 0, (FIX / "native_found.txt").read_text())
        if "Program Files" in str(argv[0]):
            return _res(argv, 0, "grok 1.2.3\n")
        return _res(argv, error="executable_not_found")

    native, _ = discover_native(runner=runner)
    assert native.state == BackendState.READY
    assert native.version == "1.2.3"
    # version argv must be path token, not shell string
    assert any("Program Files" in a[0] and a[1] == "--version" for a in seen if a)
