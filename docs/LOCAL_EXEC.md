# Local execution workflow (no extra accounts)

**Non‑negotiable:** no Tailscale / no additional end‑user service account. Use the existing Grok Bot desktop registration (`ListMachines` + approved `Shell`/`Copy*` with `machineId`).

## Runtime separation

| Location | Role |
|----------|------|
| **Bot BOX (Linux)** | Agent workspace, repo checkout, wheel **build**, draft skill text. Not the user's Windows/WSL Build host. |
| **Selected Windows desktop** | Where the companion wheel runs after **approved** copy/install. Local discovery of native/WSL Build. |

Same Grok Bot account ≠ always‑allow. A denied approval means **stop** (no privilege reroute). If `ListMachines` shows `connected=false`, local exec is **honestly blocked** until the desktop app is open and online.

## Artifacts (distributable)

| Path | Purpose |
|------|---------|
| `scripts/host-prereq-check.ps1` | **First proof** on Windows: built‑in commands only; works **before** the wheel is installed; does **not** assert companion version. |
| Packaged copy: `grok_bot_build_bridge` → `resources/scripts/host-prereq-check.ps1` | Same script inside the wheel (`host-prereq-script` CLI). |
| Wheel `grok_bot_build_bridge-*-py3-none-any.whl` | Companion CLI (discover/onboard/host-prereq/…). Built in CI / on BOX; **copied** to the host after approval - repo stays on BOX. |
| `drafts/skills/grok-bot-build-bridge-einrichtung/SKILL.md` | Portable Bot recipe (not marketplace‑installed by this PR). |

**Evidence separation:** `host-prereq --fixture windows-*` is **CI/demo only**, not live host evidence. Installed‑wheel smoke tests prove packaging; they do **not** prove the operator's desktop.

## Script guarantees (v0.15.1+)

- Enforces Python **>= 3.11** (`meets_min`).
- Retains launcher argv prefix / resolves `sys.executable` for venv (no bare `py.exe -c` without `-3`).
- Skips WindowsApps store aliases; bounded timeouts on all probes.
- WSL `-l -v` only; UTF-16/NUL decode; English states only; spaced/localized lines skipped (not ready).

## First host proof (read‑only, package may be absent)

After the parent obtains normal local‑exec approval on the selected `machineId`:

```powershell
# Copy approved artifact to a temp path on the desktop, then:
powershell -NoProfile -NonInteractive -File .\host-prereq-check.ps1
```

Expect JSON with `python.meets_min` for 3.11+, `companion_cli.status = absent` if the wheel is not installed yet - that is success for first proof, **not** a version assertion failure.

Optional after wheel install (still approved commands only):

```text
grok-bot-build-bridge host-prereq
```

## Install / uninstall (operator, isolated venv)

**Windows evidence (operator machine, 2026-11-06 Europe/Berlin):** isolated venv at
`%LOCALAPPDATA%\\GrokBotBuildBridge\\venv` with wheel `grok_bot_build_bridge-0.15.2-py3-none-any.whl`
(SHA256 `6876ce77f345a9f64a9790bfca5fa4f382528b6510d0b0e6ae1a2ccbd532a94d`). Companion is
**venv-local** (`Scripts\\grok-bot-build-bridge.exe`); `host-prereq` companion probe is PATH-only,
so status may remain `absent` unless that Scripts dir is on PATH - use the venv entrypoint explicitly.
Live `discover` / `onboard --desired unset --live` do not write preferences.

