"""Owner-chosen Grok Build model selection (box-side bridge settings). No model calls.

Evidence (official Grok Build docs, main branch, last change 2026-09-17; NOT verified against
the installed 1.0.70 --help):
- headless `-m, --model <id>` selects a model; ids come from `grok models` (built-in + custom
  `[model.<name>]` entries in ~/.grok/config.toml). User-config `allowed_models` matches catalog
  key or model id, so a custom catalog key such as `LM-Studio` is passed through verbatim.
- `grok models` may fetch a remote catalog and needs auth -> the bridge never runs it.
- Unknown-id behaviour is only described as "Model Not Found" troubleshooting; whether every
  version errors instead of falling back is unverified.
- `--output-format json` may carry `modelUsage` keyed by model; `{}` when unavailable. Whether
  keys are catalog keys or the provider model id is unverified for custom models.

Policy: a name in these settings is the owner's claim, never "verified available". Model names
stay out of the DB, outbox, webhooks and MCP; the stored summary only holds an identity status.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .state.paths import ensure_private_file, user_config_root

SETTINGS_SCHEMA = "gbbb.model_settings/v1"
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,63}$")
MAX_ALIASES = 32
MAX_USAGE_KEYS = 8
DEFAULT_TIMEOUT_CEILING_SEC = 600.0
LOCAL_TIMEOUT_CEILING_SEC = 1800.0   # hard ceiling even for slow local inference
KINDS = ("custom_local", "custom_remote", "builtin")
SOURCES = ("per_job", "owner_preference", "implicit_default")


class ModelSelectionError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_model_id(value: Any) -> str:
    """ASCII-only, bounded, no leading '-', no whitespace/control/bidi/confusables."""
    if not isinstance(value, str):
        raise ModelSelectionError("model_id_invalid", "model id must be a string")
    if unicodedata.normalize("NFKC", value) != value or not value.isascii():
        raise ModelSelectionError("model_id_non_ascii", "model id must be plain ASCII")
    if not MODEL_ID_RE.match(value) or ".." in value or "//" in value:
        raise ModelSelectionError(
            "model_id_invalid", "model id: 1-64 chars [A-Za-z0-9._:@+/-], must start alphanumeric")
    return value


@dataclass(frozen=True)
class ModelAlias:
    name: str
    kind: str = "custom_local"
    expect_usage_keys: tuple[str, ...] = ()
    timeout_sec: float | None = None


@dataclass(frozen=True)
class ModelSettings:
    preferred: str | None = None
    aliases: dict[str, ModelAlias] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelChoice:
    model_id: str | None
    source: str
    kind: str | None
    expect_usage_keys: tuple[str, ...]
    timeout_ceiling_sec: float
    default_timeout_sec: float | None
    availability: str = "unverified"  # never claimed; bridge does not enumerate models

    def describe(self) -> dict[str, Any]:
        return {
            "source": self.source, "model_id": self.model_id, "kind": self.kind,
            "availability": self.availability,
            "identity_check": ("not_requested" if self.model_id is None else
                               "post_run via modelUsage keys; unreported -> needs_review"),
            "implicit_default_note": (
                "no -m passed: Grok Build picks its configured default (config/GROK_DEFAULT_MODEL); "
                "bridge cannot tell which model that is" if self.model_id is None else None),
            "timeout_ceiling_sec": self.timeout_ceiling_sec,
            "free_or_offline_claimed": False,
        }


def _alias_from(name: str, d: Any) -> ModelAlias:
    if not isinstance(d, dict) or set(d) - {"kind", "expect_usage_keys", "timeout_sec"}:
        raise ModelSelectionError("settings_invalid", "alias entry has unknown keys")
    kind = d.get("kind", "custom_local")
    if kind not in KINDS:
        raise ModelSelectionError("settings_invalid", "alias kind invalid")
    keys = d.get("expect_usage_keys", [])
    if not isinstance(keys, list) or len(keys) > MAX_USAGE_KEYS:
        raise ModelSelectionError("settings_invalid", "expect_usage_keys must be a short list")
    t = d.get("timeout_sec")
    if t is not None:
        ceiling = LOCAL_TIMEOUT_CEILING_SEC if kind == "custom_local" else DEFAULT_TIMEOUT_CEILING_SEC
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= ceiling:
            raise ModelSelectionError("settings_invalid", f"timeout_sec must be 0..{ceiling:g}")
        t = float(t)
    return ModelAlias(validate_model_id(name), kind, tuple(validate_model_id(k) for k in keys), t)


def parse_settings(data: Any) -> ModelSettings:
    if not isinstance(data, dict) or set(data) - {"schema", "preferred", "aliases"} \
            or data.get("schema") != SETTINGS_SCHEMA:
        raise ModelSelectionError("settings_invalid", f"expected {SETTINGS_SCHEMA} object")
    raw = data.get("aliases", {})
    if not isinstance(raw, dict) or len(raw) > MAX_ALIASES:
        raise ModelSelectionError("settings_invalid", "aliases must be an object (bounded)")
    aliases = {}
    for n, d in raw.items():
        a = _alias_from(n, d)
        if a.name.casefold() in {k.casefold() for k in aliases}:
            raise ModelSelectionError("settings_invalid", "duplicate alias (case-insensitive)")
        aliases[a.name] = a
    pref = data.get("preferred")
    return ModelSettings(None if pref is None else validate_model_id(pref), aliases)


def settings_to_dict(s: ModelSettings) -> dict[str, Any]:
    return {"schema": SETTINGS_SCHEMA, "preferred": s.preferred,
            "aliases": {n: {"kind": a.kind, "expect_usage_keys": list(a.expect_usage_keys),
                            **({"timeout_sec": a.timeout_sec} if a.timeout_sec is not None else {})}
                        for n, a in sorted(s.aliases.items())}}


def default_settings_path() -> Path:
    return user_config_root() / "model_settings.json"


def load_settings(path: Path | None = None) -> ModelSettings:
    p = Path(path) if path is not None else default_settings_path()
    if p.is_symlink():
        raise ModelSelectionError("symlink_refused", "settings path must not be a symlink")
    if not p.exists():
        return ModelSettings()
    if p.stat().st_size > 64 * 1024:
        raise ModelSelectionError("settings_invalid", "settings file too large")
    try:
        return parse_settings(json.loads(p.read_text(encoding="utf-8")))
    except (ValueError, UnicodeDecodeError):
        raise ModelSelectionError("settings_invalid", "settings file is not valid JSON") from None


def save_settings(s: ModelSettings, path: Path | None = None) -> Path:
    p = Path(path) if path is not None else default_settings_path()
    if p.is_symlink() or p.parent.is_symlink():
        raise ModelSelectionError("symlink_refused", "settings path/parent must not be a symlink")
    payload = settings_to_dict(s)
    parse_settings(payload)
    ensure_private_file(p.parent, is_dir=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    ensure_private_file(tmp, is_dir=False)
    os.replace(tmp, p)
    ensure_private_file(p, is_dir=False)
    return p


def resolve_model(per_job: str | None, settings: ModelSettings, *,
                  use_preference: bool = True) -> ModelChoice:
    """Per-job flag > owner preference > implicit Grok default. Never a silent fallback:
    an explicit choice is passed as -m verbatim or the plan fails."""
    if per_job is not None:
        mid, source = validate_model_id(per_job), "per_job"
    elif use_preference and settings.preferred is not None:
        mid, source = settings.preferred, "owner_preference"
    else:
        return ModelChoice(None, "implicit_default", None, (), DEFAULT_TIMEOUT_CEILING_SEC, None)
    alias = settings.aliases.get(mid)
    if alias is None:
        return ModelChoice(mid, source, "unregistered", (mid,), DEFAULT_TIMEOUT_CEILING_SEC, None)
    ceiling = LOCAL_TIMEOUT_CEILING_SEC if alias.kind == "custom_local" else DEFAULT_TIMEOUT_CEILING_SEC
    keys = tuple(dict.fromkeys((mid, *alias.expect_usage_keys)))
    return ModelChoice(mid, source, alias.kind, keys, ceiling, alias.timeout_sec)


IDENTITY_STATUSES = ("not_requested", "confirmed", "mismatch", "unreported")


def model_identity(model_usage: Any, expected: tuple[str, ...] | None) -> str:
    """Compare modelUsage keys to the owner-declared expected keys. Names are not returned."""
    if not expected:
        return "not_requested"
    if not isinstance(model_usage, dict) or not model_usage:
        return "unreported"
    keys = list(model_usage)
    if len(keys) > 32 or not all(isinstance(k, str) and MODEL_ID_RE.match(k) for k in keys):
        return "mismatch"
    return "confirmed" if set(keys) <= set(expected) else "mismatch"
