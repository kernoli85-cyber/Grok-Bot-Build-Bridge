#!/usr/bin/env bash
# Example command hook for Grok Build. Synthetic only; not a real account.
# Erwartet BALU_WEBHOOK_URL und BALU_WEBHOOK_KEY in der Umgebung.
# Liest Hook-JSON von stdin und POSTet es an die Bot-Webhook-Routine.
set -euo pipefail
: "${BALU_WEBHOOK_URL:?BALU_WEBHOOK_URL fehlt}"
: "${BALU_WEBHOOK_KEY:?BALU_WEBHOOK_KEY fehlt}"
payload="$(cat)"
curl -sS -X POST "$BALU_WEBHOOK_URL" \
  -H "Authorization: Bearer $BALU_WEBHOOK_KEY" \
  -H "Content-Type: application/json" \
  -d "$payload" \
  --max-time 10 \
  -o /dev/null -w "%{http_code}\n"
