"""Argv-only subprocess boundary: timeouts, no shell, bounded capture."""
from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Mapping, Sequence


DEFAULT_TIMEOUT_SEC = 15
DEFAULT_MAX_STREAM_BYTES = 64 * 1024
DEFAULT_MAX_STDIN_BYTES = 64 * 1024
_CHILD_ENV_ALLOW = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "USERNAME",
    "USER",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "COMSPEC",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)


@dataclass(frozen=True)
class RunResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    error: str | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    # Limits applied — recorded even when not truncated
    max_stdout_bytes: int = DEFAULT_MAX_STREAM_BYTES
    max_stderr_bytes: int = DEFAULT_MAX_STREAM_BYTES


def decode_cli_bytes(data: bytes) -> str:
    """Decode Windows/WSL CLI output; tolerate UTF-16-LE and embedded NULs."""
    if not data:
        return ""
    if data.startswith(b"\xff\xfe"):
        text = data.decode("utf-16-le", errors="replace")
    elif data.startswith(b"\xfe\xff"):
        text = data.decode("utf-16-be", errors="replace")
    elif b"\x00" in data[:64]:
        try:
            text = data.decode("utf-16-le", errors="replace")
        except Exception:
            text = data.decode("utf-8", errors="replace")
    else:
        text = data.decode("utf-8", errors="replace")
    return text.replace("\x00", "")


def _read_bounded(stream, limit: int, bucket: list) -> None:
    """Read from pipe in chunks; stop storing after limit (still drain to avoid deadlock)."""
    retained = bytearray()
    truncated = False
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            if not truncated:
                room = limit - len(retained)
                if room <= 0:
                    truncated = True
                elif len(chunk) <= room:
                    retained.extend(chunk)
                else:
                    retained.extend(chunk[:room])
                    truncated = True
            # when truncated, continue reading/discarding to prevent pipe fill
    finally:
        try:
            stream.close()
        except Exception:
            pass
    bucket.append((bytes(retained), truncated))



def _child_env(extra: Mapping[str, str] | None) -> dict[str, str] | None:
    """Minimal host env + explicit extras. Avoid forwarding full parent secrets.

    When extra is None, return None so Popen inherits the current process env
    (legacy diagnostic callers). When extra is provided (runner path), start from
    an allowlisted subset then overlay extras; refuse secret-like keys.
    """
    if extra is None:
        return None
    base = {k: os.environ[k] for k in _CHILD_ENV_ALLOW if k in os.environ}
    for k, v in dict(extra).items():
        key = str(k)
        low = key.lower()
        if any(s in low for s in ("token", "secret", "password", "authorization", "api_key", "bearer")):
            raise ValueError("env_key_forbidden")
        base[key] = str(v)
    return base


def run_argv(
    argv: Sequence[str],
    *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    max_stdout_bytes: int = DEFAULT_MAX_STREAM_BYTES,
    max_stderr_bytes: int = DEFAULT_MAX_STREAM_BYTES,
    stdin_bytes: bytes | None = None,
) -> RunResult:
    """Run a fixed argv list. Never uses shell=True.

    Captures stdout/stderr with a hard byte cap *while reading* (not after full buffer).
    On timeout the process is killed; outcome is uncertain for callers.
    """
    args = tuple(str(a) for a in argv)
    if not args:
        return RunResult((), -1, "", "", error="empty_argv")
    if max_stdout_bytes < 0 or max_stderr_bytes < 0:
        return RunResult(args, -1, "", "", error="invalid_stream_limit")
    if stdin_bytes is not None and len(stdin_bytes) > DEFAULT_MAX_STDIN_BYTES:
        return RunResult(args, -1, "", "", error="stdin_too_large")

    try:
        child_env = _child_env(env)
        proc = subprocess.Popen(
            list(args),
            stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=child_env,
            shell=False,
        )
    except FileNotFoundError:
        return RunResult(args, -1, "", "", error="executable_not_found")
    except ValueError as exc:
        return RunResult(args, -1, "", "", error=str(exc) or "env_invalid")
    except OSError as exc:
        return RunResult(args, -1, "", "", error=type(exc).__name__)

    out_bucket: list = []
    err_bucket: list = []
    t_out = threading.Thread(
        target=_read_bounded, args=(proc.stdout, max_stdout_bytes, out_bucket), daemon=True
    )
    t_err = threading.Thread(
        target=_read_bounded, args=(proc.stderr, max_stderr_bytes, err_bucket), daemon=True
    )
    t_out.start()
    t_err.start()

    timed_out = False
    try:
        if stdin_bytes is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_bytes)
            finally:
                proc.stdin.close()
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    finally:
        t_out.join(timeout=5)
        t_err.join(timeout=5)

    out_b, out_tr = out_bucket[0] if out_bucket else (b"", False)
    err_b, err_tr = err_bucket[0] if err_bucket else (b"", False)
    code = int(proc.returncode) if proc.returncode is not None else -1

    return RunResult(
        args,
        code,
        decode_cli_bytes(out_b),
        decode_cli_bytes(err_b),
        timed_out=timed_out,
        error="timeout" if timed_out else None,
        stdout_truncated=out_tr,
        stderr_truncated=err_tr,
        max_stdout_bytes=max_stdout_bytes,
        max_stderr_bytes=max_stderr_bytes,
    )
