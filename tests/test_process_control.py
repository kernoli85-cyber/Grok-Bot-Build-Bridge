from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from grok_bot_build_bridge.process_control import (
    WindowsJobDemoError,
    capability_matrix,
    kill_direct_child,
    kill_posix_process_group,
    spawn_posix_session,
    spawn_windows_job_contained,
    win_pid_alive,
)

FIX = Path(__file__).parent / "fixtures"
TREE = FIX / "bridge-dummy-tree.py"
IS_WIN = sys.platform.startswith("win")


def test_capability_matrix_honest():
    caps = capability_matrix()
    assert caps.production_cancel == "state_only"
    assert "not_claimed" in caps.direct_child_kill or "not" in caps.direct_child_kill
    if IS_WIN:
        assert "job_object_demo" in caps.process_tree_kill
        assert caps.win_job_object_demo == "available_for_dummy_trees"
        assert "not_guest" in caps.wsl_guest_tree or "not_guest_cleanup" in caps.wsl_guest_tree
        assert "host_job_not_guest" in caps.wsl_guest_tree
    else:
        assert "posix_process_group_demo" in caps.process_tree_kill
        assert caps.win_job_object_demo == "unsupported_on_posix"


def test_no_broad_pid_or_taskkill_or_wsl_terminate_exports():
    import grok_bot_build_bridge.process_control as pc

    forbidden_substrings = (
        "taskkill",
        "kill_by_pid",
        "kill_pid",
        "wsl_terminate",
        "wsl_shutdown",
        "terminate_distro",
    )
    for name in dir(pc):
        low = name.lower()
        for bad in forbidden_substrings:
            assert bad not in low, f"forbidden export {name}"


@pytest.mark.skipif(IS_WIN, reason="POSIX process group demo")
def test_posix_process_group_stops_dummy_tree(tmp_path: Path):
    marker = tmp_path / "keep"
    marker.write_text("1", encoding="utf-8")
    child_pid_file = tmp_path / "child.pid"
    env = {**os.environ, "BRIDGE_CHILD_PID_FILE": str(child_pid_file)}
    proc = spawn_posix_session(
        [sys.executable, str(TREE), "--marker", str(marker), "--sleep", "20"],
        env=env,
    )
    for _ in range(80):
        if child_pid_file.exists():
            break
        time.sleep(0.05)
    assert child_pid_file.exists(), "dummy tree child did not start"
    child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
    assert child_pid > 0
    os.kill(child_pid, 0)
    note = kill_posix_process_group(proc)
    assert "kill" in note
    assert proc.poll() is not None
    gone = False
    for _ in range(80):
        try:
            os.kill(child_pid, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            gone = True
            break
    assert gone, "grandchild survived process-group kill — demo failed"


@pytest.mark.skipif(IS_WIN, reason="uses posix spawn")
def test_direct_child_kill_not_claimed_as_tree():
    proc = spawn_posix_session([sys.executable, "-c", "import time; time.sleep(30)"])
    note = kill_direct_child(proc)
    assert note.startswith("direct_child")
    assert proc.poll() is not None


@pytest.mark.skipif(not IS_WIN, reason="Windows Job Object demo")
def test_windows_job_object_terminates_dummy_tree(tmp_path: Path):
    marker = tmp_path / "keep"
    marker.write_text("1", encoding="utf-8")
    child_pid_file = tmp_path / "child.pid"
    env = {**os.environ, "BRIDGE_CHILD_PID_FILE": str(child_pid_file)}
    # Pass env via a tiny wrapper: CreateProcess doesn't take our env dict easily
    # without full environment block — use child argv --pid-file instead.
    contained = spawn_windows_job_contained(
        [
            sys.executable,
            str(TREE),
            "--marker",
            str(marker),
            "--sleep",
            "25",
            "--pid-file",
            str(child_pid_file),
        ]
    )
    try:
        assert contained.pid > 0
        assert win_pid_alive(contained.pid)
        for _ in range(100):
            if child_pid_file.exists():
                break
            time.sleep(0.05)
        assert child_pid_file.exists(), "Windows dummy grandchild did not start"
        gpid = int(child_pid_file.read_text(encoding="utf-8").strip())
        assert gpid > 0
        assert win_pid_alive(gpid), "grandchild not alive before terminate"
        note = contained.terminate_contained_job_demo()
        assert note == "terminate_job_object_demo_ok"
        # Root and grandchild must both be gone
        for _ in range(100):
            if not win_pid_alive(contained.pid) and not win_pid_alive(gpid):
                break
            time.sleep(0.05)
        assert not win_pid_alive(contained.pid), "root survived TerminateJobObject"
        assert not win_pid_alive(gpid), "grandchild survived TerminateJobObject"
    finally:
        contained.cleanup()


@pytest.mark.skipif(not IS_WIN, reason="Windows Job Object demo")
def test_windows_kill_on_close_terminates_tree(tmp_path: Path):
    marker = tmp_path / "keep"
    marker.write_text("1", encoding="utf-8")
    child_pid_file = tmp_path / "child.pid"
    contained = spawn_windows_job_contained(
        [
            sys.executable,
            str(TREE),
            "--marker",
            str(marker),
            "--sleep",
            "25",
            "--pid-file",
            str(child_pid_file),
        ]
    )
    try:
        for _ in range(100):
            if child_pid_file.exists():
                break
            time.sleep(0.05)
        assert child_pid_file.exists()
        gpid = int(child_pid_file.read_text(encoding="utf-8").strip())
        assert win_pid_alive(gpid)
        note = contained.close_job_kill_on_close()
        assert note == "job_handle_closed_kill_on_close"
        for _ in range(100):
            if not win_pid_alive(contained.pid) and not win_pid_alive(gpid):
                break
            time.sleep(0.05)
        assert not win_pid_alive(contained.pid)
        assert not win_pid_alive(gpid)
    finally:
        if not contained._closed:
            contained.cleanup()


@pytest.mark.skipif(not IS_WIN, reason="Windows Job Object demo")
def test_windows_empty_argv_fail_closed():
    with pytest.raises(WindowsJobDemoError) as ei:
        spawn_windows_job_contained([])
    assert ei.value.code == "empty_argv"


@pytest.mark.skipif(IS_WIN, reason="unsupported path on Windows")
def test_windows_spawn_raises_on_posix():
    with pytest.raises(WindowsJobDemoError) as ei:
        spawn_windows_job_contained([sys.executable, "-c", "pass"])
    assert ei.value.code == "unsupported"


def test_jobs_cancel_still_state_only_in_capability():
    assert capability_matrix().production_cancel == "state_only"
