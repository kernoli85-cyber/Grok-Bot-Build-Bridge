"""Direct local POSIX runner (inside WSL/Linux) for the protected text-only ask. No wsl.exe.

The guest argv is taken VERBATIM from build_wsl_grok_ask_plan (everything after `--exec`),
so tools/deny rules/sandbox/dontAsk/env switches/guest timeout are identical by construction.

Containment: own session/process group; stdout/stderr read with a byte cap; on deadline or
cap the whole group gets TERM then KILL. A descendant that calls setsid() can escape the
group; without cgroups this is NOT full containment, so cleanup is reported as
`group_empty` (proven for the group only) or `unproven`.
"""
from __future__ import annotations

import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from .grok_result import MAX_RESULT_BYTES, classify, extract_answer_text, parse_grok_json_result
from .plan import (ASK_GUEST_KILL_GRACE, ASK_HOST_DEADLINE_MARGIN, PlanError,
                   build_wsl_grok_ask_plan)

STDERR_CAP = 16 * 1024
TMP_PARENT = "/tmp"
_ASK_DIR = re.compile(r"^/tmp/gbbb-ask-[a-f0-9]{16}$")
_REQUIRED_TOOLS = ("/usr/bin/timeout", "/usr/bin/env")


class RunnerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class LocalPlan:
    argv: tuple[str, ...]
    cwd: str
    host_deadline_sec: float
    expected_models: tuple[str, ...] | None


def require_posix_nonroot() -> int:
    if os.name != "posix" or sys.platform == "win32":
        raise RunnerError("posix_only", "local runner is POSIX only")
    uid = os.getuid()
    if uid == 0 or os.geteuid() == 0:
        raise RunnerError("root_refused", "local runner refuses root")
    return uid


def _trusted_file(path: str, uid: int) -> None:
    try:
        st = os.stat(path)  # follows symlinks: the final target is what runs
    except OSError:
        raise RunnerError("executable_missing", "required executable not found") from None
    if not stat.S_ISREG(st.st_mode) or not st.st_mode & 0o111:
        raise RunnerError("executable_invalid", "required executable is not a regular executable file")
    if st.st_uid not in (0, uid) or st.st_mode & 0o022:
        raise RunnerError("executable_untrusted", "executable must be owned by root/you and not group/world-writable")
    real = os.path.realpath(path)
    d = os.path.dirname(real)
    while True:
        ds = os.stat(d)
        if ds.st_uid not in (0, uid) or (ds.st_mode & 0o002 and not ds.st_mode & stat.S_ISVTX):
            raise RunnerError("executable_untrusted", "executable directory chain is writable by others")
        if d == "/":
            break
        d = os.path.dirname(d)


def validate_executable(path: str) -> str:
    uid = require_posix_nonroot()
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or "/../" in path + "/":
        raise RunnerError("executable_invalid", "executable must be an absolute path (no PATH lookup)")
    for t in _REQUIRED_TOOLS:
        _trusted_file(t, uid)
    _trusted_file(path, uid)
    return path


def make_private_cwd() -> str:
    uid = require_posix_nonroot()
    pst = os.lstat(TMP_PARENT)
    if not stat.S_ISDIR(pst.st_mode) or (pst.st_mode & 0o002 and not pst.st_mode & stat.S_ISVTX):
        raise RunnerError("tmp_untrusted", "/tmp must be a sticky directory")
    path = f"{TMP_PARENT}/gbbb-ask-{secrets.token_hex(8)}"
    os.mkdir(path, 0o700)  # fails if it exists: never reuses a dir
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or stat.S_IMODE(st.st_mode) != 0o700 or os.listdir(path):
        raise RunnerError("cwd_untrusted", "fresh cwd failed verification")
    return path


def build_local_plan(*, grok_path: str, cwd: str, prompt: str, model, max_turns: int,
                     timeout_sec: float) -> LocalPlan:
    if not _ASK_DIR.match(cwd):
        raise RunnerError("cwd_invalid", "cwd must be a fresh /tmp/gbbb-ask-<hex16> dir")
    try:
        wsl = build_wsl_grok_ask_plan(task_id="local", distro="Ubuntu", user="localuser",
                                      grok_path=grok_path, cwd=cwd, prompt=prompt,
                                      max_turns=max_turns, timeout_sec=timeout_sec, model=model)
    except PlanError as exc:
        raise RunnerError(exc.code, exc.message) from None
    guest = tuple(wsl.argv[wsl.argv.index("--exec") + 1:])
    if guest[0] != "/usr/bin/timeout" or "wsl.exe" in guest:
        raise RunnerError("plan_invalid", "unexpected guest argv")
    return LocalPlan(guest, cwd, float(timeout_sec) + ASK_GUEST_KILL_GRACE + ASK_HOST_DEADLINE_MARGIN,
                     wsl.expected_models)


