---
name: Grok Bot-Build Bridge Einrichtung
description: Use when setting up or guiding Grok Bot-Build Bridge onboarding (discovery matrix, desired/default backend choices, pairing checklist) without installing software or collecting secrets.
---

# Grok Bot-Build Bridge Einrichtung

**Status:** Draft recipe shipped **in this repository** for evaluation. **Not** marketplace-installable yet. Do **not** treat this file as a globally installed Cursor/Grok Bot skill unless a parent agent/owner installs it separately.

**Product UX:** Bot-guided conversation. The bridge CLI (`onboard`, `prefs-*`) is a **diagnostic companion** only.

## When to use

- Operator asks to set up / pair / choose native vs WSL for Grok Bot-Build Bridge
- You need a checklist from discovery without running installs or grok jobs

## Hard rules

1. Never auto-install WSL, Grok Build, or dependencies; offer links/checklist only.
2. Never start a stopped WSL distro; ask the operator to start it.
3. Never collect, store, or echo webhook bearer tokens / passwords / prompts.
4. Pairing: config file **presence ≠ auth-ready**. Do not claim notify is authenticated.
5. Never execute checklist `action_hint` / plan strings as shell commands.
6. Desired preference and job default are **distinct** from currently discovered `discovery_kind`.
7. Persist preferences only via explicit `prefs-save` (allowlisted non-secret fields); never on read-only `onboard`.

## Steps

1. Obtain an onboarding plan JSON (`schema_version=1`):
   - Prefer Bot-orchestrated facts, or
   - Diagnostic: `python -m grok_bot_build_bridge.cli onboard --desired unset` (fixture) / `--live` (read-only probes).
2. Pass the JSON through the bridge adapter (`parse_onboarding_plan_payload` / `build_conversation_view`). Reject unknown schema, malformed, or oversized payloads.
3. Present checklist items as **display data** (status/title/detail). Do not run hints.
4. If `needs_choice` or desired is unset: ask explicitly for **desired** = `native` | `wsl` | `both`.
5. If desired = `both`, ask explicitly for **default_backend** = `native` | `wsl`.
6. Keep labels clear: **desired** (operator preference) vs **discovered** (current matrix) vs **default** (job default).
7. Optional persistence (non-secret only): explicit save — e.g. CLI `prefs-save --desired … --default-backend … --overwrite` when replacing. Refuse silent clobber.
8. Re-run planning with the chosen desired against **fresh** discovery; if discovery is stale vs preference, surface `stale_hint` / aspirational notes — do not invent ready backends.
9. For notify pairing: point at local `webhook.json` under the user config root convention; never write secrets in this skill; dry-run outbox only unless operator separately requests `--send`.

## Out of scope (future gates)

- Marketplace / plugin packaging
- Bot-managed vault for bearer tokens
- Wiring Job Object kill into production `jobs-cancel` (still state-only)
- Paid / real grok model execution in onboarding

## Companion commands (diagnostic)

- `onboard` — read-only plan JSON
- `prefs-show` / `prefs-save` — allowlisted preferences only
- `discover` / `discover-demo` — discovery facts
- `runner-run` — dry-run default; real spawn only with `--execute` and approved plan (tests use `bridge-dummy`, not paid grok)
