"""Immutable approved execution plans — fail closed, no flag injection."""
from __future__ import annotations

import os
import sys
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..models import BackendId

MAX_STDIN_BYTES = 64 * 1024

FORBIDDEN_FLAG_SUBSTRINGS = (
    "--always-approve",
    "bypasspermissions",
    "--permission-mode=bypass",
    "bypassPermissions",
)

_SAFE_PERM = frozenset({"default", "acceptEdits", "plan", "dontAsk", "auto"})
_EXE_NAME = re.compile(r"^(grok|grok\.exe|grok\.cmd|grok\.ps1)$", re.IGNORECASE)


class PlanError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def redact_argv_preview(argv: Sequence[str]) -> tuple[str, ...]:
    """Public/log-safe argv preview — never echo --single prompt payloads."""
    out: list[str] = []
    hide_next = False
    for tok in argv:
        if hide_next:
            out.append("<redacted_prompt>")
            hide_next = False
            continue
        if tok == "--single":
            out.append(tok)
            hide_next = True
            continue
        out.append(str(tok))
    return tuple(out)



@dataclass(frozen=True)
class ApprovedExecutionPlan:
    """Operator-approved, immutable run contract.

    Prompt is NOT placed on argv by default (privacy). When prompt_via_stdin
    is True, bytes are provided on stdin; otherwise prompt is omitted from the
    process invocation (fixture/dummy mode). Truthful: real grok --single puts
    prompt on argv — this bridge prefers stdin/file for privacy when executing
    through the runner; until grok stdin contract is verified, production grok
    path remains dry-run oriented / uses explicit argv only if force_prompt_argv.
    """

    task_id: str
    backend: BackendId
    cwd: str
    executable: str
    argv: tuple[str, ...]
    timeout_sec: float
    max_turns: int | None
    permission_mode: str | None
    allow_rules: tuple[str, ...]
    deny_rules: tuple[str, ...]
    stdin_bytes: bytes | None
    env: dict[str, str]
    prompt_via_stdin: bool
    force_prompt_argv: bool


def _reject_forbidden(tokens: Sequence[str]) -> None:
    for tok in tokens:
        low = tok.lower()
        for bad in FORBIDDEN_FLAG_SUBSTRINGS:
            if bad.lower() in low:
                raise PlanError("forbidden_flag", f"forbidden flag/token rejected: {bad}")


def _validate_executable_path(path: str) -> str:
    if not path or not path.strip():
        raise PlanError("executable_missing", "executable path required")
    p = path.strip()
    if any(ch in p for ch in ("|", ";", "&", ">", "<", "`", "\n", "\r")):
        raise PlanError("executable_invalid", "metacharacters in executable path")
    name = Path(p).name
    # Allow dummy test executables named bridge-dummy* as well as grok*
    if not (_EXE_NAME.match(name) or name.startswith("bridge-dummy")):
        raise PlanError(
            "executable_not_allowed",
            "executable basename must be grok[.exe|.cmd|.ps1] or bridge-dummy*",
        )
    low_name = name.lower()
    if low_name.endswith(".cmd") or low_name.endswith(".bat"):
        raise PlanError(
            "cmd_bat_refused",
            "grok.cmd/.bat refused due to cmd.exe argument reinterpretation risk",
        )
    if not os.path.isfile(p) and not name.startswith("bridge-dummy"):
        # For grok, require exists; dummy may be script path that exists in tests
        raise PlanError("executable_not_found", "executable path does not exist")
    if not os.path.isfile(p):
        raise PlanError("executable_not_found", "executable path does not exist")
    return p


def build_native_invocation(executable: str, extra: Sequence[str]) -> tuple[str, ...]:
    """Build argv for native Windows helpers without shell injection."""
    _reject_forbidden(extra)
    exe = _validate_executable_path(executable)
    low = exe.lower()
    if low.endswith(".ps1"):
        # -File takes the script path as a single literal argument
        return (
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            exe,
            *tuple(str(x) for x in extra),
        )
    if low.endswith(".cmd") or low.endswith(".bat"):
        raise PlanError(
            "cmd_bat_refused",
            "grok.cmd/.bat refused; use grok.exe or grok.ps1 via -File (no cmd.exe)",
        )
    for tok in extra:
        s = str(tok)
        if any(ch in s for ch in ("|", ";", "&", ">", "<", "`", "%")):
            raise PlanError("argv_metachar", "metacharacters in argv token refused")
        if any(ord(ch) < 32 for ch in s):
            raise PlanError("argv_control_char", "control characters in argv refused")
    return (exe, *tuple(str(x) for x in extra))


