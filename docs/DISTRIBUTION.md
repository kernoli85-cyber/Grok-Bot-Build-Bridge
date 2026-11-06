# Distribution: Python companion vs Bot plugin

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

## Bot plugin / marketplace — external gate (official docs)

Verified from [Cursor Plugins reference](https://cursor.com/docs/reference/plugins) (fetched this slice):

**Minimal Cursor Plugin requirements:**

| Requirement | Detail |
|-------------|--------|
| Manifest | `.cursor-plugin/plugin.json` with required `name` (lowercase kebab-case) |
| Skills | `skills/*/SKILL.md` with frontmatter `name` + `description` (folder discovery default) |
| Paths | Relative only; no `..` or absolute paths |
| Hosting | Public Git repository |
| Submit | https://cursor.com/marketplace/publish — Cursor team review |
| Secrets | Declared only as `variables` schema; values set in dashboard, never in repo |

Draft layout in this repo: `drafts/plugin/` (not submitted). Packaged skill text also ships inside the wheel under `grok_bot_build_bridge.resources` for `skill-draft` inspection.

**Remaining gate:** Marketplace submission + approval is an **owner/publisher** action outside this repository’s CI. Until approved, operators use: (1) Python wheel/venv companion, (2) optional parent `UpdateSkill` of the draft recipe, or (3) local Cursor plugin path — not claimed as done here.

## Reproducibility

CI records wheel/sdist filenames and SHA-256 of built artifacts for that run. Bit-for-bit reproducibility across OS/toolchains is **not** claimed unless measured equal hashes are reported for the same commit on both OS jobs (see packaging job logs).
