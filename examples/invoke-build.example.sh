#!/usr/bin/env bash
# Beispiel: von Windows aus (nicht ausführen ohne Freigabe).
# WORK und PROMPT setzen. Kein --always-approve.
set -euo pipefail
WORK="${WORK:-/tmp/balu-grok-smoke}"
PROMPT="${PROMPT:-Antworte ausschließlich mit OK.}"
MAX_TURNS="${MAX_TURNS:-1}"

wsl.exe -d Ubuntu -u okern -- bash -lic "
  set -euo pipefail
  mkdir -p \"$WORK\"
  cd \"$WORK\"
  grok --no-auto-update -p \"$PROMPT\" \\
    --cwd \"$WORK\" \\
    --output-format json \\
    --max-turns $MAX_TURNS \\
    --permission-mode plan \\
    --deny Bash --deny Edit --deny Write \\
    --disable-web-search --no-subagents
"
