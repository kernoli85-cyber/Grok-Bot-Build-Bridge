# Grok Bot-Build Bridge

Companion scaffolding that connects **Grok Bot** (orchestration, webhooks, skills) with local **Grok Build** on Windows — **native PowerShell and/or WSL2**.

This package does **not** silently install Grok Build or WSL, does **not** expose an arbitrary remote shell API, and does **not** execute `grok` from the demo CLI commands.

## Compatibility (truthful)

| Backend | Status | Notes |
|--------|--------|-------|
| WSL2 Ubuntu · non-root · grok 1.0.70 | Tested (prior workstream on reference host) | Discovery + webhook notify E2E |
| Native PowerShell Grok Build | Not installed / not validated on reference host | Discovery returns `not_found` |
| Dual (native + WSL) | Unit-tested routing only | Not runtime-validated |

Discovery matrix: `none` | `native_only` | `wsl_only` | `both`. Root-default WSL users are **never** ready.

## What this slice includes

- Pure Python **discovery parsing** (fixtures; no host mutation)
- **Per-job routing** with path validation and **no silent fallback**
- CLI: `version`, `discover-demo`, `plan-demo` (plans only — no installers, no `grok` exec)
- Legacy notify scripts under `legacy/notify/` (HTTPS webhook helper prototype)
- Deterministic unit tests (no network, no paid API)

## Develop

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
python -m grok_bot_build_bridge.cli discover-demo
python -m grok_bot_build_bridge.cli plan-demo
```

## Secrets

Never commit webhook keys, SSH keys, or `webhook.json`. See `.gitignore`.

## Non-goals (this PR)

- Live Windows/WSL probing subprocesses
- Job execution / marketplace plugin packaging
- Shared secrets or credential reads