def build_approved_plan(
    *,
    task_id: str,
    backend: BackendId,
    cwd: str,
    executable: str,
    max_turns: int | None = None,
    permission_mode: str | None = None,
    allow_rules: Sequence[str] = (),
    deny_rules: Sequence[str] = (),
    timeout_sec: float = 60.0,
    prompt: str | None = None,
    prompt_via_stdin: bool = True,
    force_prompt_argv: bool = False,
    extra_args: Sequence[str] = (),
) -> ApprovedExecutionPlan:
    if not task_id.strip():
        raise PlanError("task_id_empty", "task_id required")
    if not cwd or not cwd.strip():
        raise PlanError("cwd_empty", "cwd required")
    if timeout_sec <= 0 or timeout_sec > 3600:
        raise PlanError("timeout_invalid", "timeout_sec out of bounds")
    if max_turns is not None and max_turns < 1:
        raise PlanError("max_turns_invalid", "max_turns must be >= 1")
    if permission_mode is not None and permission_mode not in _SAFE_PERM:
        raise PlanError("permission_mode_forbidden", "permission_mode not allowed")

    _reject_forbidden(list(extra_args) + list(allow_rules) + list(deny_rules))
    if force_prompt_argv and prompt_via_stdin:
        raise PlanError("prompt_mode_conflict", "cannot force argv and stdin together")

    exe = _validate_executable_path(executable)
    tokens: list[str] = []
    # Dummy/bridge mode: no --single unless force_prompt_argv for grok-like surface
    if force_prompt_argv:
        if not prompt:
            raise PlanError("prompt_required", "prompt required for force_prompt_argv")
        tokens.extend(["--single", prompt])
        stdin = None
        via_stdin = False
    elif prompt_via_stdin and prompt is not None:
        stdin = prompt.encode("utf-8")
        if len(stdin) > MAX_STDIN_BYTES:
            raise PlanError("prompt_too_large", "stdin prompt exceeds bound")
        via_stdin = True
    else:
        stdin = None
        via_stdin = False

    if cwd:
        tokens.extend(["--cwd", cwd.strip()])
    if max_turns is not None:
        tokens.extend(["--max-turns", str(max_turns)])
    if permission_mode:
        tokens.extend(["--permission-mode", permission_mode])
    for rule in allow_rules:
        tokens.extend(["--allow", str(rule)])
    for rule in deny_rules:
        tokens.extend(["--deny", str(rule)])
    tokens.extend(str(x) for x in extra_args)
    _reject_forbidden(tokens)

    name = Path(exe).name
    needs_py_launcher = name.endswith(".py") or (
        name.startswith("bridge-dummy") and sys.platform.startswith("win")
    )
    if needs_py_launcher and not name.endswith(".ps1"):
        argv = (sys.executable, exe, *tokens)
    elif backend == "native":
        argv = build_native_invocation(exe, tokens)
    else:
        # WSL/Linux: direct executable path as argv0 (shebang scripts OK on POSIX)
        argv = (exe, *tokens)

    return ApprovedExecutionPlan(
        task_id=task_id.strip(),
        backend=backend,
        cwd=cwd.strip(),
        executable=exe,
        argv=argv,
        timeout_sec=float(timeout_sec),
        max_turns=max_turns,
        permission_mode=permission_mode,
        allow_rules=tuple(allow_rules),
        deny_rules=tuple(deny_rules),
        stdin_bytes=stdin,
        env={"GROK_BOT_BUILD_BRIDGE_TASK_ID": task_id.strip()},
        prompt_via_stdin=via_stdin,
        force_prompt_argv=force_prompt_argv,
    )
