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
