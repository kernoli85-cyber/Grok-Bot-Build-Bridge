"""Argv-only subprocess boundary: timeouts, no shell, structured diagnostics."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Mapping, Sequence


DEFAULT_TIMEOUT_SEC = 15


@dataclass(frozen=True)
class RunResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None  # FileNotFoundError, OSError class name, etc.


def decode_cli_bytes(data: bytes) -> str:
    """Decode Windows/WSL CLI output; tolerate UTF-16-LE and embedded NULs."""
    if not data:
        return ""
    # UTF-16-LE BOM
    if data.startswith(b"\xff\xfe"):
        text = data.decode("utf-16-le", errors="replace")
    elif data.startswith(b"\xfe\xff"):
        text = data.decode("utf-16-be", errors="replace")
    elif b"\x00" in data[:64]:
        # Likely UTF-16 without BOM (common for `wsl -l`)
        try:
            text = data.decode("utf-16-le", errors="replace")
        except Exception:
            text = data.decode("utf-8", errors="replace")
    else:
        text = data.decode("utf-8", errors="replace")
    # Strip NULs left over from mis-decodes
    return text.replace("\x00", "")


def run_argv(
    argv: Sequence[str],
    *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> RunResult:
    """Run a fixed argv list. Never uses shell=True. Never interpolates a command string."""
    args = tuple(str(a) for a in argv)
    if not args:
        return RunResult((), -1, "", "", error="empty_argv")
    # Refuse shell metacharacters sneaking into a single "command" slot — callers
    # must pass discrete argv tokens. Spaces inside a token (paths) are OK.
    try:
        completed = subprocess.run(
            list(args),
            capture_output=True,
            timeout=timeout_sec,
            check=False,
            shell=False,
            cwd=cwd,
            env=None if env is None else {**os.environ, **dict(env)},
        )
    except subprocess.TimeoutExpired as exc:
        out = decode_cli_bytes(exc.stdout or b"")
        err = decode_cli_bytes(exc.stderr or b"")
        return RunResult(args, -1, out, err, timed_out=True, error="timeout")
    except FileNotFoundError:
        return RunResult(args, -1, "", "", error="executable_not_found")
    except OSError as exc:
        return RunResult(args, -1, "", "", error=type(exc).__name__)

    return RunResult(
        args,
        int(completed.returncode),
        decode_cli_bytes(completed.stdout or b""),
        decode_cli_bytes(completed.stderr or b""),
    )
