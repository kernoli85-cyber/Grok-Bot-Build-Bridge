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
    # Truthful separation: plan backend (operator-selected) vs how the process is
    # actually spawned. "wsl" plan + "host_python" transport is NOT WSL execution.
    execution_transport: str = "host_direct"
    execution_kind: str = "unspecified"
    wsl_distro: str | None = None
    wsl_user: str | None = None
    parse_grok_json: bool = False
    expected_text: str | None = None
    return_answer: bool = False  # transient console display only; never persisted
    # Owner-declared modelUsage keys for the -m model; None = no -m passed (implicit default).
    expected_models: tuple[str, ...] | None = None


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



def _reject_structural_argv_metachar(tokens: Sequence[str]) -> None:
    """Refuse shell metacharacters in structural argv tokens.

    The freeform prompt immediately after ``--single`` is exempt (prompts may
    contain ``&`` / ``|`` etc.). Control characters are always refused.
    ``%`` is not banned: ``.cmd``/``.bat`` are already refused, and argv-only
    CreateProcess does not expand percent-vars.
    """
    skip_next = False
    for tok in tokens:
        s = str(tok)
        if any(ord(ch) < 32 for ch in s):
            raise PlanError("argv_control_char", "control characters in argv refused")
        if skip_next:
            skip_next = False
            continue
        if s == "--single":
            skip_next = True
            continue
        if any(ch in s for ch in ("|", ";", "&", ">", "<", "`")):
            raise PlanError("argv_metachar", "metacharacters in structural argv refused")


def build_native_invocation(executable: str, extra: Sequence[str]) -> tuple[str, ...]:
    """Build argv for native Windows helpers without shell injection."""
    _reject_forbidden(extra)
    exe = _validate_executable_path(executable)
    _reject_structural_argv_metachar(extra)
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
    is_dummy = name.startswith("bridge-dummy")
    needs_py_launcher = name.endswith(".py") or (
        is_dummy and sys.platform.startswith("win")
    )
    if needs_py_launcher and not name.endswith(".ps1"):
        argv = (sys.executable, exe, *tokens)
        transport = "host_python"
    elif backend == "native":
        argv = build_native_invocation(exe, tokens)
        transport = "host_direct"
    else:
        if sys.platform.startswith("win") and not is_dummy:
            # A Linux grok path cannot be spawned directly from Windows; the
            # previous behaviour silently ran on the host. Fail closed.
            raise PlanError(
                "wsl_transport_required",
                "backend=wsl on Windows requires the wsl.exe transport "
                "(build_wsl_exec_plan); direct host spawn refused",
            )
        # POSIX host (e.g. Bot BOX / CI Linux): direct spawn - NOT inside WSL.
        argv = (exe, *tokens)
        transport = "host_direct_posix"
    kind = "dummy" if is_dummy else "grok"

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
        execution_transport=transport,
        execution_kind=kind,
    )


_POSIX_CWD = re.compile(r"^/[A-Za-z0-9._/@+-]*$")
WSL_PROBE_EXECUTABLE = "/usr/bin/printf"


