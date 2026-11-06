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
- CLI: `version`, `discover` (live read-only probes), `discover-demo`, `plan-demo` (no installers; demos do not exec `grok` jobs)
- Legacy notify scripts under `legacy/notify/` (HTTPS webhook helper prototype)
- Argv-only subprocess boundary (`safe_exec`) with timeouts; stopped WSL distros never auto-started
- Deterministic unit tests with mocked subprocess (no network, no paid API)

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

## One-shot runner (Slice 5)

- `runner-run` dry-run by default; `--execute` required to spawn
- Immutable `ApprovedExecutionPlan`; forbids `--always-approve` / bypass modes
- Timeout / lease-while-running ⇒ `needs_review` (**no auto-requeue**)
- Explicit `jobs-retry` only; outbox notification retries stay separate
- Bounded stdout/stderr capture while reading; no transcript storage by default
- Cancel remains **state-only** (no process-tree kill claim)
- Tests use `bridge-dummy` (no paid grok calls)

## Webhook transport (Slice 4)

- HTTPS-only outbox pump (`outbox-pump`); **dry-run by default**
- Explicit `--send` required for real POST; Bearer from private config path (not repo)
- No redirects; default TLS; bounded timeouts/body; sanitized errors
- `http_accepted` ≠ `bot_processed`
- Windows: **no false chmod/ACL secrecy claims**

## Job state / outbox (Slice 3)

- Own SQLite DB (`--state-db` or platform user data dir), mode 0600
- Versioned events (`schema_version=1`), correlation_id / event_id
- Idempotent enqueue, leases, retries with injectable clock/jitter, dead-letter
- `http_accepted` ≠ `bot_processed` (no false exactly-once)
- CLI: `jobs-demo`, `jobs-status`, `outbox-tick` — **simulated** acks only (no webhook POST, no grok)

## Non-goals (this PR)

- Live Windows/WSL probing subprocesses
- Job execution / marketplace plugin packaging
- Shared secrets or credential reads
