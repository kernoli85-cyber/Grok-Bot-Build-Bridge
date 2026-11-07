"""Process containment / cancel semantics — honest capability matrix + demos.

Production JobStore.cancel / jobs-cancel remain STATE-ONLY. This module is NOT
wired into the one-shot runner cancel path and does NOT expose arbitrary
kill-by-PID, taskkill, or WSL distro termination.

Official notes (Microsoft Learn):
- Job Objects group processes; TerminateJobObject ends all associated processes.
  Default: children created with CreateProcess join the parent's job.
  Use CREATE_SUSPENDED → AssignProcessToJobObject → ResumeThread to avoid the
  create/assign race where the child could spawn before assignment.
  JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE terminates members when the last job
  handle is closed.
- WSL: wsl --terminate / --shutdown are distro/VM-wide — too broad for per-job
  cancel. Killing wsl.exe ≠ guest process-tree cleanup. Host Job Objects do
  NOT contain Linux guest PIDs.

Demos only:
- POSIX: start_new_session + os.killpg for contained bridge-dummy trees.
- Windows: per-launch Job Object around bridge-dummy trees (ctypes WinAPI).
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
    win_job_object_demo: str


def capability_matrix() -> CancelCapability:
    plat = sys.platform
    if plat.startswith("win"):
        return CancelCapability(
            platform="windows",
            direct_child_kill="supported_via_Popen_kill_not_claimed_as_tree",
            process_tree_kill="job_object_demo_only_not_wired_in_runner",
            wsl_guest_tree="wsl_exe_kill_not_guest_cleanup; terminate_shutdown_too_broad; host_job_not_guest",
            production_cancel="state_only",
            win_job_object_demo="available_for_dummy_trees",
        )
    return CancelCapability(
        platform=plat,
        direct_child_kill="supported_via_Popen_kill_not_claimed_as_tree",
        process_tree_kill="posix_process_group_demo_only_for_contained_dummies",
        wsl_guest_tree="n_a_on_posix_host",
        production_cancel="state_only",
        win_job_object_demo="unsupported_on_posix",
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
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
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


# --- Windows Job Object demo (ctypes) -------------------------------------------------

CREATE_SUSPENDED = 0x00000004
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x00000102


class WindowsJobDemoError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class WindowsContainedProcess:
    """Demo-only handle bundle for one job-contained root process.

    Owns: job handle, process handle, thread handle. Closing the job with
    KILL_ON_JOB_CLOSE terminates remaining members (Microsoft Learn).
    """

    pid: int
    _job: int
    _process: int
    _thread: int
    _closed: bool = False

    def pid_alive(self) -> bool:
        return win_pid_alive(self.pid)

    def wait(self, timeout_ms: int = 5000) -> bool:
        """Wait for root process exit. True if exited."""
        if not sys.platform.startswith("win"):
            raise WindowsJobDemoError("unsupported", "windows only")
        k32 = _kernel32()
        rc = k32.WaitForSingleObject(self._process, timeout_ms)
        return rc == WAIT_OBJECT_0

    def terminate_contained_job_demo(self, exit_code: int = 1) -> str:
        """TerminateJobObject on the job we created — demo only, not runner cancel."""
        if self._closed:
            return "already_closed"
        k32 = _kernel32()
        if not k32.TerminateJobObject(self._job, exit_code):
            err = k32.GetLastError()
            return f"terminate_job_failed:{err}"
        self.wait(5000)
        return "terminate_job_object_demo_ok"

    def close_job_kill_on_close(self) -> str:
        """Close job handle → KILL_ON_JOB_CLOSE ends remaining members."""
        if self._closed:
            return "already_closed"
        self._close_handles(close_job=True)
        return "job_handle_closed_kill_on_close"

    def abandon_without_kill_claim(self) -> None:
        """Close process/thread handles but keep job open (tests must still close)."""
        self._close_process_thread_only()

    def cleanup(self) -> None:
        """Best-effort: terminate job then close all handles (no orphans)."""
        if self._closed:
            return
        try:
            self.terminate_contained_job_demo()
        except Exception:
            pass
        self._close_handles(close_job=True)

    def _close_process_thread_only(self) -> None:
        k32 = _kernel32()
        if self._thread:
            k32.CloseHandle(self._thread)
            self._thread = 0
        if self._process:
            k32.CloseHandle(self._process)
            self._process = 0

    def _close_handles(self, *, close_job: bool) -> None:
        k32 = _kernel32()
        self._close_process_thread_only()
        if close_job and self._job:
            k32.CloseHandle(self._job)
            self._job = 0
        self._closed = True


def _kernel32():
    import ctypes

    return ctypes.windll.kernel32


def _configure_winapi() -> None:
    """Set ctypes argtypes/restypes once for correct handle widths on Win64."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    if getattr(k32, "_bridge_job_configured", False):
        return

    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.CreateJobObjectW.restype = wintypes.HANDLE

    k32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    k32.SetInformationJobObject.restype = wintypes.BOOL

    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL

    k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateJobObject.restype = wintypes.BOOL

    k32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    k32.CreateProcessW.restype = wintypes.BOOL

    k32.ResumeThread.argtypes = [wintypes.HANDLE]
    k32.ResumeThread.restype = wintypes.DWORD

    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateProcess.restype = wintypes.BOOL

    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE

    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.GetExitCodeProcess.restype = wintypes.BOOL

    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD

    k32.GetLastError.argtypes = []
    k32.GetLastError.restype = wintypes.DWORD

    k32._bridge_job_configured = True  # type: ignore[attr-defined]