**No** `curl | sh`, **no** global `pip install` recommendation, **no** secrets in the bundle.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install <approved-path>\grok_bot_build_bridge-VERSION-py3-none-any.whl
.\.venv\Scripts\grok-bot-build-bridge host-prereq
.\.venv\Scripts\grok-bot-build-bridge onboard --desired unset
```

Uninstall (venv only; does not delete `preferences.json` / `webhook.json`):

```powershell
.\.venv\Scripts\python -m pip uninstall -y grok-bot-build-bridge
```

## Routing / WSL / webhook

- Ask **desired** = `native` | `wsl` | `both`; if `both`, also ask **default_backend** = `native` | `wsl`.
- Never start stopped WSL distros; ask the operator.
- Standalone **webhook** prototype remains a **separate one‑way** notify path (WSL/host → Bot); not bidirectional job control and not a background watcher.

## Skill vs global install

The draft skill is a **portable recipe** for a parent‑owned private skill evaluation. This PR does **not** call global skill install, AddMcpServer, merge, or publication.

## Execution backend vs plan backend (v0.16.0)

`runner-run` now reports `plan_backend`, `execution_transport`, `execution_kind`.

| transport | meaning |
|-----------|---------|
| `wsl_exe` | spawned **inside WSL** via `wsl.exe -d <distro> -u <user> --cd <cwd> --exec ...` |
| `host_python` / `host_direct_posix` | spawned on the host; a `wsl` plan label here is **not** WSL execution |
| `host_direct` | native Windows spawn |

On Windows, `--backend wsl` with a non-dummy executable is refused (`wsl_transport_required`)
instead of silently spawning on the host.

**WSL transport probe** (not Grok, no file writes, fixed argv):

```text
grok-bot-build-bridge runner-run --state-db <unique temp>.db --task-id <id> \
  --cwd /home/<user> --wsl-probe --wsl-distro Ubuntu --wsl-user <user> [--execute]
