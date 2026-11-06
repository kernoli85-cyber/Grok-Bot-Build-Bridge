"""Read-only host prerequisite check (Windows-oriented; honest on Linux BOX).

Never installs, never starts stopped WSL distros, never asserts companion
version when the CLI is absent, never reads secrets.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any, Callable, Literal

from .discovery import parse_wsl_list_verbose
from .safe_exec import RunResult, run_argv

Runner = Callable[..., RunResult]

RuntimeKind = Literal["windows_host", "box_or_linux", "darwin", "other"]

_PY_VER = re.compile(r"Python\s+(\d+\.\d+\.\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class HostPrereqReport:
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def runtime_kind() -> RuntimeKind:
    plat = sys.platform
    if plat.startswith("win"):
        return "windows_host"
    if plat.startswith("linux"):
        return "box_or_linux"
    if plat == "darwin":
        return "darwin"
    return "other"


def _base_notes(kind: RuntimeKind) -> dict[str, str]:
    return {
        "runtime_note": (
            "windows_host: selected Grok Bot desktop / operator PC. "
            if kind == "windows_host"
            else "box_or_linux: typically the Grok Bot BOX — not the user's Windows device. "
            "Do not treat BOX discovery as Windows/WSL Build readiness. "
            if kind == "box_or_linux"
            else f"runtime={kind}. "
        )
        + "Repo checkouts stay on the Bot BOX; only approved wheel/script artifacts are copied to the host.",
        "authority_note": (
            "Same Grok Bot account and a registered machine do not authorize execution "
            "beyond the operator's approval for the specific request. If approval is denied: "
            "stop; do not reroute to another path or escalate privileges."
        ),
        "offline_note": (
            "If ListMachines reports connected=false for the selected desktop, local execution "
            "is honestly blocked until the Grok Bot desktop app is open and online."
        ),
        "routing_note": (
            "desired (native|wsl|both) is distinct from default_backend (native|wsl) used per job "
            "when both are ready; never silent fallback; never start stopped WSL."
        ),
        "webhook_note": (
            "Standalone webhook prototype remains a separate one-way notify path; "
            "do not conflate with local-exec job control."
        ),
    }


def _probe_python(runner: Runner) -> dict[str, Any]:
    for label, argv in (
        ("py-3", ("py", "-3", "--version")),
        ("python", ("python", "--version")),
        ("python3", ("python3", "--version")),
    ):
        r = runner(argv, timeout_sec=8)
        if r.error == "executable_not_found" or r.returncode != 0:
            continue
        text = (r.stdout or r.stderr or "").strip()
        m = _PY_VER.search(text)
        if not m:
            continue
        return {
            "present": True,
            "label": label,
            "version": m.group(1),
            "note": "interpreter_found_companion_not_asserted",
        }
    return {
        "present": False,
        "label": None,
        "version": None,
        "note": "no_python_probe_matched",
    }


def _probe_venv(runner: Runner, *, python_label: str | None) -> dict[str, Any]:
    if not python_label:
        return {"present": False, "note": "skipped_no_python"}
    # Map label to argv prefix
    if python_label == "py-3":
        prefix: tuple[str, ...] = ("py", "-3")
    elif python_label == "python":
        prefix = ("python",)
    else:
        prefix = ("python3",)
    r = runner(prefix + ("-c", "import venv; print('venv_ok')"), timeout_sec=8)
    if r.returncode == 0 and "venv_ok" in (r.stdout or ""):
        return {"present": True, "note": "stdlib_venv_import_ok"}
    return {"present": False, "note": "venv_import_failed_or_absent"}


def _probe_companion_on_path(runner: Runner) -> dict[str, Any]:
    """Presence only — never call `version` (first proof / absent package)."""
    for name in ("grok-bot-build-bridge", "grok-bot-build-bridge.exe"):
        # where/where.exe on Windows; command -v via sh not used (no shell).
        # Try Get-Command via powershell only on Windows; elsewhere which.
        if sys.platform.startswith("win"):
            ps = (
                f"$c = Get-Command {name} -ErrorAction SilentlyContinue; "
                "if ($null -eq $c) { Write-Output 'ABSENT' } "
                "else { Write-Output ('PRESENT:' + [string]$c.Source) }"
            )
            r = runner(
                (
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    ps,
                ),
                timeout_sec=10,
            )
        else:
            r = runner(("which", name), timeout_sec=5)
        if r.error == "executable_not_found":
            continue
        out = (r.stdout or "").strip()
        if out.startswith("PRESENT:") or (r.returncode == 0 and out and "ABSENT" not in out):
            return {
                "status": "present_on_path",
                "version": None,
                "note": "on_path_version_not_queried_in_prereq",
            }
        if "ABSENT" in out or r.returncode != 0:
            continue
    return {
        "status": "absent",
        "version": None,
        "note": "companion_wheel_not_required_for_this_check",
    }


def _probe_wsl(runner: Runner) -> dict[str, Any]:
    for exe in ("wsl.exe", "wsl"):
        r = runner((exe, "-l", "-v"), timeout_sec=12)
        if r.error == "executable_not_found":
            continue
        if r.timed_out:
            return {
                "exe_present": True,
                "distros": [],
                "stopped_present": False,
                "never_auto_start": True,
                "note": "wsl_list_timed_out",
            }
        rows = parse_wsl_list_verbose(r.stdout or "")
        distros = []
        stopped = False
        for row in rows:
            state = str(row.get("state") or "")
            if state.lower() == "stopped":
                stopped = True
            distros.append(
                {
                    "name": row.get("name"),
                    "state": state,
                    "version": row.get("version"),
                    "default": bool(row.get("default")),
                    "note": (
                        "stopped_ask_operator_before_start"
                        if state.lower() == "stopped"
                        else None
                    ),
                }
            )
        return {
            "exe_present": True,
            "distros": distros,
            "stopped_present": stopped,
            "never_auto_start": True,
            "note": "list_only_never_started_stopped",
        }
    return {
        "exe_present": False,
        "distros": [],
        "stopped_present": False,
        "never_auto_start": True,
        "note": "wsl_exe_absent",
    }


def collect_host_prereq(*, runner: Runner = run_argv) -> HostPrereqReport:
    kind = runtime_kind()
    notes = _base_notes(kind)
    python = _probe_python(runner)
    venv = _probe_venv(runner, python_label=python.get("label"))
    companion = _probe_companion_on_path(runner)
    wsl = _probe_wsl(runner)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "check_kind": "host_prereq",
        "runtime_kind": kind,
        **notes,
        "python": python,
        "venv": venv,
        "companion_cli": companion,
        "wsl": wsl,
        "secrets": "none_read_none_written",
        "next_operator_steps": [
            "If python.present is false: operator installs Python 3.11+; re-run check.",
            "If companion_cli.status is absent: after approval, install approved wheel into isolated venv (docs/LOCAL_EXEC.md).",
            "If WSL distros are Stopped: ask operator to start; never auto-start.",
            "When desired=both: also choose default_backend explicitly per routing rules.",
            "Webhook remains separate one-way notify; local exec is not a persistent watcher.",
        ],
    }
    return HostPrereqReport(payload=payload)


def fixture_host_prereq_windows(
    *,
    wsl_list_text: str,
    python_version_text: str = "Python 3.12.0",
    companion_present: bool = False,
    venv_ok: bool = True,
) -> HostPrereqReport:
    """Deterministic Windows-shaped report for CI (no live host mutation)."""
    notes = _base_notes("windows_host")
    m = _PY_VER.search(python_version_text)
    py_ver = m.group(1) if m else None
    rows = parse_wsl_list_verbose(wsl_list_text)
    distros = []
    stopped = False
    for row in rows:
        state = str(row.get("state") or "")
        if state.lower() == "stopped":
            stopped = True
        distros.append(
            {
                "name": row.get("name"),
                "state": state,
                "version": row.get("version"),
                "default": bool(row.get("default")),
                "note": (
                    "stopped_ask_operator_before_start"
                    if state.lower() == "stopped"
                    else None
                ),
            }
        )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "check_kind": "host_prereq_fixture",
        "runtime_kind": "windows_host",
        **notes,
        "python": {
            "present": bool(py_ver),
            "label": "fixture",
            "version": py_ver,
            "note": "fixture_interpreter",
        },
        "venv": {
            "present": venv_ok,
            "note": "fixture_venv",
        },
        "companion_cli": {
            "status": "present_on_path" if companion_present else "absent",
            "version": None,
            "note": (
                "fixture_companion_present_version_not_asserted"
                if companion_present
                else "fixture_companion_absent"
            ),
        },
        "wsl": {
            "exe_present": True,
            "distros": distros,
            "stopped_present": stopped,
            "never_auto_start": True,
            "note": "fixture_list_only",
        },
        "secrets": "none_read_none_written",
        "next_operator_steps": [
            "Fixture only — not evidence of the live selected Windows device.",
            "Host evidence requires approved local-exec prereq on the registered desktop.",
        ],
    }
    return HostPrereqReport(payload=payload)


def packaged_host_prereq_script_text() -> str:
    from importlib import resources

    base = resources.files("grok_bot_build_bridge.resources")
    return (base / "scripts" / "host-prereq-check.ps1").read_text(encoding="utf-8")