def _job_extended_limit_struct():
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    return JOBOBJECT_EXTENDED_LIMIT_INFORMATION


def _startupinfo_processinfo():
    import ctypes
    from ctypes import wintypes

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    return STARTUPINFOW, PROCESS_INFORMATION


def win_pid_alive(pid: int) -> bool:
    if not sys.platform.startswith("win"):
        raise WindowsJobDemoError("unsupported", "windows only")
    if pid <= 0:
        return False
    _configure_winapi()
    import ctypes
    from ctypes import wintypes

    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return int(code.value) == STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def spawn_windows_job_contained(argv: Sequence[str]) -> WindowsContainedProcess:
    """CREATE_SUSPENDED → AssignProcessToJobObject → ResumeThread (demo only).

    On any failure before successful resume, terminates the unpublished child
    and closes handles so no orphan remains. Not used by production cancel.
    """
    if not sys.platform.startswith("win"):
        raise WindowsJobDemoError("unsupported", "windows job demo requires win32")
    if not argv:
        raise WindowsJobDemoError("empty_argv", "argv required")

    import ctypes
    from ctypes import wintypes

    _configure_winapi()
    k32 = _kernel32()
    ExtInfo = _job_extended_limit_struct()
    STARTUPINFOW, PROCESS_INFORMATION = _startupinfo_processinfo()

    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise WindowsJobDemoError("create_job_failed", f"GetLastError={k32.GetLastError()}")

    info = ExtInfo()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
    ):
        err = k32.GetLastError()
        k32.CloseHandle(job)
        raise WindowsJobDemoError("set_job_info_failed", f"GetLastError={err}")

    cmdline = subprocess.list2cmdline([str(a) for a in argv])
    si = STARTUPINFOW()
    pi = PROCESS_INFORMATION()

    # Prefer BREAKAWAY so we can Assign into our job when the parent (e.g. CI)
    # is already inside a job that allows breakaway (Microsoft Learn).
    created = False
    last_err = 0
    for flags in (
        CREATE_SUSPENDED | CREATE_BREAKAWAY_FROM_JOB,
        CREATE_SUSPENDED,
    ):
        cmdline_buf = ctypes.create_unicode_buffer(cmdline)
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        pi = PROCESS_INFORMATION()
        ok = k32.CreateProcessW(
            None,
            cmdline_buf,
            None,
            None,
            False,
            flags,
            None,
            None,
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        if ok:
            created = True
            break
        last_err = k32.GetLastError()
    if not created:
        k32.CloseHandle(job)
        raise WindowsJobDemoError("create_process_failed", f"GetLastError={last_err}")

    h_process = int(pi.hProcess) if pi.hProcess else 0
    h_thread = int(pi.hThread) if pi.hThread else 0
    pid = int(pi.dwProcessId)

    def _fail_cleanup(code: str, err: int) -> None:
        if h_process:
            k32.TerminateProcess(h_process, 1)
            k32.WaitForSingleObject(h_process, 5000)
        if h_thread:
            k32.CloseHandle(h_thread)
        if h_process:
            k32.CloseHandle(h_process)
        k32.CloseHandle(job)
        raise WindowsJobDemoError(code, f"GetLastError={err}")

    if not k32.AssignProcessToJobObject(job, pi.hProcess):
        _fail_cleanup("assign_job_failed", k32.GetLastError())

    # ResumeThread returns previous suspend count; 0xFFFFFFFF indicates failure
    prev = k32.ResumeThread(pi.hThread)
    if prev == 0xFFFFFFFF:
        _fail_cleanup("resume_failed", k32.GetLastError())

    return WindowsContainedProcess(
        pid=pid,
        _job=int(job),
        _process=h_process,
        _thread=h_thread,
    )


def spawn_windows_job_contained_fail_closed_on_empty() -> None:
    """Helper used by tests for failure-path import without spawning."""
    spawn_windows_job_contained([])
