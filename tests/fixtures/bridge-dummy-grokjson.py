#!/usr/bin/env python3
"""Dummy emitting grok-style JSON (test only; no network, no grok)."""
import json, sys
mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
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
