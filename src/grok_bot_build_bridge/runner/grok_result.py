"""Bounded, allowlisted parser for `grok --output-format json` headless results.

Schema source: public grok-build docs (user-guide/14-headless-mode.md, main branch).
That documentation may be newer than the installed grok; unknown fields are ignored.

Never stores or returns the response text except the exact fixed smoke token when the
whole stripped `text` field equals it. Process exit 0 is NOT model success.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any

MAX_RESULT_BYTES = 64 * 1024
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_STOP_RE = re.compile(r"^[a-z_]{1,32}$")
_OK_STOP = frozenset({"end_turn"})
_USAGE_KEYS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "reasoning_tokens", "total_tokens",
)
_MAX_TOKENS = 10**12
_MAX_COST_USD = 1_000_000.0
_MAX_TICKS = 10**22


@dataclass(frozen=True)
class GrokResultSummary:
    parse_status: str  # ok | empty | truncated | oversize | not_utf8 | not_json | not_object | no_text
    model_ok: bool
    stop_reason: str | None = None
    session_id: str | None = None
    num_turns: int | None = None
    usage: dict[str, int] = field(default_factory=dict)
    total_cost_usd: float | None = None
    total_cost_usd_ticks: int | None = None
    cost_status: str = "unknown"  # reported | partial_omitted | incomplete_omitted | unknown
    text_len: int | None = None
    text_sha256: str | None = None
    expected_match: bool | None = None
    matched_text: str | None = None
    is_error: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _nonneg_int(v: Any, cap: int) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v if 0 <= v <= cap else None


def _cost(v: Any) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if not math.isfinite(f) or f < 0 or f > _MAX_COST_USD:
        return None
    return f


def _fail(status: str) -> GrokResultSummary:
    return GrokResultSummary(parse_status=status, model_ok=False)


def parse_grok_json_result(
    stdout: str | bytes | None,
    *,
    truncated: bool = False,
    expected_text: str | None = None,
) -> GrokResultSummary:
    if truncated:
        return _fail("truncated")
    if stdout is None:
        return _fail("empty")
    if isinstance(stdout, bytes):
        if len(stdout) > MAX_RESULT_BYTES:
            return _fail("oversize")
        try:
            stdout = stdout.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return _fail("not_utf8")
    if "\ufffd" in stdout:  # replacement chars from lossy decode upstream
        return _fail("not_utf8")
    if len(stdout.encode("utf-8")) > MAX_RESULT_BYTES:
        return _fail("oversize")
    s = stdout.strip()
    if not s:
        return _fail("empty")
    # Whole stdout must be exactly one JSON object: no log noise, no substring search.
    try:
        obj = json.loads(s, parse_constant=lambda _c: None)
    except (ValueError, RecursionError):
        return _fail("not_json")
    if not isinstance(obj, dict):
        return _fail("not_object")
    text = obj.get("text")
    if not isinstance(text, str):
        return _fail("no_text")

    stop = obj.get("stopReason")
    stop = stop if isinstance(stop, str) and _STOP_RE.match(stop) else None
    sid = obj.get("sessionId")
    sid = sid if isinstance(sid, str) and _SESSION_RE.match(sid) else None
    is_error = obj.get("is_error") if isinstance(obj.get("is_error"), bool) else None

    usage: dict[str, int] = {}
    u = obj.get("usage")
    if isinstance(u, dict):
        for k in _USAGE_KEYS:
            n = _nonneg_int(u.get(k), _MAX_TOKENS)
            if n is not None:
                usage[k] = n

    cost = _cost(obj.get("total_cost_usd"))
    ticks = _nonneg_int(obj.get("total_cost_usd_ticks"), _MAX_TICKS)
    if obj.get("cost_is_partial") is True:
        cost_status, cost, ticks = "partial_omitted", None, None
    elif obj.get("usage_is_incomplete") is True:
        cost_status, cost, ticks = "incomplete_omitted", None, None
    elif cost is not None:
        cost_status = "reported"
    else:
        cost_status, ticks = "unknown", None

    stripped = text.strip()
    match = None if expected_text is None else (stripped == expected_text)
    model_ok = stop in _OK_STOP and is_error is not True
    return GrokResultSummary(
        parse_status="ok",
        model_ok=model_ok,
        stop_reason=stop,
        session_id=sid,
        num_turns=_nonneg_int(obj.get("num_turns"), 10_000),
        usage=usage,
        total_cost_usd=cost,
        total_cost_usd_ticks=ticks,
        cost_status=cost_status,
        text_len=len(text),
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        expected_match=match,
        matched_text=expected_text if match else None,
        is_error=is_error,
    )


def classify(summary: GrokResultSummary, *, require_expected: bool) -> str | None:
    """Return needs_review error code, or None if the result is acceptable."""
    if summary.parse_status != "ok":
        return "success_exit_invalid_result"
    if not summary.model_ok:
        return "model_result_not_ok"
    if require_expected and summary.expected_match is not True:
        return "smoke_text_mismatch"
    return None
