#!/usr/bin/env python3
"""Mock ACP agent (test only; no network, no model). Mode = argv[1]."""
import json
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
out = sys.stdout


def send(o):
    out.write(json.dumps(o) + "\n")
    out.flush()


def read():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def upd(sid, u):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": u}})


def chunk(sid, t):
    upd(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": t}})


if mode == "contaminate_early":
    print("log: starting agent", flush=True)
m = read()
send({"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": 1, "authMethods": [],
      "_seen_caps": m["params"]["clientCapabilities"]}})
m = read()
send({"jsonrpc": "2.0", "id": m["id"], "result": {"sessionId": "mock-sess-1"}})
m = read()
pid, sid = m["id"], m["params"]["sessionId"]
if mode == "ok":
    for part in ("LOCAL", "_BRIDGE", "_OK"):
        chunk(sid, part)
    send({"jsonrpc": "2.0", "id": pid, "result": {"stopReason": "end_turn"}})
elif mode == "permission":
    upd(sid, {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "read", "kind": "read", "status": "pending"})
    send({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission", "params": {
        "sessionId": sid, "toolCall": {"toolCallId": "c1"},
        "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "deny", "name": "Deny", "kind": "reject_once"}]}})
    r = read()
    ok = r.get("id") == 900 and r["result"]["outcome"] == {"outcome": "selected", "optionId": "deny"}
    upd(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "failed"})
    chunk(sid, "DENIED_OK" if ok else "ALLOWED_BAD")
    send({"jsonrpc": "2.0", "id": pid, "result": {"stopReason": "end_turn"}})
elif mode == "permission_no_reject":
    send({"jsonrpc": "2.0", "id": 901, "method": "session/request_permission", "params": {
        "sessionId": sid, "options": [{"optionId": "allow", "kind": "allow_always"}]}})
    r = read()
    chunk(sid, "CANCELLED_OK" if r["result"]["outcome"] == {"outcome": "cancelled"} else "BAD")
    send({"jsonrpc": "2.0", "id": pid, "result": {"stopReason": "end_turn"}})
elif mode == "client_requests":
    for i, meth in enumerate(("fs/read_text_file", "fs/write_text_file", "terminal/create", "x.ai/unknown")):
        send({"jsonrpc": "2.0", "id": 950 + i, "method": meth, "params": {"path": "/etc/passwd"}})
        r = read()
        assert r["error"]["code"] == -32601
    send({"jsonrpc": "2.0", "method": "x.ai/whatever", "params": {}})
    chunk(sid, "REFUSED_OK")
    send({"jsonrpc": "2.0", "id": pid, "result": {"stopReason": "end_turn"}})
elif mode == "contaminate":
    chunk(sid, "a")
    out.write("DEBUG some log line\n")
    out.flush()
    time.sleep(5)
elif mode == "huge":
    out.write('{"jsonrpc":"2.0","method":"session/update","params":{"x":"' + "A" * 2_000_000 + '"}}\n')
    out.flush()
    time.sleep(5)
elif mode == "unknown_id":
    send({"jsonrpc": "2.0", "id": 777, "result": {}})
    time.sleep(5)
elif mode == "flood_text":
    for _ in range(200):
        chunk(sid, "x" * 1000)
    send({"jsonrpc": "2.0", "id": pid, "result": {"stopReason": "end_turn"}})
elif mode == "stall_cancel":
    chunk(sid, "partial")
    c = read()
    assert c["method"] == "session/cancel"
    send({"jsonrpc": "2.0", "id": pid, "result": {"stopReason": "cancelled"}})
elif mode == "stall_ignore_cancel":
    read()
    time.sleep(30)
elif mode == "crash":
    sys.exit(3)
