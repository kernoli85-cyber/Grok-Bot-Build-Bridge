"""CLI skeleton: guided discovery presentation — no installers, no side effects."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from . import __version__
from .discovery import classify_discovery, nonroot_ready
from .models import DiscoveryReport
from .adapter import discover_host


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
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
