# Invoke from a Windows desktop through WSL

`grok-bot-build-bridge invoke` is the supported way to run one text prompt, one bounded selected-context prompt, or one **review-only file proposal** on Grok Build **inside an already running WSL distro**.

Windows does not install the package, does not change `PATH`, and does not start a login shell. Nothing listens on a port. No Tailscale account and no Grok config edit is required. ACP stays gated. A Windows source tree is not read; files must already be on the POSIX filesystem.

## Install (inside WSL only)

In the distro, as the non-root user who will run grok:

```bash
python3 -m venv "$HOME/.venv"
"$HOME/.venv/bin/python" -m pip install --upgrade pip
"$HOME/.venv/bin/python" -m pip install /path/to/grok_bot_build_bridge-0.32.3-py3-none-any.whl
```

The absolute path `$HOME/.venv/bin/grok-bot-build-bridge` is the only companion path Windows needs. The first dry-run creates `$HOME/.config/grok-bot-build-bridge/consent.key` (mode 0600). There is no model-settings file for this command. The model id and the expected `modelUsage` key travel in the request.

## Windows command

PowerShell, no interpolation of the JSON:

```powershell
Get-Content -Raw .\envelope.json | wsl.exe -d Ubuntu -u gbbbdemo --exec /home/gbbbdemo/.venv/bin/grok-bot-build-bridge invoke
```

`gbbbdemo` and `/home/gbbbdemo` are fictional examples. Use the distro and user that are already running. `wsl.exe` must not be given a shell string (`bash -lc`, `bash -c`). Stdin of `wsl.exe` is stdin of `invoke`.

The companion prints one JSON object on stdout and does not write a job database, outbox, or MCP session. It does not retry.

## Envelope `gbbb.invoke/v1`

UTF-8, at most 16 KiB, exactly one JSON object. Unknown fields are rejected. `max_turns` must be 1. `model.kind` must be `custom_local`. `executable` is an absolute path with no shell metacharacters and no extra arguments. The process environment is the runner's fixed set; the envelope cannot set variables.

Dry-run (default): omit `execute`. The response includes `consent_token` and never includes the prompt, the question, file names, or a session id.

```json
{
  "schema": "gbbb.invoke/v1",
  "op": "ask",
  "executable": "/home/gbbbdemo/.local/bin/grok",
  "model": {"id": "LM-Studio", "kind": "custom_local", "expect_usage_keys": ["provider-model-id"]},
  "prompt": "Reply with the single word READY.",
  "timeout_sec": 180
}
```

Selected context uses the same gates as `ask-context` (explicit files, snapshot text rules, 8 KiB budget):

```json
{
  "schema": "gbbb.invoke/v1",
  "op": "ask-context",
  "executable": "/home/gbbbdemo/.local/bin/grok",
  "model": {"id": "LM-Studio", "kind": "custom_local", "expect_usage_keys": ["provider-model-id"]},
  "source": "/home/gbbbdemo/work",
  "files": ["notes/a.txt"],
  "question": "What is the widget port?"
}
```

A real call repeats that object with `"execute": true`, `"allow_model_call": true`, and `"confirm_send": "<consent_token>"`. Any change of prompt, files, question, model, executable realpath, or timeout makes the token not match (`consent_mismatch`). `"show_answer": true` may be added only together with `execute`; the answer is in that response only and is not stored.

Success requires Grok's `modelUsage` keys to be within the declared id and `expect_usage_keys`. Otherwise the status is `needs_review` and nothing is run again.

## Proposal (`op: propose`) — does not edit files

`propose` asks the same protected no-tools runner for a full-file replacement of the explicitly selected POSIX text files. The instruction is plain text, like a question. It cannot add paths, flags, or commands.

The model text is untrusted. The prompt tells the model to return exactly one JSON object with both required keys:

```json
{"schema":"gbbb.file_replacements/v1","files":[{"path":"notes/a.txt","content":"full file text\n"}]}
```

`schema` must be that string. `files` lists each selected path once, with the full new text in `content`. An object that has only `files` is rejected. Markdown fences, a second JSON value, a wrong path, control characters, or an oversize file also reject the proposal. A proposal that matches the current bytes is `changed: false`.

`result.status` is only the runner. For `propose`, the top-level `status` is the outcome: `proposal_accepted`, `proposal_rejected`, or `execution_needs_review` / `execution_failed`. Exit 0 is only `proposal_accepted`. A rejected proposal is exit 3 even when `result.status` is `succeeded`. That rejection is not a usable edit. This path is not a proven edit workflow.

A rejection includes `proposal.diagnostic` and, by default, no answer text. It names the result field used (`text` or `result`), byte length, whether the other field was also present, a leading class (`object`, `letter`, …), a fence class, and a JSON error class plus character offset. `fenced_json: true` means a markdown fence wrapped something that starts like JSON; that reply is still rejected. `stdout_capture_truncated` is only the bridge's capture cap. `usage` token counts do not prove the model cut the answer short.

`show_rejected_reply: true` together with `show_proposal: true` is an execute-only opt-in for that one response. Set the same `show_rejected_reply` value on the dry-run: the consent token binds it, and a mismatch does not call the model. The dry-run still does not print a reply. The shown text uses the same control-character replacement and length cap as `show_answer`. It is not stored, not sent to the database, outbox, or MCP, and it does not make the proposal accepted. Do not copy it into a public log. `propose` still does not accept fenced or otherwise invalid JSON.

Nothing in this response writes the source tree, commits, or applies the edit. `show_proposal: true` (execute only) adds a transient unified diff for review. `show_answer` is not this operation and is not permission to apply. `proposal_token` binds the selection order, the base bytes, and the proposed bytes. The bridge does not store the proposal. The token is not an apply step. There is no apply command in this version.

```json
{
  "schema": "gbbb.invoke/v1",
  "op": "propose",
  "executable": "/home/gbbbdemo/.local/bin/grok",
  "model": {"id": "LM-Studio", "kind": "custom_local", "expect_usage_keys": ["provider-model-id"]},
  "source": "/home/gbbbdemo/work",
  "files": ["notes/a.txt"],
  "instruction": "Replace the word alpha with beta."
}
```

Dry-run fields include `proposal_only: true`, `applied: false`, and `source_writes: false`. Execute repeats the object with `execute`, `allow_model_call`, and `confirm_send`. If the files change after the dry-run, the token does not match. If they change during the call, the proposal is rejected with `source_changed` and the source is left as it was. A model-identity mismatch or a cleanup failure does not produce a proposal.

Tools stay denied, the sandbox stays strict, and the working directory is a fresh `/tmp/gbbb-ask-<hex>` directory that is removed if empty. A guest timeout is not a retry.

## Limits

- Not a coding agent and not a marketplace app. A proposal is review material. The selected files stay as they were.
- POSIX source only. Windows paths are not read.
- `setsid` can leave the process group; that is reported as not fully cleaned up, never as success, and it does not yield an accepted proposal.
- The prompt is visible to the OS process list while grok runs, because grok takes it as an argument.
- Side-model calls are not fully switchable.
- The bridge's durable write is the consent key under the user config directory. It also creates a short-lived staging directory and, on execute, a fresh `/tmp/gbbb-ask-<hex>` directory, which it removes if empty. Grok itself may still create session or temp files under the user home. The bridge does not delete those.