def build_wsl_probe_plan(
    *,
    task_id: str,
    distro: str,
    user: str,
    cwd: str,
    timeout_sec: float = 20.0,
) -> ApprovedExecutionPlan:
    """Fixed, read-only WSL transport probe: ``/usr/bin/printf`` inside a RUNNING distro.

    Proves the wsl.exe transport + job/outbox lifecycle only. It is NOT a Grok run
    and must never be reported as Grok success. No file writes, no shell, no
    operator-supplied command. Caller must verify the distro is Running first
    (``require_wsl_distro_running``); wsl.exe would otherwise start it.
    """
    from ..discovery import is_safe_identity

    if not task_id.strip() or not is_safe_identity(task_id.strip()):
        raise PlanError("task_id_invalid", "task_id must be a safe identity")
    if not is_safe_identity(distro):
        raise PlanError("wsl_distro_invalid", "unsafe distro name")
    if not is_safe_identity(user):
        raise PlanError("wsl_user_invalid", "unsafe user name")
    if user == "root":
        raise PlanError("wsl_root_refused", "root user refused; use the discovered non-root user")
    c = (cwd or "").strip()
    if not c or not _POSIX_CWD.match(c) or ".." in c.split("/"):
        raise PlanError("cwd_invalid", "WSL cwd must be a safe absolute POSIX path")
    if timeout_sec <= 0 or timeout_sec > 120:
        raise PlanError("timeout_invalid", "probe timeout out of bounds")
    tid = task_id.strip()
    argv = (
        "wsl.exe",
        "-d",
        distro,
        "-u",
        user,
        "--cd",
        c,
        "--exec",
        WSL_PROBE_EXECUTABLE,
        "bridge-wsl-probe task=%s\\n",
        tid,
    )
    return ApprovedExecutionPlan(
        task_id=tid,
        backend="wsl",
        cwd=c,
        executable=WSL_PROBE_EXECUTABLE,
        argv=argv,
        timeout_sec=float(timeout_sec),
        max_turns=None,
        permission_mode=None,
        allow_rules=(),
        deny_rules=(),
        stdin_bytes=None,
        env={"GROK_BOT_BUILD_BRIDGE_TASK_ID": tid},
        prompt_via_stdin=False,
        force_prompt_argv=False,
        execution_transport="wsl_exe",
        execution_kind="wsl_printf_probe",
        wsl_distro=distro,
        wsl_user=user,
    )


def require_wsl_distro_running(distro: str, wsl_list_text: str) -> None:
    """Fail closed unless ``wsl -l -v`` shows ``distro`` as Running (never start it)."""
    from ..discovery import parse_wsl_list_verbose

    rows = parse_wsl_list_verbose(wsl_list_text or "")
    for row in rows:
        if row["name"] == distro:
            if row["state"] == "Running":
                return
            raise PlanError(
                "wsl_distro_not_running",
                f"distro {distro!r} is {row['state']}; operator must start it (never auto-start)",
            )
    raise PlanError("wsl_distro_not_listed", f"distro {distro!r} not in wsl -l -v output")


# --- Real Grok one-shot smoke plan via wsl.exe ---
# Flag NAMES verified against `grok --help` of grok 1.0.70 on the operator host
# (2026-11-07). Flag SEMANTICS below come from public main-branch docs, which may
# be newer than 1.0.70 -> labelled unverified until observed in a real run.
GROK_SMOKE_PROMPT = "Antworte nur BRIDGE_OK"
GROK_SMOKE_EXPECTED = "BRIDGE_OK"
GROK_DENY_BUILTIN_TOOLS = (
    "run_terminal_cmd,read_file,grep,list_dir,search_replace,web_search,"
    "web_fetch,todo_write,task,Agent,"
    # Observed on 1.0.77 (2026-11-07, streaming-json available_commands): these were still
    # offered with only the list above. With them added, the observed tool list was [].
    "monitor,search_tool,use_tool,workflow,enter_plan_mode,exit_plan_mode,ask_user_question,"
    "send_feedback,image_gen,image_edit,image_to_video,reference_to_video,write"
)
# Bare-prefix deny rules match ALL invocations of that type (docs); deny beats
# read-only auto-approval and every allow. MCPTool covers MCP meta/server tools,
# which --tools/--disallowed-tools do NOT remove.
GROK_SMOKE_DENY_RULES = ("Bash", "Read", "Edit", "Write", "Grep", "WebFetch", "MCPTool")
# Env passed via /usr/bin/env inside WSL. Only documented switches; they do NOT
# stop servers configured natively in ~/.grok/config.toml from being started.
GROK_SMOKE_ENV = (
    "GROK_MEMORY=0",
    "GROK_CLAUDE_MCPS_ENABLED=false",
    "GROK_CURSOR_MCPS_ENABLED=false",
    "GROK_CLAUDE_SKILLS_ENABLED=false",
    "GROK_CURSOR_SKILLS_ENABLED=false",
    "GROK_DISABLE_AUTOUPDATER=1",
)
GROK_SMOKE_FIXED_FLAGS = (
    "--max-turns", "1",                      # turn cap - NOT a cost/token cap
    "--output-format", "json",               # result carries total_cost_usd/usage (docs)
    "--disallowed-tools", GROK_DENY_BUILTIN_TOOLS,
    "--disable-web-search",
    "--no-subagents",
    "--no-plan",
    "--sandbox", "strict",                   # reads limited to CWD+system+~/.grok; refuses start if kernel policy unavailable (docs)
    "--permission-mode", "dontAsk",          # unmatched calls auto-denied; never bypassPermissions/--always-approve
)
_SMOKE_CWD = re.compile(r"^/tmp/gbbb-smoke-[A-Za-z0-9]{6,32}$")


