# Invoke from a Windows desktop through WSL

`grok-bot-build-bridge invoke` is the supported way to run one text prompt, or one bounded selected-context prompt, on Grok Build **inside an already running WSL distro**.

Windows does not install the package, does not change `PATH`, and does not start a login shell. Nothing listens on a port. No Tailscale account and no Grok config edit is required. ACP stays gated. A Windows source tree is not read; files must already be on the POSIX filesystem.

## Install (inside WSL only)

In the distro, as the non-root user who will run grok:

```bash
python3 -m venv "$HOME/.venv"
"$HOME/.venv/bin/python" -m pip install --upgrade pip
"$HOME/.venv/bin/python" -m pip install /path/to/grok_bot_build_bridge-0.31.0-py3-none-any.whl
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

Tools stay denied, the sandbox stays strict, and the working directory is a fresh `/tmp/gbbb-ask-<hex>` directory that is removed if empty. A guest timeout is not a retry.

## Limits

- Not a coding agent and not a marketplace app.
- POSIX source only.
- `setsid` can leave the process group; that is reported as not fully cleaned up, never as success.
- The prompt is visible to the OS process list while grok runs, because grok takes it as an argument.
- Side-model calls are not fully switchable.
- The consent key is the only file this command creates.
