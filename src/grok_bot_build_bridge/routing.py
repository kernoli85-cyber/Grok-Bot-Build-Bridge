"""Per-job backend routing and path validation — no silent fallback, no dual exec."""
from __future__ import annotations

import re
from typing import Union

from .models import BackendId, DiscoveryReport, JobPlan, JobRequest, RoutingError

_WIN_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_WSL_MNT = re.compile(r"^/mnt/[a-z](/|$)")
_WSL_UNC = re.compile(r"^\\\\\\\\wsl(\\.localhost)?\\\\", re.IGNORECASE)
_WSL_UNC2 = re.compile(r"^//wsl(\\.localhost)?/", re.IGNORECASE)


def select_backend(
    report: DiscoveryReport, requested: BackendId | None
) -> Union[BackendId, RoutingError]:
    ready = report.ready_backends()
    if requested is None:
        if report.default_backend and report.default_backend in ready:
            return report.default_backend
        if report.kind == "both":
            return RoutingError(
                "default_required",
                "both backends ready; set default or pass backend explicitly",
            )
        return RoutingError("no_backend", "no ready backend and no request")
    if requested not in ready:
        return RoutingError(
            "backend_not_ready",
            f"requested backend {requested!r} is not ready; no silent fallback",
        )
    return requested


def validate_cwd(backend: BackendId, cwd: str) -> RoutingError | None:
    if not cwd or not cwd.strip():
        return RoutingError("cwd_empty", "cwd is required")
    path = cwd.strip()
    if backend == "native":
        if _WSL_MNT.match(path) or _WSL_UNC.match(path) or _WSL_UNC2.match(path):
            return RoutingError(
                "cwd_backend_mismatch",
                "WSL-style path rejected for native backend",
            )
        # Prefer drive-letter or UNC that is not wsl$
        if path.startswith("/") and not _WIN_DRIVE.match(path):
            return RoutingError(
                "cwd_backend_mismatch",
                "POSIX path rejected for native backend without mapping",
            )
        return None
    # wsl
    if _WIN_DRIVE.match(path) or _WSL_UNC.match(path) or _WSL_UNC2.match(path):
        return RoutingError(
            "cwd_backend_mismatch",
            "Windows drive/UNC path rejected for WSL job without explicit mapping",
        )
    if not path.startswith("/"):
        return RoutingError("cwd_invalid", "WSL cwd must be an absolute POSIX path")
    return None


def plan_job(report: DiscoveryReport, req: JobRequest) -> JobPlan | RoutingError:
    selected = select_backend(report, req.backend)
    if isinstance(selected, RoutingError):
        return selected
    err = validate_cwd(selected, req.cwd)
    if err:
        return err
    if not req.task_id.strip():
        return RoutingError("task_id_empty", "task_id is required")
    if not req.prompt.strip():
        return RoutingError("prompt_empty", "prompt is required")

    # Scaffold argv only — does not execute. Maps to grok -p/--single surface.
    argv: list[str] = ["grok", "--single", req.prompt, "--cwd", req.cwd]
    if req.max_turns is not None:
        argv.extend(["--max-turns", str(req.max_turns)])
    if req.permission_mode:
        argv.extend(["--permission-mode", req.permission_mode])
    for rule in req.allow_rules:
        argv.extend(["--allow", rule])
    for rule in req.deny_rules:
        argv.extend(["--deny", rule])

    return JobPlan(
        task_id=req.task_id,
        backend=selected,
        cwd=req.cwd,
        argv=tuple(argv),
        env={"GROK_BOT_BUILD_BRIDGE_TASK_ID": req.task_id},
    )
