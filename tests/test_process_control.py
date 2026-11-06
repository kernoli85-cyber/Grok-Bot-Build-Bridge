from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from grok_bot_build_bridge.process_control import (
    capability_matrix,
    kill_direct_child,
    kill_posix_process_group,
    spawn_posix_session,
)

FIX = Path(__file__).parent / "fixtures"
TREE = FIX / "bridge-dummy-tree.py"


def test_capability_matrix_honest():
    caps = capability_matrix()
    assert caps.production_cancel == "state_only"
    assert "not" in caps.direct_child_kill or "not_claimed" in caps.direct_child_kill
    if sys.platform.startswith("win"):
        assert "job_objects" in caps.process_tree_kill
        assert "not_guest_cleanup" in caps.wsl_guest_tree or "wsl_exe" in caps.wsl_guest_tree
    else:
        assert "posix_process_group_demo" in caps.process_tree_kill


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX process group demo")
def test_posix_process_group_stops_dummy_tree(tmp_path: Path):
    marker = tmp_path / "keep"
    marker.write_text("1", encoding="utf-8")
    child_pid_file = tmp_path / "child.pid"
    env = {**os.environ, "BRIDGE_CHILD_PID_FILE": str(child_pid_file)}
    proc = spawn_posix_session(
        [sys.executable, str(TREE), "--marker", str(marker), "--sleep", "20"],
        env=env,
    )
    # Wait for grandchild pid file
    for _ in range(50):
        if child_pid_file.exists():
            break
        time.sleep(0.05)
    assert child_pid_file.exists(), "dummy tree child did not start"
    child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
    assert child_pid > 0
    # Confirm child alive
    os.kill(child_pid, 0)
    note = kill_posix_process_group(proc)
    assert "kill" in note
    # Parent should be gone
    assert proc.poll() is not None
    # Grandchild should be gone (same process group)
    gone = False
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
            time.sleep(0.05)
        except ProcessLookupError:
            gone = True
            break
    assert gone, "grandchild survived process-group kill — demo failed"


@pytest.mark.skipif(sys.platform.startswith("win"), reason="uses posix spawn")
def test_direct_child_kill_not_claimed_as_tree():
    # Document: kill_direct_child only targets Popen handle
    proc = spawn_posix_session([sys.executable, "-c", "import time; time.sleep(30)"])
    note = kill_direct_child(proc)
    assert note.startswith("direct_child")
    assert proc.poll() is not None


def test_no_windows_taskkill_helper_exported():
    import grok_bot_build_bridge.process_control as pc

    for name in dir(pc):
        low = name.lower()
        assert "taskkill" not in low
        assert "terminate_job" not in low
