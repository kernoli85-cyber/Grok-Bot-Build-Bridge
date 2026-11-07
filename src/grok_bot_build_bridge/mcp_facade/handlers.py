"""Read-only tool handlers (no MCP wire). Strict inputs; redacted outputs."""

from __future__ import annotations

from typing import Any

from .. import __version__
from ..adapter import discover_host
from ..bot_onboard_adapter import _redact_mapping
from ..discovery import classify_discovery, nonroot_ready
from ..models import DiscoveryReport
from ..onboarding import onboarding_plan_to_dict, plan_onboarding, probe_pairing_metadata
from ..resources import fixture_text
from .scope import (
    ScopeError,
    runtime_location,
    scoped_db_path,
    validate_desired,
    validate_mode,
    validate_task_id,
)



def _report_to_dict(report: DiscoveryReport) -> dict[str, Any]:
    return {
        "kind": report.kind,
        "default_backend": report.default_backend,
        "warnings": list(report.warnings),
        "native": {
            "state": report.native.state.value,
            "path": report.native.path,
            "version": report.native.version,
        },
        "wsl": [
            {
                "distro": c.distro,
                "user": c.user,
                "uid": c.uid,
                "state": c.state.value,
                "grok_path": c.grok_path,
                "version": c.version,
                "notes": list(c.notes),
            }
            for c in report.wsl
        ],
        "ready": sorted(report.ready_backends()),
        "onboarding": {
            "none": "Offer Windows / WSL2 / both links only - do not auto-install",
            "native_only": "Default native; WSL optional later",
            "wsl_only": "Default wsl; Native optional later",
            "both": "Ask default; per-job backend; never parallel-start",
        }.get(report.kind),
    }

def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = _redact_mapping(payload)
    if not isinstance(cleaned, dict):
        return {"error": {"code": "redact_failed", "message": "non-object after redact"}}
    return cleaned


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    body.update(extra)
    return _ok(body)


def handle_runtime_info() -> dict[str, Any]:
    info = runtime_location()
    info["package_version"] = __version__
    from .tools import enabled_tool_names
    info["tools"] = list(enabled_tool_names())
    info["not_exposed"] = [
        "shell",
        "write",
        "execute",
        "enqueue",
        "outbox_send",
        "secret_read",
        "cancel",
    ]
    return _ok(info)


def _fixture_report():
    wsl_list = fixture_text("wsl_list_v_running.txt")
    from ..demo_facts import DEMO_GROK, DEMO_USER

    cand = nonroot_ready(
        distro="Ubuntu",
        user=DEMO_USER,
        uid=1000,
        distro_state="Running",
        grok_path=DEMO_GROK,
        version_text="grok 1.0.70 (f50904eaee6c) [stable]",
    )
    return classify_discovery(
        native_path=None,
        native_version_text=None,
        wsl_list_text=wsl_list,
        wsl_candidates=(cand,),
    )


def handle_discover(*, mode: str = "live") -> dict[str, Any]:
    try:
        m = validate_mode(mode)
    except ScopeError as exc:
        return _err(exc.code, exc.message)

    loc = runtime_location()
    if m == "fixture":
        report = _fixture_report()
        payload = _report_to_dict(report)
        payload["mode"] = "fixture"
        payload["diagnostics"] = []
        payload["runtime"] = loc
        payload["host_note"] = (
            "Fixture demo facts only; no host mutation. Not evidence of Windows/WSL on this runtime."
        )
        return _ok(payload)

    report, diags = discover_host()
    payload = _report_to_dict(report)
    payload["mode"] = "live"
    payload["diagnostics"] = [
        {
            "step": d.step,
            "returncode": d.returncode,
            "timed_out": d.timed_out,
            "error": d.error,
            "detail": d.detail,
        }
        for d in diags
    ]
    payload["runtime"] = loc
    payload["host_note"] = (
        "Live probes are read-only; stopped WSL distros are not started; "
        "no grok jobs executed. On box/linux, Windows/WSL tools are typically absent "
        "(discovery kind often 'none')."
    )
    return _ok(payload)


def handle_onboard(*, desired: str = "unset", mode: str = "fixture") -> dict[str, Any]:
    try:
        d = validate_desired(desired)
        m = validate_mode(mode)
    except ScopeError as exc:
        return _err(exc.code, exc.message)

    loc = runtime_location()
    if m == "live":
        report, diags = discover_host()
        probe_notes = []
        for diag in diags:
            if diag.timed_out:
                probe_notes.append(f"probe_timeout:{diag.step}")
            elif diag.error and diag.error != "executable_not_found":
                probe_notes.append(f"probe_error:{diag.step}:{diag.error}")
            elif diag.error == "executable_not_found":
                probe_notes.append(f"probe_absent_tool:{diag.step}")
    else:
        report = _fixture_report()
        probe_notes = ["fixture_demo_no_host_mutation"]

    # Pairing metadata only (existence); never reads secret file contents.
    pairing = probe_pairing_metadata(None)
    plan = plan_onboarding(
        report,
        desired=d,
        pairing=pairing,
        discovery_probe_notes=probe_notes,
        dry_run=True,
    )
    payload = onboarding_plan_to_dict(plan)
    payload["mode"] = m
    payload["runtime"] = loc
    payload["cli_note"] = (
        "dry_run_default; MCP onboard is diagnostic only — no installs, prefs writes, "
        "or secret reads"
    )
    return _ok(payload)