```

Dry-run by default. With `--execute` it first reads `wsl.exe -l -v` and refuses unless the
distro is `Running` (never starts it); root refused. Runs `/usr/bin/printf` only;
`grok_executed=false`. Outbox events are then inspectable with `outbox-pump` (dry-run default).
Real Grok jobs through `wsl_exe` are a separate future gate.

## Grok one-shot smoke (v0.16.2)

**Verified on host (grok 1.0.70 `--help`, flag names only):** `--single`, `--max-turns`,
`--output-format json`, `--tools`, `--disallowed-tools`, `--deny`, `--disable-web-search`,
`--no-subagents`, `--no-plan`, `--sandbox <PROFILE>`, `--permission-mode` (incl. `dontAsk`).
No cost/token cap flag. `grok usage` exists for post-hoc spend.

**From public main-branch docs (may be newer than 1.0.70; unverified until a real run):**
- `--tools` is an allowlist, but stock profiles inject optional tools and **MCP meta-tools
  stay on**; an empty/"none" allowlist is **not documented** -> not used.
- Bare deny rules (`--deny MCPTool`, `Bash`, `Read`, `Edit`, `Write`, `Grep`, `WebFetch`)
  match all invocations and beat read-only auto-approval; `dontAsk` auto-denies unmatched calls.
- `--sandbox strict`: reads CWD + system paths + `~/.grok`; refuses to start if the kernel
  policy cannot be applied. `read-only` reads **everywhere** (previous plan; removed).
- Project `AGENTS.md` needs folder trust; **home rules (`~/.grok/rules`, `~/.claude`,
  `~/.cursor`), user skills, and natively configured MCP servers in `~/.grok/config.toml`
  still load** - cwd does not control them. Env switches used: `GROK_MEMORY=0`,
  `GROK_{CLAUDE,CURSOR}_{MCPS,SKILLS}_ENABLED=false`, `GROK_DISABLE_AUTOUPDATER=1`.
  Native MCP servers may still be **started** (processes) even though their tool calls are denied.
- JSON result reports `total_cost_usd` / `usage` after the run.

**Plan:** cwd must be a fresh empty `/tmp/gbbb-smoke-<random>` (home/repo refused).
Dry-run by default; a real call needs `--execute --allow-model-call` (default off) and a
`Running` distro. This is protection by flags/rules, **not** proven isolation.

Recommended read-only pre-check before any model call (no model, needs host approval):
`grok inspect --cwd /tmp/gbbb-smoke-<random>` and `grok mcp list` to see which MCP
servers/skills/rules would load.

## v0.16.3 — bounded Grok JSON result capture

Plans with `parse_grok_json=True` (the WSL smoke plan) parse stdout as one
documented `--output-format json` object (whole stdout; noise ⇒ `not_json`).

- Bound: 64 KiB; truncated, oversize, non-UTF-8, non-object, missing `text` ⇒ unknown, never success.
- Allowlisted fields only: `stopReason` (`^[a-z_]{1,32}$`), `sessionId` (opaque, `^[A-Za-z0-9_-]{1,128}$`),
  `num_turns`, known `usage` integer keys, `total_cost_usd` (finite, 0..1e6, reported exactly), `total_cost_usd_ticks`.
- Absent cost, `cost_is_partial`, `usage_is_incomplete`, NaN/Inf/negative ⇒ cost **unknown** (not free).
- Response text is never stored; only length + sha256. `matched_text` is set only when the
  stripped JSON `text` equals exactly `BRIDGE_OK`.
- `process_exit_ok` (returncode 0) is reported separately from `model_ok` (`end_turn`, not `is_error`).
- Exit 0 with an invalid/mismatched result ⇒ job `needs_review`, error code
  `success_exit_invalid_result` | `model_result_not_ok` | `smoke_text_mismatch`; no automatic rerun.
- Stored in table `job_results` (CREATE IF NOT EXISTS migration, first write wins, ≤4 KiB, no raw stdout).
  Hidden from `jobs-status` unless `--show-result`; never in outbox payloads.

**10:00 CET run (v0.16.2):** the only evidence is process exit 0 (~26 s). The response,
sessionId and cost were discarded by 0.16.2 and are unknown; they were not recovered and not rerun.

## v0.16.4 — atomic result commit, strict schema, exact cost

- Result, terminal status and outbox event are written in **one SQLite transaction**
  (`commit_job_result`) and only by the current, unexpired lease owner of a RUNNING job.
  Cancelled, terminal, needs_review, stale-owner or expired-lease workers commit nothing
  (expired lease flips to needs_review without the result).
- Persistence failure never yields `succeeded`: if the DB is reachable the job becomes
  `needs_review/result_persist_failed`; if not, the CLI reports `status_unknown`
  (no invented state). If COMMIT succeeded but the caller saw an error, the reported
  status is read back from the DB. Never re-executes.
- Stored summaries are strictly validated (`gbbb.grok_result_summary/v1`: exact keys,
  types, bounds, consistency); invalid rows are neither written nor returned. Results live
  in `job_results_v2` (PK task_id+attempt). The unvalidated 0.16.3 `job_results` table is
  left in place and never read.
- `total_cost_usd` is the exact decimal string from the JSON (parsed as Decimal, ≤18
  decimals, no float rounding); ticks are exact integers.
- Tolerated key variants (docs main vs. installed 1.0.70 unknown): `text|result`,
  `stopReason|stop_reason`, `sessionId|session_id`; recorded as `schema_variant`.
  Duplicate JSON keys are rejected.
- `runner-run --parse-grok-json` / `--expect-bridge-ok` for generic jobs; such runs exit
  non-zero unless the result was accepted.
- MCP and outbox never carry result text or session id; `jobs-status` hides results
  unless `--show-result`.
- CI: installed-wheel end-to-end test (dummy JSON producer → durable DB → reopen →
  hidden/shown status → outbox dry-run), Linux and Windows, no model, no network.

### Next bounded plan (toward a usable app)
1. Real job command `grok-bot-build-bridge ask` — one owner-approved prompt, parsed
   result, explicit cost/turn caps, same isolation as the smoke (no model call until approved).
2. Result retrieval via MCP behind an explicit, scoped opt-in (session id still redacted).
3. Webhook send path behind existing `--send` gate with result-free payloads; dry-run first.
4. Host install of the next tagged build only after user approval; one approved smoke to
   confirm the real 1.0.70 JSON shape.

## v0.17.0 — `ask`: text-only single prompt (not a coding agent)

```
grok-bot-build-bridge ask --prompt "…" --wsl-distro Ubuntu --wsl-user <user> \
  --executable /abs/path/to/grok [--max-turns 1..10] [--timeout ≤600] \
  [--execute --allow-model-call [--show-answer] [--state-db PATH]]