def build_wsl_grok_smoke_plan(
    *,
    task_id: str,
    distro: str,
    user: str,
    grok_path: str,
    cwd: str,
    timeout_sec: float = 90.0,
) -> ApprovedExecutionPlan:
    """Fixed headless one-shot Grok smoke via wsl.exe. Prompt/flags not operator-editable.

    cwd must be a fresh empty dir /tmp/gbbb-smoke-<random> (never $HOME / a repo).
    Truthful limits: --max-turns caps turns, not spend; grok 1.0.70 has no cost
    cap flag; user-level ~/.grok config (native MCP servers, home rules, skills)
    still loads; timeout kills host wsl.exe, guest cleanup not proven.
    """
    c = (cwd or "").strip()
    if not _SMOKE_CWD.match(c):
        raise PlanError("smoke_cwd_invalid",
                        "smoke cwd must be a fresh /tmp/gbbb-smoke-<random> dir (not home/repo)")
    probe = build_wsl_probe_plan(task_id=task_id, distro=distro, user=user, cwd=c,
                                 timeout_sec=min(timeout_sec, 120.0))
    g = (grok_path or "").strip()
    if not g.startswith("/") or Path(g).name != "grok" or not _POSIX_CWD.match(g):
        raise PlanError("grok_path_invalid", "absolute POSIX path ending in /grok required")
    deny: list[str] = []
    for r in GROK_SMOKE_DENY_RULES:
        deny += ["--deny", r]
    argv = (
        "wsl.exe", "-d", probe.wsl_distro, "-u", probe.wsl_user, "--cd", probe.cwd,
        "--exec", "/usr/bin/env", *GROK_SMOKE_ENV, g,
        "--single", GROK_SMOKE_PROMPT,
        "--cwd", probe.cwd,
        *GROK_SMOKE_FIXED_FLAGS,
        *deny,
    )
    _reject_forbidden(argv)
    return ApprovedExecutionPlan(
        task_id=probe.task_id, backend="wsl", cwd=probe.cwd, executable=g, argv=argv,
        timeout_sec=probe.timeout_sec, max_turns=1, permission_mode="dontAsk",
        allow_rules=(), deny_rules=GROK_SMOKE_DENY_RULES, stdin_bytes=None,
        env={"GROK_BOT_BUILD_BRIDGE_TASK_ID": probe.task_id},
        prompt_via_stdin=False, force_prompt_argv=True,
        execution_transport="wsl_exe", execution_kind="grok_smoke_one_shot",
        wsl_distro=probe.wsl_distro, wsl_user=probe.wsl_user,
        parse_grok_json=True, expected_text="BRIDGE_OK",
    )