RESULTS_ENV = "GROK_BOT_BUILD_BRIDGE_MCP_RESULTS"
RESULTS_COST_ENV = "GROK_BOT_BUILD_BRIDGE_MCP_RESULTS_COST"
_NOTE = ("Status metadata only; prompts, replies, session ids, cwd paths, credentials and "
         "outbox payloads are not returned. Read-only: the DB is never created or migrated.")


def env_flag(name: str) -> bool:
    """Startup-only opt-in; strict values. Read once when the server is built."""
    import os
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def _ro_open():
    from ..state.readonly import ReadOnlyState, ReadOnlyStateError
    try:
        db = scoped_db_path()
    except ScopeError as exc:
        return _err(exc.code, exc.message)
    ro = ReadOnlyState(db)
    try:
        ro.open()
    except ReadOnlyStateError as exc:
        return _err(exc.code, exc.message, runtime=runtime_location())
    return ro


def _guard(fn):
    from ..state.readonly import ReadOnlyStateError

    def wrapped(*a, **k):
        try:
            return fn(*a, **k)
        except ReadOnlyStateError as exc:
            return _err(exc.code, exc.message)
    return wrapped


@_guard
def handle_status() -> dict[str, Any]:
    ro = _ro_open()
    if isinstance(ro, dict):
        return ro
    try:
        jobs = ro.list_jobs()
        total = ro.count_jobs()
        events = ro.outbox_for(None)
    finally:
        ro.close()
    return _ok({
        "runtime": runtime_location(), "state_db_name": "state.sqlite",
        "total_jobs": total, "jobs_shown": len(jobs),
        "jobs": [{k: j[k] for k in ("task_id", "correlation_id", "status", "attempt",
                                     "backend", "last_error_code")} for j in jobs],
        "outbox": events, "note": _NOTE,
    })


@_guard
def handle_job_status(*, task_id: str) -> dict[str, Any]:
    try:
        tid = validate_task_id(task_id)
    except ScopeError as exc:
        return _err(exc.code, exc.message)
    ro = _ro_open()
    if isinstance(ro, dict):
        return ro
    try:
        job = ro.get_job(tid)
        events = ro.outbox_for(tid) if job else []
    finally:
        ro.close()
    if job is None:
        return _err("job_not_found", "no job with that task_id", runtime=runtime_location())
    return _ok({"runtime": runtime_location(), "job": job,
                "outbox_for_task": [{k: e[k] for k in e if k != "task_id"} for e in events],
                "note": _NOTE})


_RESULT_KEYS = ("schema", "schema_variant", "parse_status", "model_ok", "stop_reason",
                "is_error", "num_turns", "usage", "cost_status", "expected_match")


@_guard
def handle_job_result(*, task_id: str, include_cost: bool) -> dict[str, Any]:
    """Immutable result metadata. Never session id, text, hash, length, prompt or paths.
    include_cost is fixed at server startup, not by the caller."""
    try:
        tid = validate_task_id(task_id)
    except ScopeError as exc:
        return _err(exc.code, exc.message)
    ro = _ro_open()
    if isinstance(ro, dict):
        return ro
    try:
        job = ro.get_job(tid)
        r = ro.get_result(tid) if job else None
    finally:
        ro.close()
    if job is None:
        return _err("job_not_found", "no job with that task_id")
    if r is None:
        return _ok({"task_id": tid, "status": job["status"], "result_metadata": None,
                    "note": "no validated result summary stored for this job"})
    meta = {k: r[k] for k in _RESULT_KEYS}
    if include_cost:
        meta["total_cost_usd"] = r["total_cost_usd"]  # exact decimal string or null
        meta["total_cost_usd_ticks"] = r["total_cost_usd_ticks"]
    return _ok({"task_id": tid, "status": job["status"], "result_metadata": meta,
                "cost_included": include_cost,
                "note": "metadata only; absent cost means unknown, not zero"})


def db_exists_without_create() -> bool:
    """Test helper: check scoped DB existence without opening/creating."""
    try:
        return scoped_db_path().is_file()
    except ScopeError:
        return False