```

- **Dry-run default:** no DB, no data dirs, no temp dir, no host/WSL call; prompt is not
  stored or echoed (preview shows `<redacted_prompt>`; only byte length is reported).
- **Gate:** a real call needs `--execute --allow-model-call`. These flags are a technical
  gate only — they do **not** replace the owner's explicit permission for each paid call.
- **Same isolation as the smoke:** wsl.exe into a *Running* distro (never started),
  `/usr/bin/env` switches, all builtin tools disallowed, deny rules incl. MCPTool,
  `--permission-mode dontAsk`, `--sandbox strict`, no subagents/plan/web search.
  Per-job finite `--max-turns` (turn cap, **not** a cost cap; 1.0.70 has no cost-cap flag).
- **Fresh cwd:** `mktemp -d /tmp/gbbb-ask-XXXXXXXXXXXX` via wsl.exe, then a fixed read-only
  `find` check: real directory (symlink rejected), empty, owned by the user, not
  group/world-writable. Never home or a repo. Grok's trust state for that dir is unknown.
- **Prompt transport:** grok headless does not read stdin as prompt (docs); the prompt is the
  argv value of `--single` and is **visible in OS process listings** (Windows command line,
  `/proc/<pid>/cmdline`) while running. Bounded to 8 KiB UTF-8; NUL/control/bidi characters
  and a leading `-` are rejected. `--prompt-file`/`--prompt-stdin` only change how the
  bridge reads it.
- **Answer:** `--show-answer` prints the parsed JSON `text`, bounded to 16 000 chars,
  terminal-control/bidi chars replaced, newlines/Unicode kept — transient only, never in
  DB, outbox or MCP. Hidden if the job was cancelled or the lease lost.
- **Success** = exit 0 + one valid JSON object + `end_turn` + not `is_error`, committed
  atomically with the strict summary. No fixed expected text (BRIDGE_OK is smoke-only).
  Timeout → `needs_review/timeout_uncertain` (guest cleanup unproven); never retried.
- Tests mock the WSL side (labelled) and use a real dummy subprocess for Grok output;
  no real host is exercised. Real 1.0.70 output shape still unverified.

### Next bounded plan
1. MCP: read-only result-metadata tool behind a startup opt-in (no session id, no text, no start/write tools).
2. `ask` progress/status UX: `jobs-status --task-id`, concise human output mode.
3. Webhook result-free events via existing `--send` gate (dry-run first).
4. With the owner's approval only: install 0.17.0 on host + one approved `ask` to confirm the real JSON shape.

## v0.18.0 — read-only status, text output, opt-in MCP result metadata

**Read-only state access** (`state/readonly.py`), used by `jobs-status` and all MCP status tools.
Before 0.18.0 both opened the DB through `JobStore(...)`, whose constructor runs
`CREATE TABLE IF NOT EXISTS`, so a "status" read could migrate (write to) the file. That is fixed.
- A missing DB is reported (`state_db_absent`) and never created. Symlinks, non-SQLite files and
  foreign or too-old schemas give clear error codes; errors never echo paths or raw SQLite text.
- WAL tradeoff: **superseded in v0.19.0** (the heuristic `immutable=1` choice was unsafe;
  see below).
- Result rows are re-validated against the strict v1 schema on read; tampered rows are hidden.

**`jobs-status`**: `--task-id` filters in SQL (an unknown id exits with 3; there is no fallback to
all jobs). `--limit` defaults to 50 and is clamped to 1..500; `--offset` pages the list;
`total_jobs` is reported. `--format text` prints plain sentences. JSON stays the default
(existing keys are kept; `read_only`, `access_mode`, `total_jobs`, `task_id_filter`, `limit`
and `offset` are new).

**`ask --format text`**: plain sentences. A dry run says that nothing was sent or written and
that the prompt will be visible in the process list. Errors are always printed as JSON.

**MCP**
- Status tools no longer return the state DB path, the job cwd or the lease owner.
- The optional tool `bridge_job_result` is registered only when the operator sets
  `GROK_BOT_BUILD_BRIDGE_MCP_RESULTS=1` before the server starts; callers cannot toggle it. When
  it is off, the tool is not listed and calls to it are refused.
- It returns metadata only: schema, parse status, model_ok, stop reason, is_error, turns, usage,
  cost status and expected_match. It never returns the session id, reply text, hash or length,
  the prompt, the cwd or paths.
- The exact reported cost (a decimal string) is included only with
  `GROK_BOT_BUILD_BRIDGE_MCP_RESULTS_COST=1`. An absent cost means unknown, not zero.
- Covered over stdio and over authenticated streamable-HTTP (ASGI). Unauthenticated requests
  get 401. No start, write or cancel tools exist.

**Webhook/outbox:** unchanged and content-free (status, attempt, error_code). Nothing is sent
live; result metadata is not added to events.

### Product scope honesty
The bridge today is a gated, text-only single-prompt runner (`ask`) plus a smoke test, status,
and a read-only MCP facade. It is **not** a coding agent: all of Grok's file, shell and web tools
are denied, and the working directory is an empty temp dir. Arbitrary coding on user repos is a
separate, future, owner-approved design (needs a repo-scoped allowlist, a diff review step and
write approvals). It is not implied by any current flag.

### Next product gap
The real Grok 1.0.70 JSON shape is still unverified. Everything above is tested with dummy
producers only. The next useful step that needs the owner is: install 0.18.0 on the host and run
one approved `ask --format text --show-answer`. Box-side next steps: a `doctor` command that
checks host prerequisites read-only in one report, and a minimal repo-read-only `ask` design
document (no code-writing).


## v0.19.0 — connection lifecycle fix, honest read-only mode, `doctor`

**Connection leak fixed.** `with sqlite3.connect(...)` only scopes a transaction; it does not
close the handle. Six `JobStore` read paths and the schema init leaked connections until garbage
collection, which kept WAL sidecars alive and can block rename/delete on Windows. All paths now
use `_conn()` / `_tx()`, which always close, also on exceptions. A failed `ROLLBACK` no longer
masks the original error. Tests spy on every `sqlite3.connect` (count opened = closed, including
failing transactions and worker heartbeat threads), rename and delete the DB after use, and
check that WAL sidecars disappear after the last close with GC disabled and no forced checkpoint.

**Read-only mode corrected.** v0.18.0 chose `immutable=1` whenever no `-wal` file existed.
That was unsafe: SQLite's immutable flag promises the file never changes, and a writer can start
after the check (TOCTOU). It is now:
- Always `mode=ro` + `PRAGMA query_only`, inside one deferred read transaction per reader, so
  every query sees one coherent snapshot. A test commits result + terminal status + outbox from
  a writer between reader queries; the reader keeps seeing the old state, never a mix.
- Honest effect: the **main DB file is never modified**. Because the DB uses WAL, SQLite **may
  create or update the `-shm`/`-wal` sidecar files** for the reader. This is not a "no filesystem
  changes" mode.
- `immutable=1` is used only with `jobs-status --assume-static-snapshot`, meaning the operator
  has verified the file is a static copy nobody writes. It is never chosen automatically and is
  not available over MCP.

**`doctor`** (read-only companion report): `doctor [--live] [--state-db PATH] [--format text]`.
- The default is **fixture**: packaged demo facts, labelled as not this machine and not the user's PC.
- `--live` runs the existing read-only discovery on the machine running the command. On the
  Grok Bot box that is Linux, and the report says it cannot diagnose a Windows PC.
- Each check reports one of `ok | not_ready | absent | unknown | skipped`: companion version,
  MCP extra, native grok, WSL distros (stopped distros are never started), WSL grok, probe
  timeouts, state DB (read-only, never created) and model auth.
- Model auth is always `unknown`. `capabilities.model_auth_verified` is always false, and
  `model_call_ready` is "unknown". Doctor makes no model, auth, network or secret reads, writes
  no preferences, installs nothing and does not create state.

### Next milestone toward the original goal (secure coding bridge)
A **repo-read-only `ask`**: Grok may read files in one operator-allowlisted repo snapshot
(copied into a fresh temp dir, never the live repo), with read-only tools only (Read/Grep, still
no Bash/Edit/Write/Web/MCP). The answer is returned transiently. After that comes a
**proposed-diff** mode: Grok writes only inside the temp copy, the bridge returns a bounded
unified diff for human review, and nothing is applied to the real repo without an explicit
`apply` step that the owner approves. Both need one approved real call first to confirm the
1.0.70 JSON shape and the tool-denial semantics.
