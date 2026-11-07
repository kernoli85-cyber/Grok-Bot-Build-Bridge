# Distribution: Python companion vs Bot packaging

## Manual companion install (Python) — supported now

On the operator machine (typically Windows), in an **isolated venv**:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install path/to/grok_bot_build_bridge-*-py3-none-any.whl
grok-bot-build-bridge version
grok-bot-build-bridge onboard --desired unset
```

- Build artifacts are produced in CI (`python -m build`) but **not** published to PyPI in this PR.
- Preferences (`preferences.json`) and webhook config live under the user config root; uninstalling the wheel does not delete them.
- Tests use `bridge-dummy` only; real grok requires discovered executable + explicit `--execute`.

## What official Grok Bot docs say about plugins (verified)

Fetched from official Cursor/Grok Bot help (not third-party blogs):

| Source | What it documents |
|--------|-------------------|
| [Connect plugins / apps](https://cursor.com/help/grok-bot/connect-plugins.md) | Grok Bot **Connect apps** = service connectors (Gmail, Slack, Notion, …). Account-scoped OAuth to external services. |
| [Grok Bot settings](https://cursor.com/docs/grok-bot/settings.md) | Mentions Marketplace for plugins and packaged skills under Grok Bot settings — **does not** document that Cursor IDE Agent Plugins (`.cursor-plugin/plugin.json`) install into Grok Bot. |
| [Cursor Plugins reference](https://cursor.com/docs/plugins) | Cursor IDE/Agent plugin format, Cursor Marketplace submit at cursor.com/marketplace/publish. Scoped to Cursor Customize / IDE. |

**Do not assume** Cursor marketplace plugin = installable Grok Bot extension. Official Grok Bot "plugins/apps" pages describe **connectors**, not this companion’s skill/plugin layout. Any third-party claim that the same Cursor plugin package installs for all Grok Bot users is **unverified** here and must not be treated as product readiness.

## What this repo ships (honest)

1. **Python wheel/CLI companion** — installable via pip into an isolated venv (tested). Discovery, onboarding plan, prefs, job store, dummy runner, webhook outbox.
2. **Draft skill text** — `drafts/skills/.../SKILL.md` and packaged copy under `grok_bot_build_bridge.resources` for parent `UpdateSkill` / local evaluation. **Not** globally installed by this PR.
3. **Draft Cursor plugin scaffold** — `drafts/plugin/.cursor-plugin/plugin.json` matching Cursor plugin shape for *possible* future Cursor-side packaging. **Not** submitted; **not** claimed as Grok Bot marketplace-ready.

## Remaining gates (owner/publisher / product)

- Confirm the **actual** Grok Bot distribution path for a custom skill/plugin that drives local Windows/WSL Build (private skill vs marketplace vs other). Official connector docs do not cover this companion.
- Marketplace/publish actions are external; this PR does not publish.
- Until that path is verified, operators use: (1) Python wheel companion, (2) optional parent-owned private skill from the draft recipe.

## Reproducibility

CI records wheel/sdist filenames and SHA-256 of built artifacts for that run. Bit-for-bit reproducibility across OS/toolchains is **not** claimed unless measured equal hashes are reported for the same commit on both OS jobs (see packaging job logs).


## Webhook SSRF (v0.12+)

Client-side protection only (does not bypass OS/network controls): DNS resolve-once with deadline, reject mixed/non-global answers, TCP pin to approved IP, TLS verify with original hostname (SNI/Host). Residual risk if the OS resolver itself is malicious is out of scope for this library.


## Bounded MCP facade (v0.13+) — stdio on Bot BOX

Optional extra: `pip install 'grok-bot-build-bridge[mcp]'` (official MCP Python SDK `mcp>=1.28,<2`).

```bash
grok-bot-build-bridge mcp-stdio
# or: grok-bot-build-bridge-mcp
```

- **Transport:** stdio only from this entrypoint. No network listener, no accidental bind, no hosting cost.
- **Tools (read-only):** `bridge_runtime_info`, `bridge_discover`, `bridge_onboard`, `bridge_status`, `bridge_job_status`.
- **Not exposed:** shell, write, execute, enqueue, outbox-send, secret-read, cancel.
- **State:** server-scoped via env `GROK_BOT_BUILD_BRIDGE_MCP_STATE_DIR` (absolute directory); DB filename fixed as `state.sqlite`. Tool args cannot pass arbitrary paths. Missing DB is reported; MCP does not create it on query.
- **Runtime honesty:** responses include `runtime_location` (`box_or_linux` / `windows_host` / …). Callers must not assume the user's Windows host. Live discovery on Linux BOX is expected to find none.
- **Stdout:** MCP JSON-RPC wire only; logs go to stderr (sanitized).
- **Authority:** tool outputs are data only — no inheritance of install/execute/send authority from returned JSON.

### AddMcpServer / registration (future — not done in this PR)

Parent-confirmed product capability (remote **HTTPS** URL **or** local **stdio** command/args/env running on the **Bot BOX**, not the user's Windows host; `SearchPlugins` required before add; explicit question widget; `tls.caBundle` for private CA).

- BOX stdio companion tools therefore do **not** require public hosting or Tailscale to expose tools *to agents that share the BOX*.
- Reachability of the **user's Windows/WSL Build** remains a separate problem (hosted path, Tailscale, or webhook/companion on the host).
- This PR does **not** call `AddMcpServer`, mutate account connectors, or register anything. Future account confirmation needed: SearchPlugins → explicit user question widget → AddMcpServer with BOX stdio command pointing at `grok-bot-build-bridge-mcp` (or remote HTTPS only if safely hosted without accidental bind).

This executor's live `GetDynamicTools(cursor)` catalog search did **not** list `AddMcpServer` (may be parent/gated). Do not claim unavailable for the product; do not register from this PR either.

## Generally installable app vs private skill

User goal: generally installable app (Connect apps / MCP facade path), **not** a private skill as the product end-state. Private skill remains an interim operator aid only.


## Authenticated streamable-HTTP MCP (v0.14+) — ASGI factory only

```bash
# Configure (example — use a token file in production; never commit secrets)
export GROK_BOT_BUILD_BRIDGE_MCP_BEARER_TOKEN_FILE=/absolute/path/to/bearer
export GROK_BOT_BUILD_BRIDGE_MCP_RESOURCE_URL=https://mcp.example.example/mcp
export GROK_BOT_BUILD_BRIDGE_MCP_ALLOWED_HOSTS=mcp.example.example
export GROK_BOT_BUILD_BRIDGE_MCP_ALLOWED_ORIGINS=https://cursor.com   # optional; empty = no browser Origin allowed when present
grok-bot-build-bridge mcp-http-check   # dry-run only — never binds
```

Python API: `create_streamable_http_app()` returns an ASGI app (official MCP Streamable HTTP). **This package does not start a network listener** in v0.14.

### Auth model (honest)

- **Static bearer** via `Authorization: Bearer …` only (env or absolute token file). Fail-closed if unset.
- Constant-time compare; duplicate/malformed Authorization and credentials in query are rejected.
- Tokens never accepted from URL/query; responses/errors must not echo the secret.
- Official SDK `AuthSettings` still needs `issuer_url` / `resource_server_url` for WWW-Authenticate / protected-resource metadata when `token_verifier` is enabled. That is **SDK metadata scaffolding**, not marketplace OAuth and not an authorization server we run. AddMcpServer-style **static headers** are the supported client path here; do not invent OAuth client registration.

### Transport security

- Host + Origin allowlists; **no wildcards by default**.
- No CORS `*`.
- Request body / session caps set on the factory.
- **Do not trust** `X-Forwarded-*` from arbitrary clients. Only a trusted reverse proxy may terminate TLS and set `Host`.
- Cleartext `http://` resource/issuer URLs are refused unless loopback + explicit `GROK_BOT_BUILD_BRIDGE_MCP_ALLOW_INSECURE_LOOPBACK=1` (local tests only).

### Deployment choices that require the user (not automated here)

Once this ASGI app is ready, **you** must choose how agents reach it:

1. **Private network / Tailscale** — run the ASGI app on an HTTPS endpoint only reachable on your tailnet (or similar); point AddMcpServer at that HTTPS URL + Authorization header. No public internet.
2. **Tunnel** (e.g. operator-managed tunnel to BOX/host) — still terminate **HTTPS** at a trusted edge; never expose cleartext non-loopback.
3. **Public hosting** — deploy behind a public HTTPS reverse proxy / private CA as appropriate; manage DNS, certs, and the static bearer as account secrets.

This PR does **not** register AddMcpServer, open ports, buy hosting, or change Windows/WSL hosts.

## Local desktop execution (no extra account)

See `docs/LOCAL_EXEC.md`. Preferred operator path under the no-Tailscale constraint: registered Grok Bot desktop + approved companion wheel/script copy. Draft skill remains interim; Connect-app catalog listing is still a platform gap.
