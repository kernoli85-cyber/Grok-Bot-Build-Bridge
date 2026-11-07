#!/usr/bin/env python3
"""Dummy emitting grok-style JSON (test only; no network, no grok)."""
import json, os, sys
MODES = ("ok", "noise", "mismatch", "badutf8", "answer", "error")
mode = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in MODES else "ok"
base = os.path.basename(__file__)
for m in MODES[1:]:
    if m in base:  # copies named bridge-dummy-<mode>.py select mode without argv
        mode = m
if mode == "ok":
    print(json.dumps({"text": "BRIDGE_OK\n", "stopReason": "end_turn", "sessionId": "0d3f-AB_9",
                      "num_turns": 1, "usage": {"input_tokens": 5, "output_tokens": 2},
                      "total_cost_usd": 0.0001, "total_cost_usd_ticks": 1000000}))
elif mode == "noise":
    print("log: starting"); print(json.dumps({"text": "BRIDGE_OK", "stopReason": "end_turn"}))
elif mode == "mismatch":
    print(json.dumps({"text": "Sure! BRIDGE_OK", "stopReason": "end_turn"}))
elif mode == "badutf8":
    sys.stdout.buffer.write(b'{"text": "\xff\xfe", "stopReason": "end_turn"}')
elif mode == "answer":
    print(json.dumps({"text": "Antwort: 2+2 = 4.\nZitat: \"ok\" \u2014 Gr\u00fc\u00dfe \U0001f642\x1b[31m",
                      "stopReason": "end_turn", "sessionId": "sess-ANS-1", "num_turns": 1,
                      "usage": {"input_tokens": 12, "output_tokens": 9}, "total_cost_usd": 0.00012345}))
elif mode == "error":
    print(json.dumps({"text": "", "stopReason": "error", "is_error": True}))
    sys.exit(0)
