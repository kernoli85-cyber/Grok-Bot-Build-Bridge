# Model selection (v0.24.0): bridge settings, per-job binding, identity check

The bridge never calls a model provider itself. It only passes `--model <id>` to Grok Build,
and Grok Build keeps endpoints, tokens and the `[model.*]` config. The bridge does not read
`~/.grok/config.toml`, does not run `grok models`, and does not connect to LM Studio.

## Evidence

The sources are the official Grok Build docs on the `main` branch, last changed 2026-09-17:
- <https://docs.x.ai/build/cli/reference>
- `crates/codegen/xai-grok-pager/docs/user-guide/11-custom-models.md` and `14-headless-mode.md` in
  <https://github.com/xai-org/grok-build>
- <https://docs.x.ai/build/settings/reference>

None of this has been checked against the installed 1.0.70 `--help`.

| Claim | Status |
|---|---|
| Headless `-m, --model <id>` selects a model | documented (main) |
| `[model.<name>]` custom entries (OpenAI-compatible, e.g. local servers) appear in `grok models` and `/model` | documented |
| User `allowed_models` matches catalog key **or** model id, so a key like `LM-Studio` works with `-m` | documented, indirect |
| `grok models` lists the catalog | documented; it may fetch remotely and need auth, so the bridge never runs it |
| An unknown `-m` value errors and does not fall back to a default | **unverified** (docs only show "Model Not Found" troubleshooting) |
| `json` output `modelUsage` is keyed by the models that answered, `{}` if unavailable | documented; for custom models it is **unverified** whether the key is the catalog key or the provider model id |
| With no `-m`, the model comes from config `[models].default` / `GROK_DEFAULT_MODEL` | documented; the bridge cannot see which one |

## Behaviour

- **Order:** `ask --model X` (per job) wins over the owner's `preferred` in `model_settings.json`.
  `--no-model-preference` gives Grok's **implicit default**, and the output says this plainly.
- **No silent fallback:** an explicit id is either passed verbatim, or the plan is refused if the
  id is invalid. The bridge never substitutes another model.
- **Validation:** ASCII only, 1–64 chars `[A-Za-z0-9._:@+/-]`, starting alphanumeric. No
  `..` or `//`, no leading `-`, and NFKC must be unchanged. The value stays a separate argv
  element after `--single`, and every sandbox, deny and env flag is unchanged.
- **Aliases** (`model-settings add-alias LM-Studio --expect-usage-key <provider-model-id>`):
  these are the owner's claims and are **never "verified available"**. `kind=custom_local`
  raises the per-job timeout ceiling from 600 s to 1800 s for slow local inference. A timeout
  is still `timeout_uncertain` and goes to needs_review with no retry. Local does **not** mean
  free, offline, or authorized.
- **Identity check after a run:** when `--model` was passed, the `modelUsage` keys must all be
  in {id, declared usage keys}. The results:
  - `confirmed`: success.
  - `mismatch`: needs_review `model_identity_mismatch`.
  - `unreported` (no or empty modelUsage): needs_review `model_identity_unverified`.
  - With no `--model`, `not_requested`, and success rules are unchanged.
  The answer can still be shown transiently with `--show-answer`.
- **Privacy:** model names are kept out of the DB, outbox, webhooks and MCP. The stored summary
  (`gbbb.grok_result_summary/v2`) holds only `model_identity`; v1 rows still validate. Names do
  appear in the owner's own CLI preview and in the OS process list (argv).
- The settings file is a private 0600 JSON with a strict schema. Symlinks are refused, and the
  file is written only by an explicit `model-settings` command.

## Still unverified (needs owner-approved host validation)
1. The installed 1.0.70 accepts `--model` in headless mode, with `LM-Studio` as the key.
2. An unknown id errors rather than falling back.
3. The `modelUsage` key used for a custom LM Studio model, which sets the right
   `--expect-usage-key`.
4. Whether a local endpoint is reached without any cloud call. The bridge cannot observe this.
