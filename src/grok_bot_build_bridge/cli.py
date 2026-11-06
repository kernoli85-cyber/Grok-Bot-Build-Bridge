"""CLI skeleton: guided discovery presentation — no installers, no side effects."""
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
            "none": "Offer Windows / WSL2 / both links only — do not auto-install",
            "native_only": "Default native; WSL optional later",
            "wsl_only": "Default wsl; Native optional later",
            "both": "Ask default; per-job backend; never parallel-start",
        }.get(report.kind),
    }


def cmd_discover_demo(_args: argparse.Namespace) -> int:
    """Deterministic demo using fixtures — no host probing."""
    from pathlib import Path

    fix = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
    wsl_list = (fix / "wsl_list_v_running.txt").read_text(encoding="utf-8")
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


def cmd_version(_args: argparse.Namespace) -> int:
    print(__version__)
    return 0


def cmd_plan_demo(_args: argparse.Namespace) -> int:
    """Deterministic job-plan demo — validates routing contract, never shells out."""
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
    """Simulated job lifecycle against a state DB — no grok, no webhook POST."""
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


def cmd_jobs_status(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    jobs = store.list_jobs()
    events = store.list_outbox()
    json.dump(
        {
            "state_db": str(store.db_path),
            "jobs": [
                {
                    "task_id": j.task_id,
                    "correlation_id": j.correlation_id,
                    "status": j.status.value,
                    "attempt": j.attempt,
                    "backend": j.backend,
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
                }
                for e in events
            ],
            "note": "Status metadata only; prompts/transcripts are not stored by default.",
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


def cmd_outbox_tick(args: argparse.Namespace) -> int:
    """One-shot simulated outbox pass — not a daemon, not real HTTP."""
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
    """Dry-run (default) or explicit --send webhook pump — no daemon."""
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
            "heartbeats": outcome.heartbeats,
            "lease_lost": outcome.lease_lost,
            "state_db": str(store.db_path),
            "cancel_note": "cancellation is state-only; process tree kill not claimed",
            "cancel_capability": {
                "platform": capability_matrix().platform,
                "production_cancel": capability_matrix().production_cancel,
                "process_tree_kill": capability_matrix().process_tree_kill,
                "wsl_guest_tree": capability_matrix().wsl_guest_tree,
            },
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0


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
            },
        },
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
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

    p_status = sub.add_parser("jobs-status", help="Inspect jobs/outbox in a state DB")
    p_status.add_argument("--state-db", required=True)
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
    p_run.add_argument("--executable", required=True, help="Resolved grok or bridge-dummy path")
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
    p_run.set_defaults(func=cmd_runner_run)

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
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