# --- Text-only one-shot "ask" via wsl.exe (same isolation as the smoke) ---
# Prompt transport: grok headless does NOT read piped stdin as the prompt (docs main);
# `--prompt-file /dev/stdin` is undocumented upstream and unverified on 1.0.70.
# Therefore the prompt is passed as the argv value of `--single`. It IS visible in
# process listings (Windows wsl.exe command line, Linux /proc/<pid>/cmdline) while
# the call runs. Preview redaction only hides it from bridge output, not the OS.
ASK_MAX_PROMPT_BYTES = 8 * 1024          # far below the 32767-char Windows cmdline limit
ASK_MAX_TURNS_LIMIT = 10
ASK_MAX_TIMEOUT_SEC = 600.0
ASK_LOCAL_MAX_TIMEOUT_SEC = 1800.0  # only for owner-registered custom_local aliases
_ASK_CWD = re.compile(r"^/tmp/gbbb-ask-[A-Za-z0-9]{8,32}$")
_PROMPT_BAD = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def validate_ask_prompt(prompt: str | bytes) -> str:
    """Bounded UTF-8 text prompt; no NUL/control/bidi chars; cannot look like a flag."""
    if isinstance(prompt, bytes):
        if len(prompt) > ASK_MAX_PROMPT_BYTES:
            raise PlanError("prompt_too_large", f"prompt exceeds {ASK_MAX_PROMPT_BYTES} bytes")
        try:
            prompt = prompt.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PlanError("prompt_not_utf8", "prompt must be valid UTF-8") from exc
    if not isinstance(prompt, str):
        raise PlanError("prompt_invalid", "prompt must be text")
    if prompt.startswith("\ufeff"):
        prompt = prompt[1:]
    p = prompt.strip()
    if not p:
        raise PlanError("prompt_empty", "prompt is empty")
    if len(p.encode("utf-8")) > ASK_MAX_PROMPT_BYTES:
        raise PlanError("prompt_too_large", f"prompt exceeds {ASK_MAX_PROMPT_BYTES} bytes")
    if _PROMPT_BAD.search(p):
        raise PlanError("prompt_control_chars", "prompt contains control or bidi characters")
    if p.startswith("-"):
        raise PlanError("prompt_flag_like", "prompt must not start with '-' (could be parsed as a flag)")
    return p


# Documented env switches (docs 26-config-reference) that only DISABLE secondary features.
# They do not prove no side-model call happens (initial title generation has no documented
# switch). Used in the one observed 1.0.77 local run.
ASK_EXTRA_ENV = (
    "GROK_TURN_SUMMARY=false",
    "GROK_SESSION_RECAP=false",
    "GROK_TITLE_REFRESH=false",
    "GROK_PROMPT_SUGGESTIONS=false",
)
# Guest-side deadline: coreutils timeout inside WSL terminates grok there (TERM, KILL after
# ASK_GUEST_KILL_GRACE). The host deadline is longer, so normally the guest stops first;
# a host-side kill still yields timeout_uncertain / needs_review, never a retry.
ASK_GUEST_TIMEOUT = "/usr/bin/timeout"
ASK_GUEST_KILL_GRACE = 10
ASK_HOST_DEADLINE_MARGIN = 20.0


