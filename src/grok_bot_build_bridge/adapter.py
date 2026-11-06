"""Read-only host discovery adapter (Windows native + WSL).

Uses argv-only subprocesses with timeouts. Never starts stopped WSL distros,
never installs, never reads credential files, never auto-approves.
Device-provided names are treated as untrusted until is_safe_identity passes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .discovery import (
    is_safe_identity,
    nonroot_ready,
    parse_grok_version,
    parse_wsl_list_verbose,
)
from .models import BackendState, DiscoveryReport, NativeBackend, WslCandidate
from .safe_exec import RunResult, run_argv

# Fixed PowerShell lookup — no user-controlled interpolation into the script body.
# Discovers grok.exe / grok.cmd / grok.ps1 via Get-Command Source.
# Note: ApprovedExecutionPlan refuses .cmd/.bat (cmd.exe reinterpretation); prefer .exe/.ps1.
_NATIVE_LOOKUP_PS = (
    "$c = Get-Command grok -ErrorAction SilentlyContinue; "
    "if ($null -eq $c) { Write-Output 'NATIVE_GROK:NOT_FOUND'; exit 0 }; "
    "$src = [string]$c.Source; "
    "if (-not $src) { $src = [string]$c.Path }; "
    "Write-Output ('NATIVE_GROK:FOUND'); "
    "Write-Output ('NATIVE_GROK_SOURCE:' + $src); "
    "Write-Output ('NATIVE_GROK_COMMANDTYPE:' + [string]$c.CommandType)"
)

_NATIVE_PATH_OK = re.compile(
    r"(?i).+[\\/]grok(\.exe|\.cmd|\.ps1)?$"
)
_SOURCE_LINE = re.compile(r"^NATIVE_GROK_SOURCE:(?P<path>.+)$")
_PASSWD_LINE = re.compile(
    r"^(?P<user>[^:]+):[^:]*:(?P<uid>\d+):(?P<gid>\d+):(?P<gecos>[^:]*):(?P<home>[^:]*):(?P<shell>.*)$"
)


Runner = Callable[..., RunResult]


@dataclass(frozen=True)
class ProbeDiagnostics:
    """Non-secret structured diagnostics for a probe step."""

    step: str
    returncode: int | None = None
    timed_out: bool = False
    error: str | None = None
    detail: str | None = None  # short non-secret note only


def parse_native_lookup(stdout: str) -> tuple[str | None, str | None]:
    """Return (path, command_type) from our fixed PowerShell probe output."""
    path = None
    ctype = None
    found = False
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if line == "NATIVE_GROK:NOT_FOUND":
            return None, None
        if line == "NATIVE_GROK:FOUND":
            found = True
            continue
        m = _SOURCE_LINE.match(line)
        if m:
            path = m.group("path").strip().strip('"')
            continue
        if line.startswith("NATIVE_GROK_COMMANDTYPE:"):
            ctype = line.split(":", 1)[1].strip()
    if not found or not path:
        return None, ctype
    # Reject arbitrary command strings / unexpected shapes
    if " " in path and not (path.lower().endswith(".exe") or path.lower().endswith(".cmd") or path.lower().endswith(".ps1")):
        # Application paths may contain spaces (e.g. Program Files) — allow if ends with grok.*
        pass
    if not _NATIVE_PATH_OK.match(path) and not path.lower().rstrip().endswith("grok"):
        return None, ctype
    # Disallow shell metacharacters that should never appear in a filesystem path Source
    if any(ch in path for ch in ("|", ";", "&", ">", "<", "`", "\n", "\r")):
        return None, ctype
    return path, ctype


def parse_passwd_nonroot(text: str) -> list[tuple[str, int, str]]:
    """Parse getent/passwd lines → (user, uid, home) for uid>=1000, non-root."""
    out: list[tuple[str, int, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _PASSWD_LINE.match(line)
        if not m:
            continue
        user = m.group("user")
        uid = int(m.group("uid"))
        home = m.group("home")
        if user == "root" or uid < 1000:
            continue
        if user in ("nobody", "nfsnobody"):
            continue
        if not is_safe_identity(user):
            continue
        out.append((user, uid, home))
    return out


def discover_native(*, runner: Runner = run_argv) -> tuple[NativeBackend, tuple[ProbeDiagnostics, ...]]:
    """Read-only native grok lookup via PowerShell Get-Command (argv-only)."""
    diags: list[ProbeDiagnostics] = []
    # Prefer powershell.exe; fall back to pwsh — still argv-only fixed script.
    for exe in ("powershell.exe", "pwsh.exe", "powershell", "pwsh"):
        result = runner(
            [exe, "-NoProfile", "-NonInteractive", "-Command", _NATIVE_LOOKUP_PS],
            timeout_sec=15,
        )
        diags.append(
            ProbeDiagnostics(
                step=f"native_lookup:{exe}",
                returncode=result.returncode,
                timed_out=result.timed_out,
                error=result.error,
            )
        )
        if result.error == "executable_not_found":
            continue
        if result.timed_out:
            return NativeBackend(state=BackendState.NOT_READY), tuple(diags)
        if result.returncode != 0:
            # Nonzero without NOT_FOUND line → not ready (don't invent)
            continue
        path, _ctype = parse_native_lookup(result.stdout)
        if not path:
            # Explicit not found or rejected path
            if "NATIVE_GROK:NOT_FOUND" in result.stdout:
                return NativeBackend(state=BackendState.NOT_FOUND), tuple(diags)
            continue

        # Version probe: invoke resolved path with --version (argv only)
        ver_res = runner([path, "--version"], timeout_sec=10)
        diags.append(
            ProbeDiagnostics(
                step="native_version",
                returncode=ver_res.returncode,
                timed_out=ver_res.timed_out,
                error=ver_res.error,
            )
        )
        if ver_res.timed_out or ver_res.error:
            return (
                NativeBackend(state=BackendState.NOT_READY, path=path),
                tuple(diags),
            )
        ver = parse_grok_version(ver_res.stdout) or parse_grok_version(ver_res.stderr)
        if not ver:
            return (
                NativeBackend(state=BackendState.NOT_READY, path=path),
                tuple(diags),
            )
        return (
            NativeBackend(state=BackendState.READY, path=path, version=ver),
            tuple(diags),
        )

    return NativeBackend(state=BackendState.NOT_FOUND), tuple(diags)


def _wsl_exe_candidates() -> tuple[str, ...]:
    return ("wsl.exe", "wsl")


def discover_wsl(*, runner: Runner = run_argv) -> tuple[tuple[WslCandidate, ...], tuple[ProbeDiagnostics, ...]]:
    """Read-only WSL discovery. Never issues commands against Stopped distros."""
    diags: list[ProbeDiagnostics] = []
    list_res = None
    for exe in _wsl_exe_candidates():
        list_res = runner([exe, "-l", "-v"], timeout_sec=15)
        diags.append(
            ProbeDiagnostics(
                step=f"wsl_list:{exe}",
                returncode=list_res.returncode,
                timed_out=list_res.timed_out,
                error=list_res.error,
            )
        )
        if list_res.error == "executable_not_found":
            continue
        break
    if list_res is None or list_res.error == "executable_not_found":
        return (), tuple(diags)
    if list_res.timed_out:
        return (), tuple(diags)
    # wsl -l -v may return 0 even when empty; nonzero → unavailable
    if list_res.returncode not in (0,):
        diags.append(
            ProbeDiagnostics(
                step="wsl_list_status",
                returncode=list_res.returncode,
                detail="nonzero_list_exit",
            )
        )
        # Still try parse stdout; some builds write to stdout with weird codes

    rows = parse_wsl_list_verbose(list_res.stdout or list_res.stderr)
    if not rows:
        return (), tuple(diags)

    candidates: list[WslCandidate] = []
    wsl_bin = list_res.argv[0]

    for row in rows:
        distro = str(row["name"])
        state = str(row["state"])
        if not is_safe_identity(distro):
            candidates.append(
                WslCandidate(
                    distro=distro[:64],
                    user="",
                    uid=-1,
                    state=BackendState.NOT_READY,
                    notes=("unsafe_distro_name",),
                )
            )
            continue

        if state.lower() == "stopped":
            # CRITICAL: do not invoke wsl -d on stopped distros (would auto-start)
            candidates.append(
                WslCandidate(
                    distro=distro,
                    user="",
                    uid=-1,
                    state=BackendState.STOPPED,
                    notes=("distro_stopped_ask_before_start",),
                )
            )
            continue

        if state.lower() != "running":
            candidates.append(
                WslCandidate(
                    distro=distro,
                    user="",
                    uid=-1,
                    state=BackendState.NOT_READY,
                    notes=(f"distro_state_{state.lower()}",),
                )
            )
            continue

        # Running: enumerate nonroot users via getent (fixed argv; distro validated)
        passwd_res = runner(
            [wsl_bin, "-d", distro, "-u", "root", "--", "getent", "passwd"],
            timeout_sec=15,
        )
        diags.append(
            ProbeDiagnostics(
                step=f"wsl_passwd:{distro}",
                returncode=passwd_res.returncode,
                timed_out=passwd_res.timed_out,
                error=passwd_res.error,
            )
        )
        if passwd_res.timed_out or passwd_res.error or passwd_res.returncode != 0:
            candidates.append(
                WslCandidate(
                    distro=distro,
                    user="",
                    uid=-1,
                    state=BackendState.NOT_READY,
                    notes=("passwd_probe_failed",),
                )
            )
            continue

        users = parse_passwd_nonroot(passwd_res.stdout)
        if not users:
            candidates.append(
                WslCandidate(
                    distro=distro,
                    user="root",
                    uid=0,
                    state=BackendState.NOT_READY,
                    notes=("root_or_system_user_not_ready", "no_nonroot_users"),
                )
            )
            continue

        # Probe each nonroot user for grok (read-only); keep best READY else last status
        found_ready = False
        for user, uid, _home in users:
            grok_res = runner(
                [
                    wsl_bin,
                    "-d",
                    distro,
                    "-u",
                    user,
                    "--",
                    "bash",
                    "-lc",
                    "command -v grok; grok --version",
                ],
                timeout_sec=15,
            )
            diags.append(
                ProbeDiagnostics(
                    step=f"wsl_grok:{distro}:{user}",
                    returncode=grok_res.returncode,
                    timed_out=grok_res.timed_out,
                    error=grok_res.error,
                )
            )
            if grok_res.timed_out or grok_res.error:
                candidates.append(
                    nonroot_ready(distro, user, uid, "Running", None, None)
                )
                # nonroot_ready with None path → NOT_FOUND; override note for timeout
                if grok_res.timed_out:
                    candidates[-1] = WslCandidate(
                        distro=distro,
                        user=user,
                        uid=uid,
                        state=BackendState.NOT_READY,
                        notes=("probe_timeout",),
                    )
                continue

            # Parse command -v path from first line that looks like a path
            grok_path = None
            for line in grok_res.stdout.splitlines():
                s = line.strip()
                if s.startswith("/") and "grok" in s and " " not in s:
                    grok_path = s
                    break
            cand = nonroot_ready(
                distro, user, uid, "Running", grok_path, grok_res.stdout
            )
            candidates.append(cand)
            if cand.state == BackendState.READY:
                found_ready = True
                # Continue listing other users for transparency (no early silent skip)

        if not found_ready and not any(c.distro == distro for c in candidates):
            candidates.append(
                WslCandidate(
                    distro=distro,
                    user="",
                    uid=-1,
                    state=BackendState.NOT_FOUND,
                    notes=("grok_not_on_path",),
                )
            )

    return tuple(candidates), tuple(diags)


def discover_host(*, runner: Runner = run_argv) -> tuple[DiscoveryReport, tuple[ProbeDiagnostics, ...]]:
    """Full read-only discovery → DiscoveryReport + diagnostics (no secrets)."""
    native, d1 = discover_native(runner=runner)
    wsl, d2 = discover_wsl(runner=runner)

    ready_native = native.state == BackendState.READY
    ready_wsl = any(c.state == BackendState.READY for c in wsl)
    warnings: list[str] = []
    if any(c.state == BackendState.STOPPED for c in wsl):
        warnings.append("stopped_distro_present_not_started")
    if any("passwd_probe_failed" in c.notes for c in wsl):
        warnings.append("wsl_passwd_probe_failed")
    if native.state == BackendState.NOT_READY:
        warnings.append("native_not_ready")

    if ready_native and ready_wsl:
        kind = "both"
        default = None
    elif ready_native:
        kind = "native_only"
        default = "native"
    elif ready_wsl:
        kind = "wsl_only"
        default = "wsl"
    else:
        kind = "none"
        default = None

    report = DiscoveryReport(
        native=native,
        wsl=wsl,
        kind=kind,  # type: ignore[arg-type]
        default_backend=default,  # type: ignore[arg-type]
        warnings=tuple(warnings),
    )
    return report, d1 + d2
