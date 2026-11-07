# Grok Bot-Build Bridge

Companion library/CLI scaffolding that connects **Grok Bot** (orchestration, webhooks, skills) with local **Grok Build** on Windows — **native PowerShell and/or WSL2**.

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
- CLI: `version`, `discover`, `onboard`, `onboard-view`, `prefs-show`/`prefs-save` (live read-only probes), `discover-demo`, `plan-demo` (no installers; demos do not exec `grok` jobs)
- `invoke`: one JSON object on stdin for a text prompt, a bounded file selection, or a review-only replacement proposal, run inside WSL. See `docs/INVOKE.md`. A proposal does not edit files. Windows does not install the package.
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






## Distribution (Slice 10)

- Build **sdist + wheel** via `python -m build` (CI); **no PyPI publish** in this PR
- Isolated venv install smoke on Ubuntu + Windows CI (entry point works outside checkout)
- Packaged fixtures/skill text via `importlib.resources` (no `tests/` dependency after install)
- Manual companion install vs Bot plugin documented in `docs/DISTRIBUTION.md`
- Draft Cursor plugin layout: `drafts/plugin/` — **not** marketplace-submitted; **do not assume** Cursor marketplace = Grok Bot install (see `docs/DISTRIBUTION.md`)

## Bot onboarding adapter + preferences (Slice 9)

- Strict `schema_version=1` plan ingest (`onboard-view`): rejects unknown schema / malformed / oversized; redacts sensitive keys; checklist is **display data** (`treat_as_shell=false`)
- Choice descriptors separate **desired** (operator preference) vs **discovery_kind** vs **default_backend**
- Explicit `prefs-save` / `prefs-show` for allowlisted non-secret preferences only (atomic write; no clobber without `--overwrite`); never auto-written by `onboard`
- Draft skill recipe in-repo: `drafts/skills/grok-bot-build-bridge-einrichtung/SKILL.md` — **not** marketplace-installed; parent/owner decides global install
- No fake auth-ready; no secret collection

## Runner honesty

- `runner-run` default is **dry-run** (no process). `--execute` runs an approved plan.
- Automated tests spawn **`bridge-dummy` / `bridge-dummy-tree` only** — not the real Grok Build CLI and **not** paid model calls.
- Real grok execution remains operator-gated (discovered executable + explicit `--execute`); production cancel remains **state-only**.

## Onboarding planner (Slice 8)

- Reusable `plan_onboarding()` + structured JSON (`schema_version=1`) for future Bot skill
- CLI `onboard` is **dry-run / read-only** diagnostic companion (default fixture facts; `--live` optional)
- Missing `--desired` → `needs_choice` (never silent preference)
- Pairing status via **file existence metadata only** — config present ≠ auth-ready; secrets never read
- No installs, no WSL starts, no webhook POST, no secret writes
- Local `webhook.json` under user config root retained; Bot vault / plugin packaging = future gates

## Windows Job Object demo (Slice 7)

- Demo-only containment for `bridge-dummy-tree` via Job Objects (CREATE_SUSPENDED → assign → resume)
- `TerminateJobObject` / kill-on-close tested on Windows CI; **not** wired to `jobs-cancel` (still state-only)
- Host Job Object ≠ WSL guest cleanup; no `wsl --terminate` / `--shutdown` for per-job cancel
- POSIX process-group demo remains separate and accurate

## Lease heartbeat / cancel honesty (Slice 6)

- `renew_lease` while CLAIMED/RUNNING; **never** resurrects cancelled / terminal / needs_review
- Stale or lease-expired workers **cannot** commit success/failure (→ `needs_review`, no auto-requeue)
- Runner heartbeats during `--execute`; lease loss mid-run → `needs_review`
- `jobs-cancel` is **state-only**; process-tree kill is **not** claimed
- Windows tree kill needs Job Objects (not wired); WSL: killing `wsl.exe` ≠ guest cleanup; `wsl --terminate/--shutdown` are distro/VM-wide (too broad)
- POSIX demo only: process-group stop for intentionally contained `bridge-dummy-tree` fixtures
- Argv preview redacts `--single` prompts; no transcript storage by default

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