def build_wsl_grok_ask_plan(
    *,
    task_id: str,
    distro: str,
    user: str,
    grok_path: str,
    cwd: str,
    prompt: str,
    max_turns: int = 1,
    timeout_sec: float = 180.0,
    model: "ModelChoice | None" = None,
    native_structured: tuple[str, str] | None = None,
) -> ApprovedExecutionPlan:
    """Text-only single prompt via wsl.exe with the smoke's isolation flags/deny rules.

    Not a coding agent: all builtin tools denied, dontAsk, strict sandbox, fresh empty
    cwd /tmp/gbbb-ask-<random>. Generic answers: no fixed expected text.
    """
    c = (cwd or "").strip()
    if not _ASK_CWD.match(c):
        raise PlanError("ask_cwd_invalid", "ask cwd must be a fresh /tmp/gbbb-ask-<random> dir")
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or not 1 <= max_turns <= ASK_MAX_TURNS_LIMIT:
        raise PlanError("max_turns_invalid", f"max_turns must be an int 1..{ASK_MAX_TURNS_LIMIT}")
    t = float(timeout_sec)
    ceiling = ASK_MAX_TIMEOUT_SEC if model is None else min(model.timeout_ceiling_sec, ASK_LOCAL_MAX_TIMEOUT_SEC)
    if not (t > 0 and t <= ceiling):
        raise PlanError("timeout_invalid", f"timeout must be 0..{ceiling:g}s")
    model_flags: tuple[str, ...] = ()
    if model is not None and model.model_id is not None:
        from ..model_select import ModelSelectionError, validate_model_id
        try:
            model_flags = ("--model", validate_model_id(model.model_id))
        except ModelSelectionError as exc:
            raise PlanError(exc.code, exc.message) from None
    p = validate_ask_prompt(prompt)
    smoke = build_wsl_grok_smoke_plan(
        task_id=task_id, distro=distro, user=user, grok_path=grok_path,
        cwd="/tmp/gbbb-smoke-placeholder0", timeout_sec=min(t, 120.0),
    )
    flags = list(GROK_SMOKE_FIXED_FLAGS)
    flags[flags.index("--max-turns") + 1] = str(max_turns)
    deny: list[str] = []
    for r in GROK_SMOKE_DENY_RULES:
        deny += ["--deny", r]
    guest = str(int(t)) if float(int(t)) == t else f"{t:g}"
    head = ("wsl.exe", "-d", smoke.wsl_distro, "-u", smoke.wsl_user, "--cd", c,
            "--exec", ASK_GUEST_TIMEOUT, "-k", str(ASK_GUEST_KILL_GRACE), guest,
            "/usr/bin/env", *GROK_SMOKE_ENV, *ASK_EXTRA_ENV, smoke.executable)
    tail = ("--cwd", c, *model_flags, *flags, *deny)
    native_argv: tuple[str, ...] = ()
    if native_structured is not None:
        system, schema = native_structured
        for label, value in (("system", system), ("schema", schema)):
            if not isinstance(value, str) or not value or any(ord(ch) < 32 for ch in value):
                raise PlanError("native_structured_invalid", f"{label} text is empty or has control characters")
            if any(ch in value for ch in ("|", ";", "&", ">", "<", "`")):
                raise PlanError("native_structured_invalid", f"{label} text has shell metacharacters")
        native_argv = ("--verbatim", "--system-prompt-override", system, "--json-schema", schema)
    _reject_forbidden(head + tail + native_argv)  # prompt and schema values are checked separately
    argv = (*head, *native_argv, "--single", p, *tail)
    return ApprovedExecutionPlan(
        task_id=smoke.task_id, backend="wsl", cwd=c, executable=smoke.executable, argv=argv,
        timeout_sec=t + ASK_GUEST_KILL_GRACE + ASK_HOST_DEADLINE_MARGIN, max_turns=max_turns, permission_mode="dontAsk",
        allow_rules=(), deny_rules=GROK_SMOKE_DENY_RULES, stdin_bytes=None,
        env={"GROK_BOT_BUILD_BRIDGE_TASK_ID": smoke.task_id},
        prompt_via_stdin=False, force_prompt_argv=True,
        execution_transport="wsl_exe", execution_kind="grok_ask_text_one_shot",
        wsl_distro=smoke.wsl_distro, wsl_user=smoke.wsl_user,
        parse_grok_json=True, expected_text=None,
        expected_models=(model.expect_usage_keys if model_flags else None),
    )


def build_wsl_mktemp_ask_cwd_argv(distro: str, user: str) -> tuple[str, ...]:
    """Fixed argv creating a fresh 0700 dir via mktemp inside a RUNNING distro."""
    probe = build_wsl_probe_plan(task_id="mktemp", distro=distro, user=user, cwd="/tmp")
    return ("wsl.exe", "-d", probe.wsl_distro, "-u", probe.wsl_user, "--cd", "/tmp",
            "--exec", "/usr/bin/mktemp", "-d", "/tmp/gbbb-ask-XXXXXXXXXXXX")


def build_wsl_verify_ask_cwd_argv(distro: str, user: str, cwd: str) -> tuple[str, ...]:
    """Fixed read-only check: real dir (not symlink), empty, owned by user, not g/o-writable.

    find's default -P never follows a symlinked start point, so a symlink fails -type d.
    Output must equal the path exactly to pass.
    """
    if not _ASK_CWD.match(cwd or ""):
        raise PlanError("ask_cwd_invalid", "ask cwd must be /tmp/gbbb-ask-<random>")
    probe = build_wsl_probe_plan(task_id="verify", distro=distro, user=user, cwd="/tmp")
    return ("wsl.exe", "-d", probe.wsl_distro, "-u", probe.wsl_user, "--cd", "/tmp",
            "--exec", "/usr/bin/find", cwd, "-maxdepth", "0", "-type", "d", "-empty",
            "-user", probe.wsl_user, "!", "-perm", "/022", "-print")
