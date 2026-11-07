# ACP client prototype (v0.26.0), internal only

ACP is the Agent Client Protocol (https://agentclientprotocol.com/protocol/prompt-turn):
JSON-RPC 2.0, one object per line, over `grok agent stdio`. Grok's guide:
grok-build `docs/user-guide/15-agent-mode.md`.

`grok_bot_build_bridge.acp_client` is a library only. There is no CLI, no MCP access, no
listener, stdio only, and it is not a coding agent.

| Area | Behaviour (tested against a mock agent subprocess) |
|---|---|
| Framing | Each line must be one JSON-RPC 2.0 object of at most 1 MiB. Log noise, a non-JSON line or an oversized line aborts the session (`stdout_contamination`, `line_too_large`). |
| IDs | Responses must match an outstanding id; an unknown or out-of-order id aborts. |
| Capabilities | The client advertises `fs` read/write false and `terminal` false. |
| Agent→client requests | `session/request_permission` always gets a reject option, or `cancelled` if none is offered. `fs/*`, `terminal/*` and unknown methods get -32601. |
| Streaming | `agent_message_chunk` text is capped at 16 000 chars. Tool calls and statuses are counted. |
| Deadlines | Each request has a timeout. On a prompt deadline the client sends `session/cancel`, then waits for `stopReason: cancelled`, else `timeout_uncertain`. |
| Process | Fixed argv, `shell=False`. Cleanup closes stdin, then terminates and kills only its own child. |

## Why no real ACP run yet (gap, not a bypass)
`grok agent --help` and `grok agent stdio --help` on **1.0.77** (read 2026-11-07) list only
`-m/--model`, `--reasoning-effort`, `--always-approve`, `--agent-profile`, `--plugin-dir`,
leader, URL and debug options. There is **no `--sandbox`, `--deny`, `--disallowed-tools`,
`--permission-mode` or `--max-turns`**.

The client's permission rejection only helps for tools the agent actually asks about; that
is the agent's policy and unproven. So an ACP turn would have weaker protections than the
headless plan. Per policy it was not run against the real model.

The ACP docs list `session/set_config_option` (model), `_meta` options and an agent profile.
Whether any of these give equivalent enforcement (for example a profile with deny rules) is a
follow-up to verify from the docs. Model support exists: `grok agent -m LM-Studio stdio`.

## v0.27.0 — protection-equivalence check (observed 2026-11-07, Grok CLI 1.0.77)

- Observed (host, help-only, no model call): `grok agent --deny MCPTool stdio --help` → `error: unexpected argument '--deny'`. `grok agent --help` lists no deny, sandbox, permission-mode, disallowed-tools or max-turns flags.
- Documented (xai-grok-agent README): `--agent-profile` frontmatter `tools: []`, `disallowedTools`, `permissionMode: dontAsk`, `agentsMd`; unknown fields silently ignored. `GROK_SANDBOX` env selects the sandbox.
- Not available for ACP without out-of-scope config writes: the deny-rule layer (global config, or project config that needs folder trust) and a turn cap.
- Result: ACP is **not** protection-equivalent to the headless ask plan. Live ACP stays gated.
- `acp_launch.build_acp_launch_plan()` builds the would-be argv and profile, parses the installed agent help, and sets `live_allowed=False` with gaps `deny_rule_layer`, `turn_cap`. It opens only when the help lists `--deny` and `--max-turns`, and then adds them. It has no CLI and never runs anything. Mock-tested only; there was no real ACP turn.
