#!/usr/bin/env python3
"""Spawn a grandchild that waits on a marker file — for process-group demos only."""
import os
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    marker = None
    sleep_sec = 30.0
    if "--marker" in argv:
        i = argv.index("--marker")
        marker = Path(argv[i + 1])
    if "--sleep" in argv:
        i = argv.index("--sleep")
        sleep_sec = float(argv[i + 1])

    # Child writes its pid then sleeps; parent waits on child.
    child_pid_file = Path(os.environ.get("BRIDGE_CHILD_PID_FILE", "")) if os.environ.get("BRIDGE_CHILD_PID_FILE") else None

    def child() -> None:
        if child_pid_file is not None:
            child_pid_file.write_text(str(os.getpid()), encoding="utf-8")
        if marker is not None:
            # wait until marker removed or timeout
            end = time.time() + sleep_sec
            while time.time() < end:
                if not marker.exists():
                    break
                time.sleep(0.05)
        else:
            time.sleep(sleep_sec)

    pid = os.fork() if hasattr(os, "fork") else None
    if pid == 0:
        child()
        os._exit(0)
    if pid is None:
        # Windows / no fork: just sleep as single process (tree demo N/A)
        if marker is not None:
            end = time.time() + sleep_sec
            while time.time() < end:
                if not marker.exists():
                    break
                time.sleep(0.05)
        else:
            time.sleep(sleep_sec)
        return 0
    # parent waits
    os.waitpid(pid, 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