def _minimal_env() -> dict[str, str]:
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    for k in ("HOME", "USER", "LOGNAME"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _reader(stream, cap: int, sink: list, flag: list, done: threading.Event) -> None:
    total = 0
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                break
            if total + len(chunk) > cap:
                sink.append(chunk[: cap - total])
                flag.append(True)
                done.set()
                break
            sink.append(chunk)
            total += len(chunk)
    finally:
        stream.close()


def _kill_group(pgid: int) -> str:
    """group_empty: nothing left; group_killed: leftovers existed and are gone; unproven."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return "group_empty"
    except PermissionError:
        return "unproven"
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return "group_killed"
        except PermissionError:
            return "unproven"
        time.sleep(0.5 if sig == signal.SIGTERM else 0.2)
    try:
        os.killpg(pgid, 0)
        return "unproven"
    except ProcessLookupError:
        return "group_killed"
    except PermissionError:
        return "unproven"


def run_local(plan: LocalPlan, *, show_answer: bool = False) -> dict:
    """Run once. Never retries. Returns a redacted status dict (no prompt/paths/session)."""
    require_posix_nonroot()
    p = subprocess.Popen(list(plan.argv), cwd=plan.cwd, env=_minimal_env(), stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                         close_fds=True, shell=False)
    out: list[bytes] = []
    err: list[bytes] = []
    o_cut: list[bool] = []
    e_cut: list[bool] = []
    cap_hit = threading.Event()
    ts = [threading.Thread(target=_reader, args=(p.stdout, MAX_RESULT_BYTES, out, o_cut, cap_hit), daemon=True),
          threading.Thread(target=_reader, args=(p.stderr, STDERR_CAP, err, e_cut, threading.Event()), daemon=True)]
    for t in ts:
        t.start()
    deadline = time.monotonic() + plan.host_deadline_sec
    timed_out = False
    while p.poll() is None:
        if cap_hit.is_set():
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.05)
    cleanup = _kill_group(p.pid)
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        cleanup = "unproven"
    for t in ts:
        t.join(timeout=2)
    if any(t.is_alive() for t in ts):
        cleanup = "unproven"  # a descendant outside the group still holds our pipes (daemon reader left)
    rc = p.returncode
    truncated = bool(o_cut)
    stdout = b"".join(out)
    summary = parse_grok_json_result(stdout, truncated=truncated, expected_models=plan.expected_models)
    if timed_out or rc in (124, 137):
        status, code = "needs_review", "timeout"
    elif truncated:
        status, code = "needs_review", "output_truncated"
    elif rc != 0:
        status, code = "failed", "process_exit_nonzero"
    else:
        code = classify(summary, require_expected=False)
        status = "succeeded" if code is None else "needs_review"
    if status != "failed" and cleanup == "unproven":
        status, code = "needs_review", "cleanup_unproven"
    if status == "succeeded" and cleanup != "group_empty":
        status, code = "needs_review", "cleanup_unproven"
    cwd_state = _cleanup_cwd(plan.cwd)
    res = {"status": status, "error_code": code, "exit_code": rc, "retry": False,
           "stdout_truncated": truncated, "stderr_truncated": bool(e_cut),
           "process_cleanup": cleanup, "cwd_cleanup": cwd_state,
           "stop_reason": summary.stop_reason, "num_turns": summary.num_turns,
           "usage": summary.usage, "model_identity": summary.model_identity,
           "cost_status": summary.cost_status,
           "total_cost_usd": summary.total_cost_usd if summary.cost_status == "reported" else None}
    if show_answer:
        text, cut = extract_answer_text(stdout, truncated=truncated)
        res["answer"] = text
        res["answer_cut"] = cut
    return res


def _cleanup_cwd(cwd: str) -> str:
    """Remove only our own fresh dir, only if empty; never recursive."""
    if not _ASK_DIR.match(cwd):
        return "skipped"
    try:
        st = os.lstat(cwd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            return "left_untrusted"
        os.rmdir(cwd)
        return "removed"
    except OSError:
        return "left_nonempty"
