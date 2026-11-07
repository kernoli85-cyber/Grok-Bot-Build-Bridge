#!/usr/bin/env bash
# Synthetic example only. From a Windows shell, prefer the PowerShell form in docs/INVOKE.md.
# This redirect does not expand the JSON file. No login shell, no bash -c, no flag splicing.
set -euo pipefail
DISTRO="${DISTRO:-Ubuntu}"
USER_NAME="${USER_NAME:-gbbbdemo}"
COMPANION="${COMPANION:-/home/gbbbdemo/.venv/bin/grok-bot-build-bridge}"
ENVELOPE="${ENVELOPE:-examples/invoke-envelope.example.json}"
exec wsl.exe -d "$DISTRO" -u "$USER_NAME" --exec "$COMPANION" invoke < "$ENVELOPE"
