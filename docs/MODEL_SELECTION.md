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

## v0.25.0: first real observation, plus explicit identity onboarding

### What was observed (one approved run, 2026-11-07 ~12:50 CET)
- WSL Grok is **1.0.77** (`grok --version`). Earlier notes say 1.0.70; that was the version at
  the time, and they are kept as history. `grok --help` on 1.0.77 lists `-m, --model <MODEL>`.
- The owner's catalog key is `LM-Studio` (owner-supplied, exact case). One headless run used
  `--model LM-Studio --output-format json --max-turns 1`, strict sandbox, `dontAsk`, every tool
  denied, the env switches below, and `/usr/bin/timeout 900` inside WSL. It finished after ~20 s.
- Result: `text` `LOCAL_BRIDGE_OK`, `stopReason` `end_turn`, `num_turns` 1, **no `is_error` field**,
  **no cost fields** (cost unknown, not zero). Tokens: input 9299, output 29, reasoning 21,
  total 9328. Here `total = input + output`, so reasoning is counted inside output; the bridge
  stores what Grok reports and never re-adds reasoning.
- `modelUsage` is keyed by the **provider model id** `qwen3.5-9b-mtp`, **not** the catalog key `LM-Studio`.
- The owner saw the request in the LM Studio server debug window. That backs up that the main
  request was processed locally. It does **not** prove that no side-model call happened
  (session title etc.), and it proves nothing about tool isolation (the tool canary is untested).
- No bridge was installed on the host for this run. The Windows companion there is still 0.16.2,
  and no host setting was changed.
- `tests/fixtures/grok_json_1077_local_observed_redacted.json` keeps the observed field names,
  nesting and numbers. Session and request ids, cwd and thought text are replaced placeholders.
  It is a derived fixture, not a raw capture.

### Explicit identity onboarding (no auto-trust)
The bridge never hardcodes `LM-Studio -> qwen...`. Each user declares their own key:

```
grok-bot-build-bridge model-settings add-alias LM-Studio --expect-usage-key qwen3.5-9b-mtp
# or learn it from a saved result file explicitly:
grok-bot-build-bridge model-settings check-result LM-Studio --result-file result.json          # dry run, exit 3 if not confirmed
grok-bot-build-bridge model-settings check-result LM-Studio --result-file result.json --set-usage-key qwen3.5-9b-mtp
```

- `check-result` reads only the `modelUsage` keys, bounded to 64 KiB with strict JSON and no
  duplicate keys. It never prints the answer, the thought, session ids or the cwd.
- Saving needs `--set-usage-key` equal to the single reported key. A result that reports several
  models is refused (use `add-alias --replace`). Saving replaces the expected keys rather than
  adding to them.
- If you load a different model in LM Studio, the reported id changes. Runs then become
  `model_identity_mismatch` (needs_review) until you update the key explicitly.
- `modelUsage` is Grok's report of the main model calls. It does not prove a local route, and
  side-model calls are not listed.

### `ask` plan additions (used in the observed run)
- Env `GROK_TURN_SUMMARY=false GROK_SESSION_RECAP=false GROK_TITLE_REFRESH=false GROK_PROMPT_SUGGESTIONS=false`
  (all documented in 26-config-reference). These only switch features off. The initial
  title has no documented switch.
- Guest deadline `/usr/bin/timeout -k 10 <timeout_sec>` inside WSL. The host deadline is
  `timeout_sec + 30 s`, so the guest normally stops grok first. A host-side kill is still
  `timeout_uncertain`: needs_review, no retry.
- The success rules are unchanged. Success needs exit 0, a valid JSON result, `end_turn`, no
  `is_error: true` and, with `--model`, `model_identity == confirmed`. An absent `is_error` counts
  as "not reported", which is fine. Exit 0 alone is never success.

### Still unproven
Tool isolation (canary), the absence of side-model or cloud calls, unknown-id behaviour,
installation of the bridge inside WSL, and distribution of an installable app.
