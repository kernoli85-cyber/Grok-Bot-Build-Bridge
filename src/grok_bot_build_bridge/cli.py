"""CLI skeleton: guided discovery presentation - no installers, no side effects."""
from __future__ import annotations

import argparse
from pathlib import Path
import json
import sys
from typing import Sequence

from . import __version__
from .discovery import classify_discovery, nonroot_ready
from .models import DiscoveryReport
from .adapter import discover_host
from .state import DeliveryState, JobStatus, JobStore
from .state.store import StoreError
from .transport.pump import pump_outbox
from .transport.webhook import WebhookConfig, WebhookTransport, load_webhook_config
from .runner import build_approved_plan, execute_one_shot
from .process_control import capability_matrix
from .bot_onboard_adapter import (
    PlanContractError,
    build_conversation_view,
    conversation_view_to_dict,
    parse_onboarding_plan_payload,
    validate_operator_choices,
)
from .preferences import (
    OperatorPreferences,
    PreferenceError,
    default_preferences_path,
    load_preferences,
    save_preferences,
)
from .onboarding import (
    onboarding_plan_to_dict,
    plan_onboarding,
    probe_pairing_metadata,
)
from .runner.plan import PlanError


def _report_to_dict(report: DiscoveryReport) -> dict:
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


def cmd_discover_demo(_args: argparse.Namespace) -> int:
    """Deterministic demo using packaged fixtures - no host probing."""
    from .resources import fixture_text

    wsl_list = fixture_text("wsl_list_v_running.txt")
    # Simulate measured host: native absent, WSL okern ready
    cand = nonroot_ready(
        distro="Ubuntu",
        user="okern",
        uid=1000,
        distro_state="Running",
        grok_path="/home/okern/.local/bin/grok",
        version_text="grok 1.0.70 (f50904eaee6c) [stable]",
    )
    report = classify_discovery(
        native_path=None,
        native_version_text=None,
        wsl_list_text=wsl_list,
        wsl_candidates=(cand,),
    )
    json.dump(_report_to_dict(report), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    """Live read-only host discovery (argv-only probes). Graceful if tools absent."""
    report, diags = discover_host()
    payload = _report_to_dict(report)
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
    payload["host_note"] = (
        "Live probes are read-only; stopped WSL distros are not started; "
        "no grok jobs executed; Linux hosts typically report native/wsl tools missing."
    )
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_host_prereq(args: argparse.Namespace) -> int:
    """Read-only host prereq JSON. Never installs; never starts WSL; never asserts absent package version."""
    from .host_prereq import collect_host_prereq, fixture_host_prereq_windows
    from .resources import fixture_text

    if getattr(args, "fixture", None) == "windows-running":
        report = fixture_host_prereq_windows(
            wsl_list_text=fixture_text("wsl_list_v_running.txt"),
            companion_present=False,
        )
    elif getattr(args, "fixture", None) == "windows-stopped":
        report = fixture_host_prereq_windows(
            wsl_list_text=fixture_text("wsl_list_v_stopped.txt"),
            companion_present=False,
        )
    else:
        report = collect_host_prereq()
    json.dump(report.to_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_host_prereq_script(_args: argparse.Namespace) -> int:
    """Print packaged Windows host-prereq-check.ps1 (no secrets)."""
    from .host_prereq import packaged_host_prereq_script_text

    text = packaged_host_prereq_script_text()
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    return 0



def cmd_version(_args: argparse.Namespace) -> int:
    print(__version__)
    return 0


def cmd_plan_demo(_args: argparse.Namespace) -> int:
    """Deterministic job-plan demo - validates routing contract, never shells out."""
    from .models import JobRequest, RoutingError
    from .routing import plan_job

    cand = nonroot_ready(
        distro="Ubuntu",
        user="okern",
        uid=1000,
        distro_state="Running",
        grok_path="/home/okern/.local/bin/grok",
        version_text="grok 1.0.70 (f50904eaee6c) [stable]",
    )
    report = classify_discovery(
        native_path=None,
        native_version_text=None,
        wsl_candidates=(cand,),
    )
    plan = plan_job(
        report,
        JobRequest(
            task_id="demo-task",
            prompt="explain API in ./src (read-only)",
            cwd="/home/okern/proj",
            max_turns=3,
            deny_rules=("Bash(rm *)",),
        ),
    )
    if isinstance(plan, RoutingError):
        json.dump({"error": {"code": plan.code, "message": plan.message}}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 1
    json.dump(
        {
            "task_id": plan.task_id,
            "backend": plan.backend,
            "cwd": plan.cwd,
            "argv": list(plan.argv),
            "env_keys": sorted(plan.env.keys()),
            "note": "argv is a plan only; this CLI does not execute grok",
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


def _store_from_args(args: argparse.Namespace) -> JobStore:
    path = getattr(args, "state_db", None)
    if path:
        return JobStore(path)
    return JobStore.open_default()


def cmd_jobs_demo(args: argparse.Namespace) -> int:
    """Simulated job lifecycle against a state DB - no grok, no webhook POST."""
    store = _store_from_args(args)
    job = store.enqueue_job(
        task_id=args.task_id,
        correlation_id=args.correlation_id or args.task_id,
        backend=args.backend,
        cwd=args.cwd,
        max_attempts=args.max_attempts,
    )
    claimed = store.claim_job(job.task_id, owner="demo-worker")
    running = store.mark_running(job.task_id, owner="demo-worker")
    if args.fail:
        final = store.mark_failed(job.task_id, error_code="demo_fail", retry=True)
    else:
        final = store.mark_succeeded(job.task_id)
    tick = store.simulate_outbox_tick(transport_ok=True, bot_process=False)
    payload = {
        "simulated": True,
        "note": "No grok execution and no webhook POST; outbox acks are fake.",
        "state_db": str(store.db_path),
        "job": {
            "task_id": final.task_id,
            "correlation_id": final.correlation_id,
            "status": final.status.value,
            "attempt": final.attempt,
        },
        "outbox_tick": tick,
        "http_accepted_vs_bot_processed": (
            "http_accepted means transport ack only; bot_processed is separate"
        ),
    }
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


_ERROR_TEXT = {
    "timeout_uncertain": "the run timed out; the process was killed on the host but cleanup inside WSL is not proven",
    "success_exit_invalid_result": "the process exited 0 but its output was not a valid Grok JSON result",
    "model_result_not_ok": "Grok returned a result that does not report a normal finish",
    "smoke_text_mismatch": "the smoke reply was not exactly BRIDGE_OK",
    "result_persist_failed": "the result could not be saved, so success is not claimed",
    "lease_lost_during_run": "the worker lost its lease while running",
}


def _job_sentence(j: dict) -> str:
    s = (f"Job {j['task_id']} is {j['status']} (attempt {j['attempt']} of {j['max_attempts']}, "
         f"backend {j['backend']}).")
    code = j.get("last_error_code")
    if code:
        s += f" Reason: {_ERROR_TEXT.get(code, code)}."
    if j["status"] in {"needs_review", "failed"}:
        s += " It will not be retried automatically."
    return s


def _result_sentence(r: dict | None) -> str:
    if r is None:
        return "No validated result summary is stored for this job."
    if r["parse_status"] != "ok":
        return f"The output could not be accepted as a Grok result ({r['parse_status']})."
    parts = [f"Grok output parsed; stop reason {r['stop_reason'] or 'unknown'}"
             f"{', marked as error' if r['is_error'] else ''}."]
    if r["num_turns"] is not None:
        parts.append(f"Turns used: {r['num_turns']}.")
    if r["cost_status"] == "reported":
        parts.append(f"Cost reported by Grok: US$ {r['total_cost_usd']}.")
    else:
        parts.append(f"Cost is unknown ({r['cost_status']}); absent cost is not zero.")
    if r["expected_match"] is not None:
        parts.append("The expected smoke text matched exactly." if r["expected_match"]
                     else "The expected smoke text did not match.")
    return " ".join(parts)


def cmd_repo_plan(args: argparse.Namespace) -> int:
    from .repo_plan import plan_repo_question, render_text
    plan = plan_repo_question(args.root, list(args.select or []), reveal_paths=args.reveal_paths)
    if args.format == "text":
        sys.stdout.write(render_text(plan))
    else:
        json.dump(plan, sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 0 if plan["status"] in {"planned", "partial"} else 2


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import render_text, run_doctor
    rep = run_doctor(mode="live" if args.live else "fixture", state_db=args.state_db)
    if args.format == "text":
        sys.stdout.write(render_text(rep))
    else:
        json.dump(rep, sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 0


def cmd_jobs_status(args: argparse.Namespace) -> int:
    """Strictly read-only (never creates or migrates the DB)."""
    from .mcp_facade.scope import ScopeError, validate_task_id
    from .state.readonly import ReadOnlyState, ReadOnlyStateError

    def _err(code: str, msg: str, rc: int) -> int:
        if args.format == "text":
            sys.stdout.write(f"Error ({code}): {msg}\n")
        else:
            json.dump({"error": {"code": code, "message": msg}}, sys.stdout, indent=2)
            sys.stdout.write("\n")
        return rc

    args.limit = max(1, min(int(args.limit), 500))
    args.offset = max(0, int(args.offset))
    tid = None
    if args.task_id is not None:
        try:
            tid = validate_task_id(args.task_id)
        except ScopeError as exc:
            return _err(exc.code, exc.message, 2)
    show = bool(args.show_result)
    try:
        with ReadOnlyState(args.state_db, assume_static_snapshot=args.assume_static_snapshot) as ro:
            if tid is not None:
                job = ro.get_job(tid)
                if job is None:
                    return _err("job_not_found", "no job with that task_id in this state DB", 3)
                jobs, total = [job], 1
                events = ro.outbox_for(tid, limit=args.limit)
            else:
                jobs = ro.list_jobs(limit=args.limit, offset=args.offset)
                total = ro.count_jobs()
                events = ro.outbox_for(None, limit=args.limit)
            if show:
                for j in jobs:
                    j["result_summary"] = ro.get_result(j["task_id"])
            mode = ro.info.access_mode if ro.info else None
    except ReadOnlyStateError as exc:
        return _err(exc.code, exc.message, 2)
    if args.format == "text":
        lines = [_job_sentence(j) + ((" " + _result_sentence(j.get("result_summary"))) if show else "")
                 for j in jobs]
        if tid is None:
            lines.append(f"Showing {len(jobs)} of {total} jobs (offset {args.offset}).")
        if not jobs:
            lines.append("There are no jobs in this state DB.")
        sys.stdout.write("\n".join(lines) + "\n")
        return 0
    json.dump({
        "state_db": str(args.state_db), "read_only": True, "access_mode": mode,
        "task_id_filter": tid, "total_jobs": total, "limit": args.limit,
        "offset": 0 if tid else args.offset, "jobs": jobs, "outbox": events,
        "note": "Status metadata only; prompts/transcripts are not stored by default.",
    }, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_outbox_tick(args: argparse.Namespace) -> int:
    """One-shot simulated outbox pass - not a daemon, not real HTTP."""
    store = _store_from_args(args)
    results = store.simulate_outbox_tick(
        transport_ok=not args.fail_transport,
        bot_process=args.bot_process,
        limit=args.limit,
    )
    json.dump(
        {
            "simulated": True,
            "state_db": str(store.db_path),
            "results": results,
            "note": "Fake acknowledgments only; does not POST webhooks.",
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0



def cmd_outbox_pump(args: argparse.Namespace) -> int:
    """Dry-run (default) or explicit --send webhook pump - no daemon."""
    store = _store_from_args(args)
    transport = None
    if args.config:
        transport = WebhookTransport(load_webhook_config(Path(args.config)))
    elif args.url and args.token:
        transport = WebhookTransport(
            WebhookConfig(url=args.url, bearer_token=args.token)
        )
    dry_run = not args.send
    if args.send and transport is None:
        print(json.dumps({"error": "config_or_url_token_required_for_send"}), flush=True)
        return 2
    try:
        results = pump_outbox(
            store,
            transport,
            dry_run=dry_run,
            limit=args.limit,
        )
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
        return 2
    json.dump(
        {
            "dry_run": dry_run,
            "state_db": str(store.db_path),
            "results": results,
            "note": (
                "dry_run_no_network"
                if dry_run
                else "send_mode_http_accepted_is_not_bot_processed"
            ),
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0



def cmd_runner_run(args: argparse.Namespace) -> int:
    """One-shot runner. Dry-run by default; --execute required to spawn a process."""
    store = _store_from_args(args)
    if args.wsl_probe:
        return _cmd_runner_wsl_probe(args, store)
    if args.wsl_grok_smoke:
        return _cmd_runner_wsl_grok_smoke(args, store)
    if not args.executable:
        json.dump({"error": {"code": "executable_missing", "message": "--executable required (or --wsl-probe)"}}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 2
    try:
        plan = build_approved_plan(
            task_id=args.task_id,
            backend=args.backend,
            cwd=args.cwd,
            executable=args.executable,
            max_turns=args.max_turns,
            permission_mode=args.permission_mode,
            timeout_sec=args.timeout,
            prompt=args.prompt,
            prompt_via_stdin=not args.force_prompt_argv,
            force_prompt_argv=args.force_prompt_argv,
            extra_args=tuple(args.extra or ()),
            deny_rules=tuple(args.deny or ()),
        )
    except PlanError as exc:
        json.dump({"error": {"code": exc.code, "message": exc.message}}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 2
    if args.parse_grok_json or args.expect_bridge_ok or args.show_answer:
        from dataclasses import replace as _r
        from .runner.plan import GROK_SMOKE_EXPECTED
        plan = _r(plan, parse_grok_json=True, return_answer=bool(args.show_answer),
                  expected_text=GROK_SMOKE_EXPECTED if args.expect_bridge_ok else None)
    try:
        outcome = execute_one_shot(
            store,
            plan,
            owner=args.owner,
            dry_run=not args.execute,
            lease_sec=args.lease_sec,
            heartbeat_interval_sec=args.heartbeat_sec,
        )
    except StoreError as exc:
        json.dump({"error": {"code": exc.code, "message": exc.message}}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 2
    json.dump(
        {
            "dry_run": outcome.dry_run,
            "task_id": outcome.task_id,
            "status": outcome.status,
            "returncode": outcome.returncode,
            "timed_out": outcome.timed_out,
            "error_code": outcome.error_code,
            "stdout_truncated": outcome.stdout_truncated,
            "stderr_truncated": outcome.stderr_truncated,
            "note": outcome.note,
            "argv_preview": list(outcome.argv_preview),
            "plan_backend": outcome.plan_backend,
            "execution_transport": outcome.execution_transport,
            "execution_kind": outcome.execution_kind,
            "transport_note": _transport_note(outcome.plan_backend, outcome.execution_transport),
            "heartbeats": outcome.heartbeats,
            "lease_lost": outcome.lease_lost,
            "process_exit_ok": outcome.returncode == 0 if outcome.returncode is not None else None,
            "result_summary": outcome.result_summary,
            **({"answer": outcome.answer_text, "answer_truncated": outcome.answer_truncated,
                "answer_note": "transient display only; not stored"} if args.show_answer else {}),
            "state_db": str(store.db_path),
            "cancel_note": "cancellation is state-only; process tree kill not claimed",
            "cancel_capability": {
                "platform": capability_matrix().platform,
                "production_cancel": capability_matrix().production_cancel,
                "process_tree_kill": capability_matrix().process_tree_kill,
                "wsl_guest_tree": capability_matrix().wsl_guest_tree,
                "win_job_object_demo": capability_matrix().win_job_object_demo,
            },
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    if plan.parse_grok_json:  # result-checked runs: nonzero unless accepted
        return 0 if outcome.status in {"dry_run", "succeeded"} else 1
    return 0


def _transport_note(plan_backend: str | None, transport: str | None) -> str:
    if transport == "wsl_exe":
        return "executed_inside_wsl_via_wsl_exe"
    if plan_backend == "wsl":
        return "plan_backend_wsl_but_spawned_on_host_NOT_wsl_execution"
    return "spawned_on_host"


def _read_wsl_list_text() -> tuple[str | None, str | None]:
    from .safe_exec import run_argv

    r = run_argv(("wsl.exe", "-l", "-v"), timeout_sec=10)
    if r.error or r.timed_out:
        return None, r.error or "timeout"
    return (r.stdout or "") + "\n" + (r.stderr or ""), None


def _cmd_runner_wsl_probe(args: argparse.Namespace, store: JobStore) -> int:
    """Fixed wsl.exe printf probe: proves WSL transport + job/outbox, NOT Grok."""
    from .runner.plan import build_wsl_probe_plan, require_wsl_distro_running

    try:
        plan = build_wsl_probe_plan(
            task_id=args.task_id,
            distro=args.wsl_distro or "",
            user=args.wsl_user or "",
            cwd=args.cwd,
            timeout_sec=min(float(args.timeout), 120.0),
        )
    except PlanError as exc:
        json.dump({"error": {"code": exc.code, "message": exc.message}}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 2
    precheck = "skipped_dry_run"
    if args.execute:
        text, err = _wsl_list_reader()
        if err is not None:
            json.dump({"error": {"code": "wsl_list_failed", "message": err}}, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 2
        try:
            require_wsl_distro_running(plan.wsl_distro or "", text or "")
        except PlanError as exc:
            json.dump({"error": {"code": exc.code, "message": exc.message}}, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 2
        precheck = "distro_running_confirmed"
    try:
        outcome = execute_one_shot(store, plan, owner=args.owner, dry_run=not args.execute)
    except StoreError as exc:
        json.dump({"error": {"code": exc.code, "message": exc.message}}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 2
    json.dump(
        {
            "dry_run": outcome.dry_run,
            "task_id": outcome.task_id,
            "status": outcome.status,
            "returncode": outcome.returncode,
            "timed_out": outcome.timed_out,
            "error_code": outcome.error_code,
            "argv_preview": list(outcome.argv_preview),
            "plan_backend": outcome.plan_backend,
            "execution_transport": outcome.execution_transport,
            "execution_kind": outcome.execution_kind,
            "transport_note": _transport_note(outcome.plan_backend, outcome.execution_transport),
            "wsl_precheck": precheck,
            "grok_executed": False,
            "note": "wsl_printf_probe proves wsl.exe transport + job/outbox only; NOT a Grok run",
            "state_db": str(store.db_path),
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0 if outcome.status in {"dry_run", "succeeded"} else 1


def _cmd_runner_wsl_grok_smoke(args: argparse.Namespace, store: JobStore) -> int:
    """Fixed Grok one-shot via wsl.exe. Dry-run default; real call needs
    --execute AND --allow-model-call (default false) AND a Running distro."""
    from .runner.plan import (build_wsl_grok_smoke_plan, redact_argv_preview,
                              require_wsl_distro_running)

    def _err(code: str, msg: str) -> int:
        json.dump({"error": {"code": code, "message": msg}}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 2

    try:
        plan = build_wsl_grok_smoke_plan(
            task_id=args.task_id, distro=args.wsl_distro or "", user=args.wsl_user or "",
            grok_path=args.executable or "", cwd=args.cwd,
            timeout_sec=min(float(args.timeout), 120.0),
        )
    except PlanError as exc:
        return _err(exc.code, exc.message)
    limits = {
        "max_turns": 1, "max_turns_is_cost_cap": False, "cost_cap_flag": None,
        "cost": "unknown_until_run; read result total_cost_usd/usage (docs) afterwards",
        "tool_denial": "builtin denylist + bare deny rules incl MCPTool + dontAsk (docs semantics, unverified on 1.0.70)",
        "not_isolated": "user ~/.grok config, native MCP servers (may start), home rules/skills still load",
        "sandbox": "strict profile per docs; unverified on 1.0.70; grok refuses start if policy unavailable (docs)",
        "timeout": "kills host wsl.exe; guest cleanup not proven",
    }
    if args.execute and not args.allow_model_call:
        return _err("model_call_gated", "real Grok call requires --allow-model-call (explicit owner approval)")
    precheck = "skipped_dry_run"
    executed = False
    outcome = None
    if args.execute:
        text, err = _wsl_list_reader()
        if err is not None:
            return _err("wsl_list_failed", err)
        try:
            require_wsl_distro_running(plan.wsl_distro or "", text or "")
        except PlanError as exc:
            return _err(exc.code, exc.message)
        precheck = "distro_running_confirmed"
        try:
            outcome = execute_one_shot(store, plan, owner=args.owner, dry_run=False)
        except StoreError as exc:
            return _err(exc.code, exc.message)
        executed = True
    json.dump({
        "dry_run": not executed, "task_id": plan.task_id,
        "argv_preview": list(redact_argv_preview(plan.argv)),
        "plan_backend": plan.backend, "execution_transport": plan.execution_transport,
        "execution_kind": plan.execution_kind, "grok_executed": executed,
        "wsl_precheck": precheck,
        "status": outcome.status if outcome else "dry_run",
        "returncode": outcome.returncode if outcome else None,
        "timed_out": outcome.timed_out if outcome else False,
        "error_code": outcome.error_code if outcome else None,
        "process_exit_ok": (outcome.returncode == 0) if outcome else None,
        "result_summary": outcome.result_summary if outcome else None,
        "result_note": "result_summary is allowlisted metadata; response text never stored except exact BRIDGE_OK match",
        "limits": limits,
    }, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if (outcome is None or outcome.status == "succeeded") else 1


# Indirection for tests (monkeypatch) - never starts a distro.
_wsl_list_reader = _read_wsl_list_text


def _run_fixed_wsl(argv: tuple[str, ...]) -> tuple[int | None, str]:
    from .safe_exec import run_argv
    r = run_argv(argv, timeout_sec=30.0, max_stdout_bytes=4096, max_stderr_bytes=4096)
    return (None if (r.timed_out or r.error) else r.returncode), (r.stdout or "")


# Indirection for tests (monkeypatch); only ever receives the fixed mktemp/find argv.
_wsl_cmd_runner = _run_fixed_wsl

ASK_DRYRUN_CWD = "/tmp/gbbb-ask-CREATEDONEXECUTE"


def _read_ask_prompt(args: argparse.Namespace) -> str:
    from .runner.plan import ASK_MAX_PROMPT_BYTES, validate_ask_prompt
    sources = [x for x in (args.prompt is not None, bool(args.prompt_file), args.prompt_stdin) if x]
    if len(sources) != 1:
        raise PlanError("prompt_source", "give exactly one of --prompt, --prompt-file, --prompt-stdin")
    if args.prompt is not None:
        return validate_ask_prompt(args.prompt)
    if args.prompt_file:
        with open(args.prompt_file, "rb") as fh:
            data = fh.read(ASK_MAX_PROMPT_BYTES + 1)
    else:
        data = sys.stdin.buffer.read(ASK_MAX_PROMPT_BYTES + 1)
    return validate_ask_prompt(data)


def cmd_ask(args: argparse.Namespace) -> int:
    """Text-only single prompt to Grok via wsl.exe. Dry-run default: no DB, no temp
    dir, no host call, prompt not stored. Real call: --execute AND --allow-model-call."""
    import uuid

    from .runner.plan import (ASK_MAX_PROMPT_BYTES, build_wsl_grok_ask_plan,
                              build_wsl_mktemp_ask_cwd_argv, build_wsl_verify_ask_cwd_argv,
                              redact_argv_preview, require_wsl_distro_running)

    def _err(code: str, msg: str) -> int:
        json.dump({"error": {"code": code, "message": msg}}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 2

    task_id = args.task_id or f"ask-{uuid.uuid4().hex[:16]}"
    try:
        prompt = _read_ask_prompt(args)
        plan = build_wsl_grok_ask_plan(
            task_id=task_id, distro=args.wsl_distro or "", user=args.wsl_user or "",
            grok_path=args.executable or "", cwd=args.cwd or ASK_DRYRUN_CWD,
            prompt=prompt, max_turns=args.max_turns, timeout_sec=args.timeout,
        )
    except PlanError as exc:
        return _err(exc.code, exc.message)
    except OSError as exc:
        return _err("prompt_file_unreadable", type(exc).__name__)
    if args.execute and not args.allow_model_call:
        return _err("model_call_gated", "real Grok call requires --allow-model-call")
    info = {
        "mode": "text_only_single_prompt", "coding_agent": False,
        "prompt_bytes": len(prompt.encode("utf-8")), "prompt_max_bytes": ASK_MAX_PROMPT_BYTES,
        "prompt_stored": False,
        "prompt_transport": "argv value of --single (grok headless does not read stdin as prompt)",
        "prompt_process_visibility": "VISIBLE in OS process listings (wsl.exe command line, /proc cmdline) while running; preview redaction only hides it from bridge output",
        "max_turns": plan.max_turns, "max_turns_is_cost_cap": False, "cost_cap_flag": None,
        "timeout_sec": plan.timeout_sec,
        "timeout": "kills host wsl.exe; guest cleanup not proven -> needs_review, no retry",
        "tools": "builtin tools denied + deny rules incl MCPTool + dontAsk + strict sandbox (docs semantics, unverified on 1.0.70)",
        "not_isolated": "user ~/.grok config and native MCP servers may still load; trust state of the fresh cwd unknown",
        "approval": "--allow-model-call is a technical gate only; it does not replace the owner's explicit permission",
    }
    if not args.execute and args.format == "text":
        sys.stdout.write(
            f"Dry run only: nothing was sent to Grok and nothing was written. "
            f"The prompt has {info['prompt_bytes']} bytes and would run in a fresh /tmp/gbbb-ask-* "
            f"directory inside WSL distro {plan.wsl_distro} as {plan.wsl_user}, with at most "
            f"{plan.max_turns} turn(s) and a {plan.timeout_sec:g} s timeout. "
            "A real call needs --execute --allow-model-call and the owner's permission. "
            "While it runs, the prompt is visible in the process list.\n")
        return 0
    if not args.execute:
        json.dump({"dry_run": True, "task_id": task_id, "grok_executed": False,
                   "state_written": False, "temp_dir_created": False,
                   "cwd_preview": args.cwd or ASK_DRYRUN_CWD + " (placeholder; mktemp on execute)",
                   "argv_preview": list(redact_argv_preview(plan.argv)), "status": "dry_run",
                   "limits": info}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    text, err = _wsl_list_reader()
    if err is not None:
        return _err("wsl_list_failed", err)
    try:
        require_wsl_distro_running(plan.wsl_distro or "", text or "")
    except PlanError as exc:
        return _err(exc.code, exc.message)
    cwd = args.cwd
    if not cwd:
        rc, out = _wsl_cmd_runner(build_wsl_mktemp_ask_cwd_argv(plan.wsl_distro, plan.wsl_user))
        cwd = out.strip()
        if rc != 0:
            return _err("ask_cwd_create_failed", "mktemp via wsl.exe failed")
    try:
        verify = build_wsl_verify_ask_cwd_argv(plan.wsl_distro, plan.wsl_user, cwd)
    except PlanError as exc:
        return _err(exc.code, exc.message)
    rc, out = _wsl_cmd_runner(verify)
    if rc != 0 or out.strip() != cwd:
        return _err("ask_cwd_unsafe", "cwd must be an existing empty real dir owned by the user, not group/world-writable, not a symlink")
    from dataclasses import replace as _r
    plan = build_wsl_grok_ask_plan(
        task_id=task_id, distro=plan.wsl_distro, user=plan.wsl_user, grok_path=plan.executable,
        cwd=cwd, prompt=prompt, max_turns=plan.max_turns, timeout_sec=plan.timeout_sec)
    plan = _r(plan, return_answer=bool(args.show_answer))
    store = _store_from_args(args)
    try:
        outcome = execute_one_shot(store, plan, owner=args.owner, dry_run=False)
    except StoreError as exc:
        return _err(exc.code, exc.message)
    payload = {
        "dry_run": False, "task_id": task_id, "grok_executed": True, "cwd": cwd,
        "argv_preview": list(redact_argv_preview(plan.argv)),
        "execution_transport": outcome.execution_transport,
        "status": outcome.status, "returncode": outcome.returncode,
        "process_exit_ok": outcome.returncode == 0 if outcome.returncode is not None else None,
        "timed_out": outcome.timed_out, "error_code": outcome.error_code, "note": outcome.note,
        "result_summary": outcome.result_summary, "limits": info,
    }
    if args.show_answer:
        payload["answer"] = outcome.answer_text
        payload["answer_truncated"] = outcome.answer_truncated
        payload["answer_note"] = "transient display only; not stored in state/outbox/MCP"
    if args.format == "text":
        st = outcome.status
        lines = [f"Job {task_id}: {st}."]
        if outcome.error_code:
            lines.append(f"Reason: {_ERROR_TEXT.get(outcome.error_code, outcome.error_code)}.")
        lines.append(_result_sentence(outcome.result_summary))
        if st != "succeeded":
            lines.append("Nothing is retried automatically.")
        if args.show_answer:
            if outcome.answer_text is not None:
                lines.append("Answer (not stored):")
                lines.append(outcome.answer_text + (" [cut at display limit]" if outcome.answer_truncated else ""))
            else:
                lines.append("No answer can be shown for this run.")
        sys.stdout.write("\n".join(lines) + "\n")
    else:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
    return 0 if outcome.status == "succeeded" else 1


def cmd_jobs_retry(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    try:
        job = store.explicit_retry(args.task_id)
    except StoreError as exc:
        json.dump({"error": {"code": exc.code, "message": exc.message}}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 2
    json.dump(
        {
            "task_id": job.task_id,
            "status": job.status.value,
            "attempt": job.attempt,
            "note": "explicit_retry_only; notification outbox separate",
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0



def cmd_jobs_cancel(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    try:
        job = store.cancel(args.task_id)
    except StoreError as exc:
        json.dump({"error": {"code": exc.code, "message": exc.message}}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 2
    caps = capability_matrix()
    json.dump(
        {
            "task_id": job.task_id,
            "status": job.status.value,
            "note": "state_only_no_process_kill",
            "cancel_capability": {
                "platform": caps.platform,
                "production_cancel": caps.production_cancel,
                "process_tree_kill": caps.process_tree_kill,
                "wsl_guest_tree": caps.wsl_guest_tree,
                "win_job_object_demo": caps.win_job_object_demo,
            },
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


def cmd_onboard(args: argparse.Namespace) -> int:
    """Read-only onboarding plan. Dry-run always; never writes secrets or installs."""
    if getattr(args, "apply", False):
        json.dump(
            {
                "error": {
                    "code": "apply_not_supported",
                    "message": "onboard is read-only; no --apply writes in this slice",
                }
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 2

    if args.live:
        report, diags = discover_host()
        probe_notes = []
        for d in diags:
            if d.timed_out:
                probe_notes.append(f"probe_timeout:{d.step}")
            elif d.error and d.error != "executable_not_found":
                probe_notes.append(f"probe_error:{d.step}:{d.error}")
            elif d.error == "executable_not_found":
                probe_notes.append(f"probe_absent_tool:{d.step}")
    else:
        # Deterministic packaged fixtures - same facts as discover-demo
        from .discovery import classify_discovery, nonroot_ready
        from .resources import fixture_text

        wsl_list = fixture_text("wsl_list_v_running.txt")
        cand = nonroot_ready(
            distro="Ubuntu",
            user="okern",
            uid=1000,
            distro_state="Running",
            grok_path="/home/okern/.local/bin/grok",
            version_text="grok 1.0.70 (f50904eaee6c) [stable]",
        )
        report = classify_discovery(
            native_path=None,
            native_version_text=None,
            wsl_list_text=wsl_list,
            wsl_candidates=(cand,),
        )
        probe_notes = ["fixture_demo_no_host_mutation"]

    pairing = probe_pairing_metadata(
        Path(args.pairing_config) if args.pairing_config else None
    )
    plan = plan_onboarding(
        report,
        desired=args.desired,
        pairing=pairing,
        discovery_probe_notes=probe_notes,
        dry_run=True,
    )
    payload = onboarding_plan_to_dict(plan)
    payload["cli_note"] = (
        "dry_run_default; Bot extension guided setup is product UX; "
        "this CLI is diagnostic companion only"
    )
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0



def cmd_onboard_view(args: argparse.Namespace) -> int:
    """Parse onboard plan JSON -> conversation view (no writes)."""
    raw = Path(args.plan_json).read_bytes() if args.plan_json else sys.stdin.buffer.read()
    try:
        plan = parse_onboarding_plan_payload(raw)
        saved = None
        saved_default = None
        if args.prefs:
            try:
                prefs = load_preferences(Path(args.prefs))
            except PreferenceError as exc:
                json.dump({"error": {"code": exc.code, "message": exc.message}}, sys.stdout, indent=2)
                sys.stdout.write("\n")
                return 2
            if prefs:
                saved = prefs.desired
                saved_default = prefs.default_backend
        view = build_conversation_view(plan, saved_desired=saved, saved_default=saved_default)
    except PlanContractError as exc:
        json.dump({"error": {"code": exc.code, "message": exc.message}}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 2
    json.dump(conversation_view_to_dict(view), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_prefs_show(args: argparse.Namespace) -> int:
    path = Path(args.path) if args.path else default_preferences_path()
    try:
        prefs = load_preferences(path)
    except PreferenceError as exc:
        json.dump({"error": {"code": exc.code, "message": exc.message}}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 2
    json.dump(
        {
            "path": str(path),
            "exists": prefs is not None,
            "preferences": None
            if prefs is None
            else {
                "schema_version": prefs.schema_version,
                "desired": prefs.desired,
                "default_backend": prefs.default_backend,
                "discovery_kind_at_save": prefs.discovery_kind_at_save,
                "notes": list(prefs.notes),
            },
            "note": "non-secret allowlisted fields only; no auth-ready claim",
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


def cmd_prefs_save(args: argparse.Namespace) -> int:
    """Explicit preference save - never called by read-only onboard."""
    path = Path(args.path) if args.path else default_preferences_path()
    try:
        choices = validate_operator_choices(
            desired=args.desired,
            default_backend=args.default_backend,
            discovery_kind=args.discovery_kind,
        )
        prefs = OperatorPreferences(
            schema_version=1,
            desired=choices.desired,  # type: ignore[arg-type]
            default_backend=choices.default_backend,  # type: ignore[arg-type]
            discovery_kind_at_save=choices.discovery_kind_at_choice,
            notes=choices.notes,
        )
        meta = save_preferences(prefs, path, overwrite=bool(args.overwrite))
    except (PlanContractError, PreferenceError) as exc:
        json.dump({"error": {"code": exc.code, "message": exc.message}}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 2
    json.dump({"saved": meta, "preferences": {
        "desired": prefs.desired,
        "default_backend": prefs.default_backend,
        "discovery_kind_at_save": prefs.discovery_kind_at_save,
        "notes": list(prefs.notes),
    }}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0



def cmd_skill_draft(_args: argparse.Namespace) -> int:
    """Print packaged draft skill text (not globally installed)."""
    from .resources import draft_skill_path_hint, draft_skill_text

    payload = {
        "note": "repo/package draft only - not marketplace-installed; parent owns UpdateSkill",
        "repo_path_hint": draft_skill_path_hint(),
        "text": draft_skill_text(),
    }
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0



def cmd_mcp_http_check(_args: argparse.Namespace) -> int:
    """Validate streamable-HTTP MCP config; never binds a listener."""
    try:
        from .mcp_facade.http_config import check_http_config
    except ImportError as exc:
        json.dump(
            {"ok": False, "error": {"code": "import_error", "message": str(exc)}, "listener": False},
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 2
    payload = check_http_config()
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if payload.get("ok") else 2



def cmd_mcp_stdio(_args: argparse.Namespace) -> int:
    """Run bounded read-only MCP server over stdio (official SDK). No bind."""
    try:
        from .mcp_facade.server import run_stdio
    except ImportError as exc:
        json.dump(
            {
                "error": {
                    "code": "mcp_extra_missing",
                    "message": (
                        "Install optional extra: pip install 'grok-bot-build-bridge[mcp]' "
                        f"({exc})"
                    ),
                }
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 2
    run_stdio()
    return 0



def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="grok-bot-build-bridge",
        description=(
            "Grok Bot-Build Bridge companion CLI (scaffold). "
            "Discovery is read-only; this binary does not install Grok Build or WSL."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version", help="Print package version").set_defaults(func=cmd_version)
    p_hp = sub.add_parser(
        "host-prereq",
        help=(
            "Read-only host prerequisite JSON (Python/venv/WSL list); "
            "never installs; never starts stopped WSL; does not assert companion version"
        ),
    )
    p_hp.add_argument(
        "--fixture",
        choices=["windows-running", "windows-stopped"],
        default=None,
        help="Deterministic Windows-shaped fixture (CI / demos); not live host evidence",
    )
    p_hp.set_defaults(func=cmd_host_prereq)
    sub.add_parser(
        "host-prereq-script",
        help="Print packaged host-prereq-check.ps1 for approved copy to Windows desktop",
    ).set_defaults(func=cmd_host_prereq_script)

    sub.add_parser(
        "skill-draft",
        help="Print packaged draft Einrichtung skill (not globally installed)",
    ).set_defaults(func=cmd_skill_draft)
    sub.add_parser(
        "discover",
        help="Live read-only Windows/WSL discovery (no installs, no grok jobs)",
    ).set_defaults(func=cmd_discover)
    sub.add_parser(
        "discover-demo",
        help="Print fixture-based discovery JSON (no host changes)",
    ).set_defaults(func=cmd_discover_demo)
    sub.add_parser(
        "plan-demo",
        help="Print fixture-based job plan JSON (no grok execution)",
    ).set_defaults(func=cmd_plan_demo)

    p_on = sub.add_parser(
        "onboard",
        help="Read-only onboarding plan JSON (dry-run; no installs/secrets/network)",
    )
    p_on.add_argument(
        "--desired",
        choices=["unset", "native", "wsl", "both"],
        default="unset",
        help="Explicit operator preference (default unset -> needs_choice)",
    )
    p_on.add_argument(
        "--live",
        action="store_true",
        help="Use live read-only discover_host (default: fixture demo facts)",
    )
    p_on.add_argument(
        "--pairing-config",
        default=None,
        help="Path to webhook.json for existence check only (never read)",
    )
    p_on.add_argument(
        "--apply",
        action="store_true",
        help="Rejected: onboard cannot apply/write in this slice",
    )
    p_on.set_defaults(func=cmd_onboard)

    p_ov = sub.add_parser(
        "onboard-view",
        help="Parse onboard plan JSON -> Bot conversation view (stdin or --plan-json)",
    )
    p_ov.add_argument("--plan-json", default=None, help="Path to plan JSON (default stdin)")
    p_ov.add_argument("--prefs", default=None, help="Optional preferences.json for stale hints")
    p_ov.set_defaults(func=cmd_onboard_view)

    p_ps = sub.add_parser("prefs-show", help="Show allowlisted non-secret preferences")
    p_ps.add_argument("--path", default=None)
    p_ps.set_defaults(func=cmd_prefs_show)

    p_pw = sub.add_parser(
        "prefs-save",
        help="Explicit save of allowlisted preferences (refuses clobber without --overwrite)",
    )
    p_pw.add_argument("--desired", required=True, choices=["native", "wsl", "both"])
    p_pw.add_argument("--default-backend", required=True, choices=["native", "wsl"])
    p_pw.add_argument(
        "--discovery-kind",
        required=True,
        choices=["none", "native_only", "wsl_only", "both"],
    )
    p_pw.add_argument("--path", default=None)
    p_pw.add_argument("--overwrite", action="store_true")
    p_pw.set_defaults(func=cmd_prefs_save)

    p_jobs = sub.add_parser(
        "jobs-demo",
        help="Simulated job+outbox lifecycle (no grok, no webhook POST)",
    )
    p_jobs.add_argument("--state-db", required=True, help="SQLite path (use a temp file)")
    p_jobs.add_argument("--task-id", default="demo-task")
    p_jobs.add_argument("--correlation-id", default=None)
    p_jobs.add_argument("--backend", default="wsl")
    p_jobs.add_argument("--cwd", default="/tmp/demo")
    p_jobs.add_argument("--max-attempts", type=int, default=5)
    p_jobs.add_argument("--fail", action="store_true", help="Simulate failure+requeue")
    p_jobs.set_defaults(func=cmd_jobs_demo)

    p_rp = sub.add_parser("repo-plan", help="Dry-run repo-question manifest plan (metadata only; reads no file contents)")
    p_rp.add_argument("--root", required=True, help="Owner-selected absolute repo root (local)")
    p_rp.add_argument("--select", action="append", default=[], help="Relative file/dir under root (repeatable; no wildcards)")
    p_rp.add_argument("--reveal-paths", action="store_true", help="Include root and relative paths in output (owner-local only)")
    p_rp.add_argument("--format", choices=["json", "text"], default="json")
    p_rp.set_defaults(func=cmd_repo_plan)

    p_doc = sub.add_parser("doctor", help="Read-only companion report (fixture default; --live probes this machine)")
    p_doc.add_argument("--live", action="store_true", help="Probe the machine running this command (read-only)")
    p_doc.add_argument("--state-db", default=None, help="Existing state DB to check read-only (never created)")
    p_doc.add_argument("--format", choices=["json", "text"], default="json")
    p_doc.set_defaults(func=cmd_doctor)

    p_status = sub.add_parser("jobs-status", help="Inspect jobs/outbox in a state DB")
    p_status.add_argument("--state-db", required=True)
    p_status.add_argument("--show-result", action="store_true", help="Include allowlisted result summary (default hidden)")
    p_status.add_argument("--task-id", default=None, help="Only this job (SQL-scoped)")
    p_status.add_argument("--limit", type=int, default=50, help="Max jobs/events (1..500)")
    p_status.add_argument("--offset", type=int, default=0)
    p_status.add_argument("--format", choices=["json", "text"], default="json")
    p_status.add_argument("--assume-static-snapshot", action="store_true",
                          help="Operator asserts the DB file is a static copy nobody writes (opens immutable, no sidecars). Wrong results if false.")
    p_status.set_defaults(func=cmd_jobs_status)

    p_tick = sub.add_parser(
        "outbox-tick",
        help="One-shot SIMULATED outbox delivery (fake acks, no network)",
    )
    p_tick.add_argument("--state-db", required=True)
    p_tick.add_argument("--fail-transport", action="store_true")
    p_tick.add_argument("--bot-process", action="store_true")
    p_tick.add_argument("--limit", type=int, default=50)
    p_tick.set_defaults(func=cmd_outbox_tick)

    p_pump = sub.add_parser(
        "outbox-pump",
        help="Dry-run (default) or --send webhook pump for due outbox events",
    )
    p_pump.add_argument("--state-db", required=True)
    p_pump.add_argument("--config", help="Path to webhook.json (outside repo)")
    p_pump.add_argument("--url", help="HTTPS webhook URL (send mode; prefer --config)")
    p_pump.add_argument("--token", help="Bearer token (send mode; prefer --config)")
    p_pump.add_argument("--send", action="store_true", help="Actually POST (default is dry-run)")
    p_pump.add_argument("--limit", type=int, default=20)
    p_pump.set_defaults(func=cmd_outbox_pump)

    p_run = sub.add_parser(
        "runner-run",
        help="One-shot job runner (dry-run default; --execute to spawn)",
    )
    p_run.add_argument("--state-db", required=True)
    p_run.add_argument("--task-id", required=True)
    p_run.add_argument("--backend", choices=["native", "wsl"], default="wsl")
    p_run.add_argument("--cwd", required=True)
    p_run.add_argument("--executable", default=None, help="Resolved grok or bridge-dummy path")
    p_run.add_argument("--wsl-probe", action="store_true", help="Fixed /usr/bin/printf inside a RUNNING distro via wsl.exe (transport proof, not Grok)")
    p_run.add_argument("--wsl-grok-smoke", action="store_true", help="Fixed one-shot Grok smoke via wsl.exe (dry-run; real call needs --execute --allow-model-call)")
    p_run.add_argument("--allow-model-call", action="store_true", help="With --wsl-grok-smoke --execute: permit ONE real Grok call (owner-approved; default off)")
    p_run.add_argument("--wsl-distro", default=None)
    p_run.add_argument("--wsl-user", default=None)
    p_run.add_argument("--owner", default="local-worker")
    p_run.add_argument("--timeout", type=float, default=30.0)
    p_run.add_argument("--lease-sec", type=float, default=300.0)
    p_run.add_argument(
        "--heartbeat-sec",
        type=float,
        default=None,
        help="Lease renew interval while running (default: lease_sec/3 capped)",
    )
    p_run.add_argument("--max-turns", type=int, default=None)
    p_run.add_argument("--permission-mode", default=None)
    p_run.add_argument("--prompt", default="")
    p_run.add_argument("--force-prompt-argv", action="store_true")
    p_run.add_argument("--deny", action="append", default=[])
    p_run.add_argument("--extra", action="append", default=[])
    p_run.add_argument("--execute", action="store_true", help="Actually spawn process")
    p_run.add_argument("--parse-grok-json", action="store_true", help="Parse stdout as grok --output-format json into an allowlisted summary (text never stored)")
    p_run.add_argument("--show-answer", action="store_true", help="With --parse-grok-json: print bounded answer (transient; not stored)")
    p_run.add_argument("--expect-bridge-ok", action="store_true", help="Require exact BRIDGE_OK text (implies --parse-grok-json)")
    p_run.set_defaults(func=cmd_runner_run)

    p_ask = sub.add_parser("ask", help="Text-only single Grok prompt via wsl.exe (dry-run default)")
    p_ask.add_argument("--prompt", default=None)
    p_ask.add_argument("--prompt-file", default=None)
    p_ask.add_argument("--prompt-stdin", action="store_true")
    p_ask.add_argument("--wsl-distro", required=True)
    p_ask.add_argument("--wsl-user", required=True)
    p_ask.add_argument("--executable", required=True, help="Absolute POSIX path to grok inside WSL")
    p_ask.add_argument("--cwd", default=None, help="Existing /tmp/gbbb-ask-<random> dir (default: mktemp on execute)")
    p_ask.add_argument("--max-turns", type=int, default=1)
    p_ask.add_argument("--timeout", type=float, default=180.0)
    p_ask.add_argument("--task-id", default=None)
    p_ask.add_argument("--state-db", default=None, help="Used only with --execute")
    p_ask.add_argument("--owner", default="local-worker")
    p_ask.add_argument("--execute", action="store_true")
    p_ask.add_argument("--allow-model-call", action="store_true")
    p_ask.add_argument("--show-answer", action="store_true", help="Print bounded answer text (transient; not stored)")
    p_ask.add_argument("--format", choices=["json", "text"], default="json")
    p_ask.set_defaults(func=cmd_ask)

    p_retry = sub.add_parser("jobs-retry", help="Explicit retry from failed/needs_review")
    p_retry.add_argument("--state-db", required=True)
    p_retry.add_argument("--task-id", required=True)
    p_retry.set_defaults(func=cmd_jobs_retry)

    p_cancel = sub.add_parser(
        "jobs-cancel",
        help="Mark job cancelled (STATE-ONLY; does not kill processes)",
    )
    p_cancel.add_argument("--state-db", required=True)
    p_cancel.add_argument("--task-id", required=True)
    p_cancel.set_defaults(func=cmd_jobs_cancel)
    sub.add_parser(
        "mcp-http-check",
        help=(
            "Dry-run validate authenticated MCP streamable-HTTP config "
            "(static bearer + Host/Origin); never starts a listener"
        ),
    ).set_defaults(func=cmd_mcp_http_check)

    sub.add_parser(
        "mcp-stdio",
        help=(
            "Bounded read-only MCP stdio server (discover/onboard/status/job-status); "
            "requires optional [mcp] extra; never binds a port"
        ),
    ).set_defaults(func=cmd_mcp_stdio)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
