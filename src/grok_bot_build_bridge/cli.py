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
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