## Local-exec host prereq (Slice 15 / v0.15.0)

Portable draft skill + `scripts/host-prereq-check.ps1` (and packaged copy) for **approved** Grok Bot desktop local execution — no Tailscale, no extra accounts. First proof is read-only built-in checks **without** asserting companion version. See `docs/LOCAL_EXEC.md`. Wheel remains the distributable; repo stays on Bot BOX.

## Authenticated MCP streamable HTTP (Slice 14 / v0.14.0)

ASGI app factory with static-bearer auth + Host/Origin allowlists (official MCP SDK Streamable HTTP). CLI `mcp-http-check` only — **no listener**. In-process ASGI tests; see `docs/DISTRIBUTION.md` for deployment choices (private network / tunnel / public HTTPS) that require the user.

## Bounded MCP stdio facade (Slice 13 / v0.13.0)

Optional `[mcp]` extra using the official MCP Python SDK (FastMCP / stdio). Read-only tools: discover, onboard, status, job-status. No shell/execute/enqueue/outbox-send/secret/cancel. Server-scoped state path only. Honest `box_or_linux` runtime marker. Integration tests cover real MCP initialize/list_tools/call_tool over subprocess stdio (including wheel install). **No** `AddMcpServer` registration in this PR — see `docs/DISTRIBUTION.md`.

## DNS-pinned webhook SSRF (Slice 12 / v0.12.0)

Webhook HTTPS now **resolves once** (deadline-bounded), **rejects mixed/non-global** answers (private, loopback, link-local, IPv4-mapped-to-non-global, IPv6 zone), **connects to a pinned approved IP**, and keeps the **original hostname** for TLS SNI, certificate verification, and `Host`. Env proxies and redirects remain suppressed; verification is never disabled. Errors are sanitized codes (no IP/token leak).

Argv metachar rules refined: freeform `--single` prompts may contain `&`/`|`; structural tokens (`--cwd`, etc.) still refuse shell metacharacters; `.cmd`/`.bat` still refused.

## Audit hardening (Slice 11 / v0.11.0)

Security/correctness fixes from a source-level audit (not another feature demo):

- Webhook: refuse loopback/private/link-local/metadata literal hosts; empty `ProxyHandler` so `HTTP(S)_PROXY` cannot exfiltrate Bearer.
- Execution plan: refuse `.cmd`/`.bat` (cmd.exe argument reinterpretation); reject argv metacharacters; bound stdin prompts.
- `safe_exec`: bound stdin; allowlisted child env when extras are provided (runner path); refuse secret-like env keys.
- Preferences: refuse symlink path/parent on load/save.
- Job claim: `UPDATE ... WHERE status=queued` under `BEGIN IMMEDIATE` to fail closed on races.
- Docs: official Grok Bot Connect apps ≠ proven Cursor marketplace install path (see `docs/DISTRIBUTION.md`).
- E2E: fresh wheel → onboard → dummy job → mocked outbox receiver (`tests/test_e2e_wheel_workflow.py`).

### Real runner vs dummy (precise)

| Capability | Status |
|------------|--------|
| Discover native/WSL grok paths | Yes (probes) |
| Approve immutable plan / argv-only exec | Yes |
| Run `bridge-dummy*` fixtures | Yes (CI) |
| Run real `grok` with `--execute` | Code path exists; **not** exercised with paid model in this PR |
| Cancel = kill process tree | **No** — state-only; Job Object / POSIX demos are demos only |
| Outbox webhook POST | Yes (HTTPS, no redirects, mocked in tests) |
| Installable for all Grok Bot users as marketplace plugin | **Not verified** by official Grok Bot docs |

## Non-goals (this PR)

- Marketplace plugin packaging as done product
- Unattended daemon / scheduled loops
- Shared secrets or credential reads
