# Draft Cursor plugin scaffold (not published)

This directory is a **draft** Cursor Plugin layout for evaluation against the
[Cursor Plugins](https://cursor.com/docs/plugins) format. It is **not**
submitted to any marketplace and is **not** globally installed by CI.

## What this is / is not

| Claim | Status |
|-------|--------|
| Matches Cursor `.cursor-plugin/plugin.json` shape | Yes (draft) |
| Submitted to Cursor Marketplace | **No** |
| Official Grok Bot docs say Cursor plugins install into Grok Bot | **Unverified / do not claim** |
| Official Grok Bot Connect apps = this companion | **No** — those docs describe service connectors |

See `docs/DISTRIBUTION.md` for the verified source table.

## Cursor-side requirements (if ever submitted as a Cursor plugin)

1. Manifest at `.cursor-plugin/plugin.json` with required `name` (lowercase kebab-case).
2. Skills under `skills/<name>/SKILL.md` with YAML `name` + `description` frontmatter.
3. Host in a public Git repository.
4. Submit at [cursor.com/marketplace/publish](https://cursor.com/marketplace/publish) for Cursor review.

## Separation of concerns

| Artifact | Role |
|----------|------|
| Python wheel (`grok-bot-build-bridge`) | Local companion CLI/library on the operator Windows/WSL host |
| This plugin draft | Optional Cursor-side skill packaging sketch only |
| `drafts/skills/.../SKILL.md` | Bot-facing recipe text for parent-owned private skill evaluation |

Installing the Python package does **not** install a Grok Bot plugin.
Publishing a Cursor plugin does **not** install Python on the user host, and
does **not** by itself prove Grok Bot marketplace readiness.
