"""Bounded, allowlisted parser for `grok --output-format json` headless results.

Schema source: public grok-build docs (user-guide/14-headless-mode.md, main branch).
Those docs may be newer than the installed grok (e.g. 1.0.70) and no real result
from 1.0.70 has been captured, so a small set of documented/common key variants is
tolerated (``text``|``result``, ``stopReason``|``stop_reason``, ``sessionId``|
``session_id``); unknown fields are ignored. Which variant matched is recorded.

Never stores or returns response text except the exact fixed smoke token when the
whole stripped text equals it. Process exit 0 is NOT model success.

Cost is kept as the exact decimal string from the JSON (no float rounding).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

SUMMARY_SCHEMA = "gbbb.grok_result_summary/v1"
MAX_RESULT_BYTES = 64 * 1024
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_STOP_RE = re.compile(r"^[a-z_]{1,32}$")
_COST_RE = re.compile(r"^(0|[1-9][0-9]{0,5})(\.[0-9]{1,18})?$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_OK_STOP = frozenset({"end_turn"})
_USAGE_KEYS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "reasoning_tokens", "total_tokens",
)
_MAX_TOKENS = 10**12
_MAX_TICKS = 10**22
PARSE_STATUSES = frozenset(
    {"ok", "empty", "truncated", "oversize", "not_utf8", "not_json", "not_object", "no_text"}
)
COST_STATUSES = frozenset({"reported", "partial_omitted", "incomplete_omitted", "unknown"})
VARIANTS = frozenset({"docs_main", "alt_keys", None})

_TEXT_KEYS = ("text", "result")
_STOP_KEYS = ("stopReason", "stop_reason")
_SID_KEYS = ("sessionId", "session_id")


@dataclass(frozen=True)
class GrokResultSummary:
    parse_status: str
    model_ok: bool
    schema: str = SUMMARY_SCHEMA
    schema_variant: str | None = None
    stop_reason: str | None = None
    session_id: str | None = None
    num_turns: int | None = None
    usage: dict[str, int] = field(default_factory=dict)
    total_cost_usd: str | None = None  # exact decimal string
    total_cost_usd_ticks: int | None = None
    cost_status: str = "unknown"
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


def _cost(v: Any) -> str | None:
    """Exact decimal text, finite, >= 0, bounded. JSON floats arrive as Decimal."""
    if isinstance(v, bool) or not isinstance(v, (int, Decimal)):
        return None
    try:
        d = Decimal(v)
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d < 0 or d > 1_000_000:
        return None
    s = format(d, "f")
    return s if _COST_RE.match(s) else None


def _first(obj: dict, keys: tuple[str, ...]) -> tuple[Any, int | None]:
    for i, k in enumerate(keys):
        if k in obj:
            return obj[k], i
    return None, None


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
    if "\ufffd" in stdout:
        return _fail("not_utf8")
    if len(stdout.encode("utf-8")) > MAX_RESULT_BYTES:
        return _fail("oversize")
    s = stdout.strip()
    if not s:
        return _fail("empty")

    def _no_dupes(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                raise ValueError("duplicate key")
            d[k] = v
        return d

    try:
        obj = json.loads(
            s, parse_constant=lambda _c: None, parse_float=Decimal,
            object_pairs_hook=_no_dupes,
        )
    except (ValueError, RecursionError):
        return _fail("not_json")
    if not isinstance(obj, dict):
        return _fail("not_object")
    text, ti = _first(obj, _TEXT_KEYS)
    if not isinstance(text, str):
        return _fail("no_text")

    stop, si = _first(obj, _STOP_KEYS)
    stop = stop if isinstance(stop, str) and _STOP_RE.match(stop) else None
    sid, di = _first(obj, _SID_KEYS)
    sid = sid if isinstance(sid, str) and _SESSION_RE.match(sid) else None
    variant = "alt_keys" if any(i == 1 for i in (ti, si, di)) else "docs_main"
    ie = obj.get("is_error")
    is_error = ie if isinstance(ie, bool) else None

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
    return GrokResultSummary(
        parse_status="ok",
        model_ok=stop in _OK_STOP and is_error is not True,
        schema_variant=variant,
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


ALLOWED_MATCHED_TEXT = frozenset({"BRIDGE_OK"})


def validate_summary(d: Any) -> bool:
    """Strict typed check of a stored summary dict (exact keys, types, bounds)."""
    if not isinstance(d, dict):
        return False
    keys = set(GrokResultSummary.__dataclass_fields__)
    if set(d) != keys or d["schema"] != SUMMARY_SCHEMA:
        return False

    def opt(v, t):
        return v is None or (isinstance(v, t) and not isinstance(v, bool)) if t is not bool else (
            v is None or isinstance(v, bool))

    ok = (
        d["parse_status"] in PARSE_STATUSES
        and isinstance(d["model_ok"], bool)
        and d["schema_variant"] in VARIANTS
        and (d["stop_reason"] is None or (isinstance(d["stop_reason"], str) and _STOP_RE.match(d["stop_reason"])))
        and (d["session_id"] is None or (isinstance(d["session_id"], str) and _SESSION_RE.match(d["session_id"])))
        and (d["num_turns"] is None or _nonneg_int(d["num_turns"], 10_000) is not None)
        and isinstance(d["usage"], dict)
        and all(k in _USAGE_KEYS and _nonneg_int(v, _MAX_TOKENS) is not None for k, v in d["usage"].items())
        and (d["total_cost_usd"] is None or (isinstance(d["total_cost_usd"], str) and _COST_RE.match(d["total_cost_usd"])))
        and (d["total_cost_usd_ticks"] is None or _nonneg_int(d["total_cost_usd_ticks"], _MAX_TICKS) is not None)
        and d["cost_status"] in COST_STATUSES
        and (d["text_len"] is None or _nonneg_int(d["text_len"], MAX_RESULT_BYTES) is not None)
        and (d["text_sha256"] is None or (isinstance(d["text_sha256"], str) and _SHA_RE.match(d["text_sha256"])))
        and opt(d["expected_match"], bool)
        and (d["matched_text"] is None or d["matched_text"] in ALLOWED_MATCHED_TEXT)
        and opt(d["is_error"], bool)
    )
    if not ok:
        return False
    # Consistency: failed parses carry no fields; reported cost needs a value.
    if d["parse_status"] != "ok" and (d["model_ok"] or d["matched_text"] or d["session_id"]):
        return False
    if (d["cost_status"] == "reported") != (d["total_cost_usd"] is not None):
        return False
    if d["matched_text"] is not None and d["expected_match"] is not True:
        return False
    return True


ANSWER_MAX_CHARS = 16_000
_ANSWER_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def extract_answer_text(stdout: str | bytes | None, *, truncated: bool = False) -> tuple[str | None, bool]:
    """Transient, bounded answer text for opt-in console display. Never persisted.

    Returns (text, was_cut). Only from a fully valid JSON result's text field;
    terminal control / bidi characters are replaced with U+FFFD; newlines/tabs kept.
    """
    if parse_grok_json_result(stdout, truncated=truncated).parse_status != "ok":
        return None, False
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8")
    obj = json.loads(stdout.strip(), parse_float=Decimal, parse_constant=lambda _c: None)
    text, _ = _first(obj, _TEXT_KEYS)
    clean = _ANSWER_CTRL.sub("\ufffd", text.replace("\r\n", "\n"))
    if len(clean) > ANSWER_MAX_CHARS:
        return clean[:ANSWER_MAX_CHARS], True
    return clean, False
