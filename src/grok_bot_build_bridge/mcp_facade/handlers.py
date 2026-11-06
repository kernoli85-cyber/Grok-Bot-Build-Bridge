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
from ..state.store import JobStore
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
    info["tools"] = [
        "bridge_runtime_info",
        "bridge_discover",
        "bridge_onboard",
        "bridge_status",
        "bridge_job_status",
    ]
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
    cand = nonroot_ready(
        distro="Ubuntu",
        user="okern",
        uid=1000,
        distro_state="Running",
        grok_path="/home/okern/.local/bin/grok",
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


def _open_existing_store() -> JobStore | dict[str, Any]:
    """Open scoped DB if present. Does not create a missing database file."""
    try:
        db = scoped_db_path()
    except ScopeError as exc:
        return _err(exc.code, exc.message)

    if not db.is_file():
        return _err(
            "state_db_absent",
            "scoped state.sqlite does not exist; MCP will not create it",
            scoped_db=str(db),
            runtime=runtime_location(),
        )
    # Existing file only — JobStore may ensure schema tables on open.
    return JobStore(db)


def handle_status() -> dict[str, Any]:
    store_or_err = _open_existing_store()
    if isinstance(store_or_err, dict):
        return store_or_err
    store = store_or_err
    jobs = store.list_jobs()
    events = store.list_outbox()
    return _ok(
        {
            "scoped_db": str(store.db_path),
            "runtime": runtime_location(),
            "jobs": [
                {
                    "task_id": j.task_id,
                    "correlation_id": j.correlation_id,
                    "status": j.status.value,
                    "attempt": j.attempt,
                    "backend": j.backend,
                    "last_error_code": j.last_error_code,
                }
                for j in jobs
            ],
            "outbox": [
                {
                    "event_id": e.event_id,
                    "task_id": e.task_id,
                    "event_type": e.event_type,
                    "delivery": e.delivery.value,
                    "attempt": e.attempt,
                    "schema_version": e.schema_version,
                    # deliberately omit payload / last_error text (may hold operator content)
                }
                for e in events
            ],
            "note": (
                "Status metadata only; prompts, transcripts, credentials, and outbox "
                "payloads are not returned."
            ),
        }
    )


def handle_job_status(*, task_id: str) -> dict[str, Any]:
    try:
        tid = validate_task_id(task_id)
    except ScopeError as exc:
        return _err(exc.code, exc.message)

    store_or_err = _open_existing_store()
    if isinstance(store_or_err, dict):
        return store_or_err
    store = store_or_err
    job = store.get_job(tid)
    if job is None:
        return _err(
            "job_not_found",
            f"no job with task_id={tid}",
            scoped_db=str(store.db_path),
            runtime=runtime_location(),
        )
    related = [
        {
            "event_id": e.event_id,
            "event_type": e.event_type,
            "delivery": e.delivery.value,
            "attempt": e.attempt,
            "schema_version": e.schema_version,
        }
        for e in store.list_outbox()
        if e.task_id == tid
    ]
    return _ok(
        {
            "scoped_db": str(store.db_path),
            "runtime": runtime_location(),
            "job": {
                "task_id": job.task_id,
                "correlation_id": job.correlation_id,
                "status": job.status.value,
                "attempt": job.attempt,
                "max_attempts": job.max_attempts,
                "backend": job.backend,
                "cwd": job.cwd,
                "last_error_code": job.last_error_code,
                "lease_owner": job.lease_owner,
                # omit lease timing detail beyond presence
            },
            "outbox_for_task": related,
            "note": (
                "Status metadata only; prompts, transcripts, credentials, and outbox "
                "payloads are not returned."
            ),
        }
    )


def db_exists_without_create() -> bool:
    """Test helper: check scoped DB existence without opening/creating."""
    try:
        return scoped_db_path().is_file()
    except ScopeError:
        return False
