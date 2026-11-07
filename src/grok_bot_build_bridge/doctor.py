"""Read-only companion doctor: one structured report from existing probes.

Never: starts WSL distros, calls the model, checks auth, reads secrets/credentials,
creates or migrates state, writes preferences, installs, or opens network connections.
Default mode is ``fixture`` (packaged demo data, NOT this machine). ``live`` probes the
process host -- on the Grok Bot box that is Linux, not the user's Windows PC.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .models import BackendState

STATUSES = ("ok", "not_ready", "absent", "unknown", "skipped")


def _check(cid: str, status: str, detail: str, **data: Any) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError("unknown doctor status")
    return {"id": cid, "status": status, "detail": detail, **data}


def _discovery_checks(report, diags) -> list[dict[str, Any]]:
    out = []
    n = report.native
    nmap = {BackendState.READY: "ok", BackendState.NOT_FOUND: "absent",
            BackendState.NOT_READY: "not_ready", BackendState.STOPPED: "not_ready"}
    timed = [d.step for d in diags if d.timed_out]
    out.append(_check("grok_native", nmap.get(n.state, "unknown"),
                      f"Native Windows grok: {n.state.value}" + (f", version {n.version}" if n.version else ""),
                      version=n.version))
    if not report.wsl:
        st = "unknown" if any("wsl" in s for s in timed) else "absent"
        out.append(_check("wsl_distros", st, "No WSL distros found" if st == "absent"
                          else "WSL listing timed out"))
        out.append(_check("wsl_grok", "skipped", "No WSL distro to check"))
    else:
        running = [c for c in report.wsl if c.state != BackendState.STOPPED]
        stopped = [c for c in report.wsl if c.state == BackendState.STOPPED]
        out.append(_check("wsl_distros", "ok" if running else "not_ready",
                          f"{len(running)} running, {len(stopped)} stopped (stopped distros are never started)",
                          distros=[{"distro": c.distro, "state": c.state.value} for c in report.wsl]))
        ready = [c for c in report.wsl if c.state == BackendState.READY]
        if ready:
            c = ready[0]
            out.append(_check("wsl_grok", "ok", f"grok found in {c.distro} for non-root user {c.user}",
                              distro=c.distro, user=c.user, grok_path=c.grok_path, version=c.version))
        elif running:
            notes = sorted({x for c in running for x in c.notes})
            st = "unknown" if "probe_timeout" in notes else "not_ready"
            out.append(_check("wsl_grok", st, "Running distro(s) but no usable non-root grok", notes=notes))
        else:
            out.append(_check("wsl_grok", "skipped", "All distros stopped; not started to check"))
    if timed:
        out.append(_check("probe_timeouts", "unknown", "Some probes timed out: " + ", ".join(timed)))
    return out


def _state_check(state_db: str | None) -> dict[str, Any]:
    if not state_db:
        return _check("state_db", "skipped", "No --state-db given")
    from .state.readonly import ReadOnlyState, ReadOnlyStateError
    try:
        with ReadOnlyState(state_db) as ro:
            n = ro.count_jobs()
            res = bool(ro.info and ro.info.has_results_table)
    except ReadOnlyStateError as exc:
        st = "absent" if exc.code == "state_db_absent" else "not_ready"
        return _check("state_db", st, exc.message, code=exc.code)
    return _check("state_db", "ok", f"Readable (read-only, main file not modified; WAL -shm may update); {n} jobs",
                  jobs=n, results_table=res)


def run_doctor(*, mode: str = "fixture", state_db: str | None = None,
               runner: Callable | None = None) -> dict[str, Any]:
    from .mcp_facade.scope import runtime_location
    if mode not in ("fixture", "live"):
        raise ValueError("mode must be fixture or live")
    loc = runtime_location()["runtime_location"]
    checks = [
        _check("companion", "ok", f"grok-bot-build-bridge {__version__} on Python {sys.version.split()[0]}",
               version=__version__),
        _check("mcp_extra", "ok" if importlib.util.find_spec("mcp") else "absent",
               "MCP SDK importable" if importlib.util.find_spec("mcp") else "MCP SDK not installed (optional)"),
    ]
    if mode == "fixture":
        from .mcp_facade.handlers import _fixture_report
        report, diags = _fixture_report(), ()
        scope_note = "FIXTURE: packaged demo facts, not this machine and not the user's PC."
    else:
        from .adapter import discover_host
        kw = {"runner": runner} if runner else {}
        report, diags = discover_host(**kw)
        scope_note = ("LIVE: read-only probes of the machine running this command"
                      + (" (Linux box/WSL guest - this cannot diagnose a Windows PC)."
                         if loc != "windows_host" else " (Windows host)."))
    checks += _discovery_checks(report, diags)
    checks.append(_state_check(state_db))
    checks.append(_check("model_auth", "unknown",
                         "Not checked: doctor never reads credentials or calls the model"))
    by = {c["id"]: c["status"] for c in checks}
    return {
        "mode": mode, "runtime_location": loc, "scope_note": scope_note,
        "checks": checks,
        "capabilities": {
            "ask_plannable_via_wsl": by.get("wsl_distros") == "ok" and by.get("wsl_grok") == "ok",
            "model_auth_verified": False,
            "model_call_ready": "unknown",
        },
        "never_done": ["start_wsl_distro", "model_call", "auth_check", "secret_read",
                       "state_create_or_migrate", "preferences_write", "install", "network"],
    }


def render_text(rep: dict[str, Any]) -> str:
    lines = [rep["scope_note"]]
    for c in rep["checks"]:
        lines.append(f"[{c['status']}] {c['id']}: {c['detail']}.")
    cap = rep["capabilities"]
    demo = " (demo data only)" if rep["mode"] == "fixture" else ""
    lines.append(("An ask could be planned via WSL" if cap["ask_plannable_via_wsl"]
                  else "An ask cannot be planned via WSL from these results") + demo + ".")
    lines.append("Model authentication was not verified, so readiness for a real model call is unknown.")
    return "\n".join(lines) + "\n"
