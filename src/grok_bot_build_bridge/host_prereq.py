"""Read-only host prerequisite check (Windows-oriented; honest on Linux BOX).

Never installs, never starts stopped WSL distros, never asserts companion
version when the CLI is absent, never reads secrets. Probes are argv-only
with timeouts via safe_exec.
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
_SYS_VER = re.compile(r"^(\d+\.\d+\.\d+)$")
_MIN = (3, 11)


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


def version_meets_min(version: str, *, minimum: tuple[int, int] = _MIN) -> bool:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", version or "")
    if not m:
        return False
    maj, minor = int(m.group(1)), int(m.group(2))
    if maj != minimum[0]:
        return maj > minimum[0]
    return minor >= minimum[1]


def is_windowsapps_alias_path(path: str | None) -> bool:
    if not path:
        return False
    norm = path.replace("/", "\\").lower()
    return "\\windowsapps\\" in norm


def parse_wsl_list_with_skips(text: str) -> dict[str, Any]:
    """Parse WSL list text; English states only. Unknown/localized lines → skipped."""
    rows = parse_wsl_list_verbose(text)
    # Re-scan for skipped (parity messaging for reports / tests)
    cleaned = (text or "").replace("\x00", "").replace("\ufeff", "")
    skipped: list[dict[str, str]] = []
    parsed_names = {str(r.get("name")) for r in rows}
    for raw in cleaned.splitlines():
        line = re.sub(r"\s+", " ", raw.strip())
        if not line:
            continue
        low = line.lower()
        if low.startswith("name") and "state" in low:
            continue
        if low == "name":
            continue
        # If parse_wsl_list_verbose accepted it, skip
        # Heuristic: line contains a safe name we already have
        matched = False
        for name in parsed_names:
            if re.search(rf"(^|\*\s*){re.escape(name)}\s+", line):
                matched = True
                break
        if matched:
            continue
        # Could be malformed / localized / unsafe name
        skipped.append(
            {
                "line": line[:120],
                "reason": "unparsed_localized_or_unsafe_or_malformed",
            }
        )
    stopped = any(str(r.get("state", "")).lower() == "stopped" for r in rows)
    distros = []
    for r in rows:
        state = str(r.get("state") or "")
        distros.append(
            {
                "name": r.get("name"),
                "state": state,
                "version": r.get("version"),
                "default": bool(r.get("default")),
                "note": (
                    "stopped_ask_operator_before_start"
                    if state.lower() == "stopped"
                    else None
                ),
                "readiness": (
                    "listed_running_not_probed"
                    if state.lower() == "running"
                    else "not_ready_from_list_alone"
                ),
            }
        )
    return {
        "distros": distros,
        "skipped_lines": skipped,
        "stopped_present": stopped,
    }


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
    """Find Python >=3.11; retain argv prefix; resolve sys.executable; skip WindowsApps."""
    candidates: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("py-3", ("py", "-3")),
        ("python", ("python",)),
        ("python3", ("python3",)),
    )
    skipped_aliases: list[str] = []
    for label, prefix in candidates:
        ver_r = runner(prefix + ("--version",), timeout_sec=8)
        if ver_r.error == "executable_not_found" or ver_r.timed_out:
            continue
        text = (ver_r.stdout or ver_r.stderr or "").strip()
        m = _PY_VER.search(text)
        if not m or not version_meets_min(m.group(1)):
            continue
        # Resolve real interpreter with SAME prefix (never drop -3 from py).
        res = runner(
            prefix
            + (
                "-c",
                "import sys; print(sys.version.split()[0]); print(sys.executable)",
            ),
            timeout_sec=8,
        )
        if res.error or res.timed_out or res.returncode != 0:
            continue
        lines = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        ver2, exe = lines[0], lines[1]
        if not _SYS_VER.match(ver2) or not version_meets_min(ver2):
            continue
        if is_windowsapps_alias_path(exe):
            skipped_aliases.append(exe)
            continue
        return {
            "present": True,
            "meets_min": True,
            "min_required": "3.11",
            "label": label,
            "version": ver2,
            "argv_prefix": list(prefix),
            "executable": exe,
            "note": "interpreter_ge_311_sys_executable_resolved",
            "windowsapps_aliases_skipped": skipped_aliases,
        }
    note = "no_python_311_plus_probe_matched"
    if skipped_aliases:
        note = "no_python_311_plus_probe_matched_windowsapps_aliases_skipped"
    return {
        "present": False,
        "meets_min": False,
        "min_required": "3.11",
        "label": None,
        "version": None,
        "argv_prefix": [],
        "executable": None,
        "note": note,
        "windowsapps_aliases_skipped": skipped_aliases,
    }


def _probe_venv(runner: Runner, *, executable: str | None) -> dict[str, Any]:
    if not executable:
        return {"present": False, "note": "skipped_no_python"}
    if is_windowsapps_alias_path(executable):
        return {"present": False, "note": "refused_windowsapps_alias"}
    # Call resolved executable directly (argv prefix already applied when resolving).
    r = runner((executable, "-c", "import venv; print('venv_ok')"), timeout_sec=8)
    if r.timed_out:
        return {"present": False, "note": "venv_probe_timeout"}
    if r.returncode == 0 and "venv_ok" in (r.stdout or ""):
        return {"present": True, "note": "stdlib_venv_import_ok"}
    return {"present": False, "note": "venv_import_failed_or_absent"}


def _probe_companion_on_path(runner: Runner) -> dict[str, Any]:
    """Presence only — never call `version`."""
    for name in ("grok-bot-build-bridge", "grok-bot-build-bridge.exe"):
        if sys.platform.startswith("win"):
            ps = (
                f"$c = Get-Command {name} -ErrorAction SilentlyContinue; "
                "if ($null -eq $c) { Write-Output 'ABSENT' } "
                "elseif (-not [string]$c.Source) { Write-Output 'UNKNOWN' } "
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
        if r.error == "executable_not_found" or r.timed_out:
            continue
        out = (r.stdout or "").strip()
        if "UNKNOWN" in out:
            return {
                "status": "unknown",
                "version": None,
                "note": "command_found_but_source_unresolved_version_not_queried",
            }
        if out.startswith("PRESENT:") or (r.returncode == 0 and out and "ABSENT" not in out):
            return {
                "status": "present_on_path",
                "version": None,
                "note": "on_path_version_not_queried_in_prereq",
            }
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
                "skipped_lines": [],
                "stopped_present": False,
                "never_auto_start": True,
                "note": "wsl_list_timed_out",
                "timed_out": True,
            }
        parsed = parse_wsl_list_with_skips(r.stdout or "")
        return {
            "exe_present": True,
            **parsed,
            "never_auto_start": True,
            "note": "list_only_never_started_stopped",
        }
    return {
        "exe_present": False,
        "distros": [],
        "skipped_lines": [],
        "stopped_present": False,
        "never_auto_start": True,
        "note": "wsl_exe_absent",
    }


def collect_host_prereq(*, runner: Runner = run_argv) -> HostPrereqReport:
    kind = runtime_kind()
    notes = _base_notes(kind)
    python = _probe_python(runner)
    venv = _probe_venv(runner, executable=python.get("executable"))
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
            "If python.present is false or meets_min is false: operator installs Python 3.11+; avoid WindowsApps aliases; re-run check.",
            "If companion_cli.status is absent: after approval, install approved wheel into isolated venv (docs/LOCAL_EXEC.md).",
            "If WSL distros are Stopped: ask operator to start; never auto-start. Skipped lines are not ready.",
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
    companion_status: str | None = None,
    venv_ok: bool = True,
) -> HostPrereqReport:
    """Deterministic Windows-shaped report for CI (no live host mutation)."""
    notes = _base_notes("windows_host")
    m = _PY_VER.search(python_version_text)
    py_ver = m.group(1) if m else None
    meets = bool(py_ver) and version_meets_min(py_ver)
    parsed = parse_wsl_list_with_skips(wsl_list_text)
    status = companion_status or ("present_on_path" if companion_present else "absent")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "check_kind": "host_prereq_fixture",
        "runtime_kind": "windows_host",
        **notes,
        "python": {
            "present": meets,
            "meets_min": meets,
            "min_required": "3.11",
            "label": "fixture",
            "version": py_ver if meets else None,
            "argv_prefix": ["fixture"],
            "executable": r"C:\Python312\python.exe" if meets else None,
            "note": "fixture_interpreter",
            "windowsapps_aliases_skipped": [],
        },
        "venv": {"present": venv_ok and meets, "note": "fixture_venv"},
        "companion_cli": {
            "status": status,
            "version": None,
            "note": (
                "fixture_companion_present_version_not_asserted"
                if status == "present_on_path"
                else (
                    "fixture_companion_unknown"
                    if status == "unknown"
                    else "fixture_companion_absent"
                )
            ),
        },
        "wsl": {
            "exe_present": True,
            **parsed,
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
