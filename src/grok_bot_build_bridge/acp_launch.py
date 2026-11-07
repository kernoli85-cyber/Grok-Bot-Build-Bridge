"""Fail-closed launch gate for a future real `grok agent stdio` session. No CLI, no execution.

Equal-protection requirement (vs the headless ask plan): built-in tool removal, a deny-rule
layer incl. MCPTool, dontAsk, strict sandbox, a turn cap, the documented env off-switches.

What ACP offers on 1.0.77 (help read 2026-11-07; `grok agent --deny ...` -> "unexpected
argument"):
- `--agent-profile <PATH>`: frontmatter `tools: []`, `disallowedTools`, `permissionMode:
  dontAsk`, `agentsMd: false` (xai-grok-agent README, main branch; unknown fields are
  SILENTLY ignored, so a typo weakens protection without error);
- `GROK_SANDBOX=strict` (sandbox guide precedence: flag or env); a built-in profile that fails
  to apply only WARNS and continues;
- `--no-leader`, `-m`.
Missing: deny rules (only via global or folder-trusted project config, both out of scope)
and a max-turns cap. Therefore `live_allowed` is False unless the installed help shows the
missing flags.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .model_select import validate_model_id
from .runner.plan import (ASK_EXTRA_ENV, GROK_DENY_BUILTIN_TOOLS, GROK_SMOKE_DENY_RULES,
                          GROK_SMOKE_ENV, _ASK_CWD)

REQUIRED_FOR_EQUAL = {"--deny": "deny_rule_layer", "--max-turns": "turn_cap"}
PROFILE_NAME = "gbbb-text-only"


@dataclass(frozen=True)
class AcpLaunchPlan:
    argv: tuple[str, ...]
    profile_text: str
    live_allowed: bool
    gaps: tuple[str, ...]


def agent_flags_from_help(help_text: str) -> set[str]:
    return set(re.findall(r"(?m)^\s+(?:-\w, )?(--[a-z][a-z0-9-]*)", help_text or ""))


def profile_text() -> str:
    tools = [t for t in GROK_DENY_BUILTIN_TOOLS.split(",") if t]
    lines = ["---", f"name: {PROFILE_NAME}",
             "description: Bridge text-only profile; no tools.",
             "tools: []",
             "disallowedTools:", *[f"  - {t}" for t in tools],
             "permissionMode: dontAsk",
             "agentsMd: false",
             "skills: []",
             "---",
             "Answer in plain text only. You have no tools."]
    return "\n".join(lines) + "\n"


def build_acp_launch_plan(*, distro: str, user: str, grok_path: str, cwd: str, model: str,
                          profile_path: str, agent_help: str, guest_timeout_sec: int = 900) -> AcpLaunchPlan:
    if not _ASK_CWD.match(cwd or "") or not re.match(r"^/tmp/gbbb-ask-[A-Za-z0-9]{8,32}/gbbb-profile\.md$", profile_path or ""):
        raise ValueError("cwd/profile must be inside a fresh /tmp/gbbb-ask-* dir")
    if not profile_path.startswith(cwd + "/"):
        raise ValueError("profile must live in the job's own cwd")
    if not (re.match(r"^/[A-Za-z0-9._/-]+$", grok_path or "") and ".." not in grok_path):
        raise ValueError("grok path must be absolute")
    if not 1 <= int(guest_timeout_sec) <= 1800:
        raise ValueError("guest timeout out of bounds")
    validate_model_id(model)
    flags = agent_flags_from_help(agent_help)
    gaps = tuple(sorted(v for k, v in REQUIRED_FOR_EQUAL.items() if k not in flags))
    if not {"--agent-profile", "--no-leader", "--model"} <= flags:
        gaps = tuple(sorted(set(gaps) | {"profile_or_model_flags_missing"}))
    extra: list[str] = []
    if "--deny" in flags:
        for r in GROK_SMOKE_DENY_RULES:
            extra += ["--deny", r]
    if "--max-turns" in flags:
        extra += ["--max-turns", "1"]
    argv = ("wsl.exe", "-d", distro, "-u", user, "--cd", cwd, "--exec",
            "/usr/bin/timeout", "-k", "10", str(int(guest_timeout_sec)),
            "/usr/bin/env", *GROK_SMOKE_ENV, *ASK_EXTRA_ENV, "GROK_SANDBOX=strict",
            grok_path, "agent", "--no-leader", "--model", model, "--agent-profile", profile_path,
            *extra, "stdio")
    for bad in ("--always-approve", "--yolo", "bypassPermissions"):
        if bad in argv:
            raise ValueError("forbidden flag")
    return AcpLaunchPlan(argv, profile_text(), live_allowed=not gaps, gaps=gaps)
