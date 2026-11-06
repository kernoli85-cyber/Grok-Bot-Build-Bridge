# Draft Cursor / Grok Bot plugin (not published)

This directory is a **draft** Cursor Plugin layout for evaluation. It is **not**
submitted to the marketplace and is **not** globally installed by CI.

## Official distribution path (sourced)

Per [Cursor Plugins reference](https://cursor.com/docs/reference/plugins):

1. Manifest at `.cursor-plugin/plugin.json` with required `name` (lowercase kebab-case).
2. Skills under `skills/<name>/SKILL.md` with YAML `name` + `description` frontmatter.
3. Host in a **public** Git repository.
4. Submit at [cursor.com/marketplace/publish](https://cursor.com/marketplace/publish) for Cursor review.
5. Grok Bot installs the **same** approved Cursor marketplace plugins (skills-only plugins are valid when no MCP is required).

## Separation of concerns

| Artifact | Role |
|----------|------|
| Python wheel (`grok-bot-build-bridge`) | Local companion CLI/library on the operator Windows host |
| This plugin draft | Bot-facing skill recipe for guided Einrichtung conversation |

Installing the Python package does **not** install this plugin into Grok Bot.
Publishing this plugin does **not** install Python on the user host.
