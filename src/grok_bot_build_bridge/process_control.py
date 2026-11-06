"""Process containment / cancel semantics — honest capability matrix.

Production JobStore.cancel remains STATE-ONLY. This module documents platform
limits and exposes *demo-only* helpers for intentionally contained dummy trees.
It is NOT wired into the one-shot runner cancel path and does NOT expose
arbitrary kill-by-PID or taskkill MCP surfaces.

Official / observed facts (research notes):
- Windows Job Objects can terminate associated process trees via
  TerminateJobObject (Microsoft Learn: Job Objects). CREATE_NEW_PROCESS_GROUP
  alone is for console ctrl events — not a substitute for job-object tree kill.
- WSL: `wsl --terminate <Distro>` / `wsl --shutdown` stop a distribution or the
  WSL VM (Microsoft Learn: Basic commands for WSL). Killing the Windows-side
  wsl.exe helper is NOT equivalent to cleaning up the Linux guest process tree;
  distro-wide terminate/shutdown is too broad for per-job cancel.
- Direct-child Popen.kill()/terminate() is NOT process-tree kill.

Safe demo (POSIX only): spawn with start_new_session=True and stop via
os.killpg(pgid, SIGKILL) for processes we ourselves created in tests.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class CancelCapability:
    platform: str
    direct_child_kill: str
    process_tree_kill: str
    wsl_guest_tree: str
    production_cancel: str


def capability_matrix() -> CancelCapability:
    plat = sys.platform
    if plat.startswith("win"):
        return CancelCapability(
            platform="windows",
            direct_child_kill="supported_via_Popen_kill_not_claimed_as_tree",
            process_tree_kill="requires_job_objects_not_wired_in_runner",
            wsl_guest_tree="wsl_exe_kill_not_guest_cleanup; terminate_shutdown_too_broad",
            production_cancel="state_only",
        )
    return CancelCapability(
        platform=plat,
        direct_child_kill="supported_via_Popen_kill_not_claimed_as_tree",
        process_tree_kill="posix_process_group_demo_only_for_contained_dummies",
        wsl_guest_tree="n_a_on_posix_host",
        production_cancel="state_only",
    )


def kill_direct_child(proc: subprocess.Popen) -> str:
    """Kill only the known Popen child. Does NOT claim tree cleanup."""
    try:
        proc.kill()
    except OSError:
        return "kill_failed"
    try:
        proc.wait(timeout=5)
    except Exception:
        return "kill_sent_wait_uncertain"
    return "direct_child_kill_attempted"


def spawn_posix_session(argv: Sequence[str], **kwargs) -> subprocess.Popen:
    """Spawn with a new session (POSIX). Raises on Windows — not a silent fallback."""
    if sys.platform.startswith("win"):
        raise RuntimeError("posix_session_spawn_unsupported_on_windows")
    return subprocess.Popen(
        list(argv),
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )


def kill_posix_process_group(proc: subprocess.Popen) -> str:
    """SIGKILL the process group for a start_new_session child we own (demo)."""
    if sys.platform.startswith("win"):
        raise RuntimeError("posix_killpg_unsupported_on_windows")
    if proc.pid is None:
        return "no_pid"
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return "group_already_gone"
    except OSError:
        return "killpg_failed"
    try:
        proc.wait(timeout=5)
    except Exception:
        return "killpg_sent_wait_uncertain"
    return "posix_process_group_kill_attempted"
