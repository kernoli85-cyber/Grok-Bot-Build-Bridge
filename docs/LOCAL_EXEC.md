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
