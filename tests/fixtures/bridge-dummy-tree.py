#!/usr/bin/env python3
"""Harmless dummy process tree for containment demos (no network, no grok).

Parent spawns one grandchild that writes its PID and waits on a marker file.
Uses subprocess on all platforms so Windows Job Object inheritance applies
(grandchild created via CreateProcess joins the parent's job by default).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def _child_main(marker: Path | None, sleep_sec: float, pid_file: Path | None) -> int:
    if pid_file is not None:
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    if marker is not None:
        end = time.time() + sleep_sec
        while time.time() < end:
            if not marker.exists():
                break
            time.sleep(0.05)
    else:
        time.sleep(sleep_sec)
    return 0


def main(argv: list[str]) -> int:
    if "--role" in argv and argv[argv.index("--role") + 1] == "child":
        marker = None
        sleep_sec = 30.0
        pid_file = None
        if "--marker" in argv:
            marker = Path(argv[argv.index("--marker") + 1])
        if "--sleep" in argv:
            sleep_sec = float(argv[argv.index("--sleep") + 1])
        if "--pid-file" in argv:
            pid_file = Path(argv[argv.index("--pid-file") + 1])
        elif os.environ.get("BRIDGE_CHILD_PID_FILE"):
            pid_file = Path(os.environ["BRIDGE_CHILD_PID_FILE"])
        return _child_main(marker, sleep_sec, pid_file)

    marker = None
    sleep_sec = 30.0
    pid_file = None
    if "--marker" in argv:
        marker = Path(argv[argv.index("--marker") + 1])
    if "--sleep" in argv:
        sleep_sec = float(argv[argv.index("--sleep") + 1])
    if "--pid-file" in argv:
        pid_file = Path(argv[argv.index("--pid-file") + 1])
    elif os.environ.get("BRIDGE_CHILD_PID_FILE"):
        pid_file = Path(os.environ["BRIDGE_CHILD_PID_FILE"])

    child_argv = [sys.executable, str(Path(__file__).resolve()), "--role", "child", "--sleep", str(sleep_sec)]
    if marker is not None:
        child_argv.extend(["--marker", str(marker)])
    if pid_file is not None:
        child_argv.extend(["--pid-file", str(pid_file)])

    # New process group/session is NOT required for Windows job inheritance.
    proc = subprocess.Popen(child_argv, shell=False)
    try:
        proc.wait(timeout=sleep_sec + 5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        return 1
    return int(proc.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
