"""Internal ACP (Agent Client Protocol) stdio client PROTOTYPE. Not wired to any CLI/MCP.

Sources: https://agentclientprotocol.com/protocol/prompt-turn and the Grok Build agent-mode
guide (grok-build docs/user-guide/15-agent-mode.md). Newline-delimited JSON-RPC 2.0 over the
agent's stdin/stdout.

Fail-closed client policy:
- every stdout line must be one JSON-RPC object <= max_line_bytes; anything else
  (log noise, binary, oversize) aborts the session (stdout_contamination / line_too_large);
- responses must match an outstanding request id; unknown/duplicate ids abort;
- agent->client requests: session/request_permission is ALWAYS answered with a reject
  option (or "cancelled" if no reject option exists); fs/*, terminal/* and every other
  method get JSON-RPC error -32601. The client advertises no fs/terminal capabilities;
- streamed agent_message_chunk text is accumulated up to max_text_chars (then truncated);
- deadlines: each request has a timeout; a prompt past its deadline sends session/cancel
  and waits cancel_grace_sec for stopReason "cancelled", else timeout_uncertain.

Not a guarantee: whether the agent asks permission for every tool is the AGENT's policy.
On 1.0.77 `grok agent` has no --sandbox/--deny/--disallowed-tools/--permission-mode/
--max-turns flags, so this client alone does not give the protections of the headless plan.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, BinaryIO

PROTOCOL_VERSION = 1
REJECT_KINDS = ("reject_once", "reject_always")


class AcpError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AcpLimits:
    max_line_bytes: int = 1024 * 1024
    max_text_chars: int = 16_000
    max_messages: int = 20_000
    request_timeout_sec: float = 30.0
    cancel_grace_sec: float = 10.0


@dataclass
class TurnResult:
    stop_reason: str | None
    text: str
    text_truncated: bool
    tool_calls: int
    tool_statuses: dict[str, int] = field(default_factory=dict)
    permission_requests: int = 0
    permission_rejected: int = 0
    refused_client_requests: int = 0
    cancelled_by_client: bool = False


_EOF = object()


class AcpClient:
    def __init__(self, stdin: BinaryIO, stdout: BinaryIO, limits: AcpLimits = AcpLimits()) -> None:
        self._w = stdin
        self._limits = limits
        self._q: queue.Queue = queue.Queue(maxsize=1024)
        self._next_id = 1
        self._pending: set[int] = set()
        self._wlock = threading.Lock()
        self._messages = 0
        self.closed_error: AcpError | None = None
        self._reader = threading.Thread(target=self._read_loop, args=(stdout,), daemon=True)
        self._reader.start()
        self._turn: TurnResult | None = None

    # ---------------- transport ----------------
    def _read_loop(self, src: BinaryIO) -> None:
        lim = self._limits.max_line_bytes
        try:
            while True:
                line = src.readline(lim + 1)
                if not line:
                    break
                if len(line) > lim and not line.endswith(b"\n"):
                    self._q.put(AcpError("line_too_large", "agent line exceeds bound"))
                    return
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", errors="strict"),
                                     parse_constant=lambda _c: None)
                except (UnicodeDecodeError, ValueError, RecursionError):
                    self._q.put(AcpError("stdout_contamination", "non JSON-RPC output on agent stdout"))
                    return
                if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
                    self._q.put(AcpError("stdout_contamination", "not a JSON-RPC 2.0 object"))
                    return
                self._q.put(msg)
        except (OSError, ValueError):
            pass
        self._q.put(_EOF)

    def _send(self, obj: dict) -> None:
        data = (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        with self._wlock:
            try:
                self._w.write(data)
                self._w.flush()
            except (OSError, ValueError):
                raise AcpError("peer_closed", "agent stdin closed") from None

    def _get(self, deadline: float) -> Any:
        if self.closed_error:
            raise self.closed_error
        left = deadline - time.monotonic()
        if left <= 0:
            raise AcpError("timeout", "deadline exceeded")
        try:
            item = self._q.get(timeout=left)
        except queue.Empty:
            raise AcpError("timeout", "deadline exceeded") from None
        if item is _EOF:
            self.closed_error = AcpError("peer_closed", "agent closed stdout")
            raise self.closed_error
        if isinstance(item, AcpError):
            self.closed_error = item
            raise item
        self._messages += 1
        if self._messages > self._limits.max_messages:
            self.closed_error = AcpError("too_many_messages", "message bound exceeded")
            raise self.closed_error
        return item

    # ---------------- dispatch ----------------
    def _handle_incoming(self, msg: dict) -> dict | None:
        """Return a response message if msg is a response, else handle and return None."""
        if "method" in msg:
            method = msg.get("method")
            if "id" in msg:  # agent -> client request
                self._answer_request(msg["id"], method, msg.get("params"))
            elif method == "session/update":
                self._on_update(msg.get("params"))
            return None
        mid = msg.get("id")
        if not isinstance(mid, int) or mid not in self._pending:
            self.closed_error = AcpError("unexpected_response", "response for unknown request id")
            raise self.closed_error
        self._pending.discard(mid)
        return msg

    def _answer_request(self, rid: Any, method: Any, params: Any) -> None:
        t = self._turn
        if method == "session/request_permission":
            opts = params.get("options") if isinstance(params, dict) else None
            reject = None
            if isinstance(opts, list):
                for kind in REJECT_KINDS:
                    reject = next((o.get("optionId") for o in opts if isinstance(o, dict)
                                   and o.get("kind") == kind and isinstance(o.get("optionId"), str)), None)
                    if reject:
                        break
            outcome = {"outcome": "selected", "optionId": reject} if reject else {"outcome": "cancelled"}
            if t:
                t.permission_requests += 1
                t.permission_rejected += 1
            self._send({"jsonrpc": "2.0", "id": rid, "result": {"outcome": outcome}})
            return
        if t:
            t.refused_client_requests += 1
        self._send({"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": "method not supported by this client"}})

    def _on_update(self, params: Any) -> None:
        t = self._turn
        if t is None or not isinstance(params, dict):
            return
        up = params.get("update")
        if not isinstance(up, dict):
            return
        kind = up.get("sessionUpdate")
        if kind == "agent_message_chunk":
            c = up.get("content")
            txt = c.get("text") if isinstance(c, dict) else None
            if isinstance(txt, str) and not t.text_truncated:
                room = self._limits.max_text_chars - len(t.text)
                if len(txt) > room:
                    t.text += txt[:max(room, 0)]
                    t.text_truncated = True
                else:
                    t.text += txt
        elif kind in ("tool_call", "tool_call_update"):
            if kind == "tool_call":
                t.tool_calls += 1
            st = up.get("status")
            if isinstance(st, str) and len(st) <= 32:
                t.tool_statuses[st] = t.tool_statuses.get(st, 0) + 1

    def request(self, method: str, params: dict, timeout: float | None = None) -> dict:
        rid = self._next_id
        self._next_id += 1
        self._pending.add(rid)
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + (timeout or self._limits.request_timeout_sec)
        while True:
            msg = self._handle_incoming(self._get(deadline))
            if msg is None:
                continue
            if msg.get("id") != rid:
                self.closed_error = AcpError("out_of_order_response", "response for another request")
                raise self.closed_error
            if "error" in msg:
                e = msg["error"] if isinstance(msg["error"], dict) else {}
                raise AcpError("agent_error", str(e.get("message", "error"))[:200])
            res = msg.get("result")
            return res if isinstance(res, dict) else {}

    # ---------------- ACP flow ----------------
    def initialize(self) -> dict:
        return self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
        })

    def new_session(self, cwd: str) -> str:
        res = self.request("session/new", {"cwd": cwd, "mcpServers": []})
        sid = res.get("sessionId")
        if not isinstance(sid, str) or not 0 < len(sid) <= 128:
            raise AcpError("bad_session", "session/new returned no sessionId")
        return sid

    def prompt(self, session_id: str, text: str, timeout: float) -> TurnResult:
        self._turn = t = TurnResult(None, "", False, 0)
        rid = self._next_id
        self._next_id += 1
        self._pending.add(rid)
        self._send({"jsonrpc": "2.0", "id": rid, "method": "session/prompt",
                    "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]}})
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    msg = self._handle_incoming(self._get(deadline))
                except AcpError as exc:
                    if exc.code != "timeout" or t.cancelled_by_client:
                        if exc.code == "timeout":
                            raise AcpError("timeout_uncertain", "no cancelled stopReason after session/cancel") from None
                        raise
                    t.cancelled_by_client = True
                    self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}})
                    deadline = time.monotonic() + self._limits.cancel_grace_sec
                    continue
                if msg is None:
                    continue
                if msg.get("id") != rid:
                    raise AcpError("out_of_order_response", "response for another request")
                if "error" in msg:
                    raise AcpError("agent_error", "session/prompt failed")
                res = msg.get("result") if isinstance(msg.get("result"), dict) else {}
                sr = res.get("stopReason")
                t.stop_reason = sr if isinstance(sr, str) and len(sr) <= 32 else None
                return t
        finally:
            self._turn = None


def spawn_agent(argv: list[str] | tuple[str, ...]) -> subprocess.Popen:
    """Spawn a fixed argv (no shell). Caller must call close_agent()."""
    return subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, shell=False, close_fds=True)


def close_agent(proc: subprocess.Popen, grace: float = 5.0) -> int | None:
    """Close stdin, wait, then terminate/kill this child only. Returns exit code or None."""
    for s in (proc.stdin,):
        try:
            if s:
                s.close()
        except OSError:
            pass
    try:
        return proc.wait(grace)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            return proc.wait(grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(grace)
            return None
    finally:
        if proc.stdout:
            proc.stdout.close()
