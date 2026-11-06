"""Allowlisted non-secret operator preferences (desired/default backend only).

Explicit save only. Never stores paths, env, tokens, executables, or prompts.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .state.paths import ensure_private_file, user_config_root

PREFS_SCHEMA_VERSION = 1
DesiredSaved = Literal["native", "wsl", "both"]
DefaultBackend = Literal["native", "wsl"]

_ALLOWED_KEYS = frozenset(
    {"schema_version", "desired", "default_backend", "discovery_kind_at_save", "notes"}
)
_FORBIDDEN_SUBSTRINGS = (
    "token",
    "secret",
    "bearer",
    "password",
    "prompt",
    "executable",
    "argv",
    "env",
    "path",
    "cwd",
    "url",
    "webhook",
    "authorization",
)


class PreferenceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OperatorPreferences:
    schema_version: int
    desired: DesiredSaved
    default_backend: DefaultBackend
    discovery_kind_at_save: str | None
    notes: tuple[str, ...]


def default_preferences_path() -> Path:
    return user_config_root() / "preferences.json"


def _validate_prefs_dict(data: dict[str, Any]) -> OperatorPreferences:
    unknown = set(data.keys()) - _ALLOWED_KEYS
    if unknown:
        raise PreferenceError("unknown_keys", f"non-allowlisted keys: {sorted(unknown)}")
    for k in data:
        low = k.lower()
        for bad in _FORBIDDEN_SUBSTRINGS:
            if bad in low and k not in _ALLOWED_KEYS:
                raise PreferenceError("forbidden_key", k)
    if data.get("schema_version") != PREFS_SCHEMA_VERSION:
        raise PreferenceError(
            "unsupported_schema",
            f"schema_version={data.get('schema_version')!r}",
        )
    desired = data.get("desired")
    if desired not in ("native", "wsl", "both"):
        raise PreferenceError("desired_invalid", "desired must be native|wsl|both")
    default = data.get("default_backend")
    if default not in ("native", "wsl"):
        raise PreferenceError("default_invalid", "default_backend must be native|wsl")
    if desired in ("native", "wsl") and default != desired:
        raise PreferenceError("default_mismatch", "default must match single desired")
    kind = data.get("discovery_kind_at_save")
    if kind is not None and kind not in ("none", "native_only", "wsl_only", "both"):
        raise PreferenceError("discovery_kind_invalid", str(kind))
    notes = data.get("notes") or []
    if not isinstance(notes, list):
        raise PreferenceError("notes_invalid", "notes must be list")
    clean_notes = [str(n)[:500] for n in notes[:32]]
    return OperatorPreferences(
        schema_version=PREFS_SCHEMA_VERSION,
        desired=desired,
        default_backend=default,
        discovery_kind_at_save=kind,
        notes=tuple(clean_notes),
    )


def preferences_to_dict(prefs: OperatorPreferences) -> dict[str, Any]:
    return {
        "schema_version": prefs.schema_version,
        "desired": prefs.desired,
        "default_backend": prefs.default_backend,
        "discovery_kind_at_save": prefs.discovery_kind_at_save,
        "notes": list(prefs.notes),
    }


def load_preferences(path: Path | None = None) -> OperatorPreferences | None:
    """Read allowlisted prefs. Returns None if missing. Never creates files."""
    cfg = Path(path) if path is not None else default_preferences_path()
    if cfg.is_symlink():
        raise PreferenceError("symlink_refused", "preferences path must not be a symlink")
    if not cfg.is_file():
        return None
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreferenceError("load_failed", type(exc).__name__) from exc
    if not isinstance(data, dict):
        raise PreferenceError("load_failed", "root must be object")
    return _validate_prefs_dict(data)


def save_preferences(
    prefs: OperatorPreferences,
    path: Path | None = None,
    *,
    overwrite: bool = False,
) -> dict[str, str]:
    """Atomic write of allowlisted prefs. Refuses clobber unless overwrite=True."""
    cfg = Path(path) if path is not None else default_preferences_path()
    # Refuse writing through a symlink (path or parent) — avoids unexpected targets.
    if cfg.is_symlink() or (cfg.exists() and cfg.is_symlink()):
        raise PreferenceError("symlink_refused", "preferences path must not be a symlink")
    if cfg.parent.exists() and cfg.parent.is_symlink():
        raise PreferenceError("symlink_refused", "preferences parent must not be a symlink")
    if cfg.exists() and not overwrite:
        raise PreferenceError(
            "exists_no_clobber",
            "preferences file exists; pass overwrite=True / --overwrite for intentional replace",
        )
    payload = preferences_to_dict(prefs)
    # Defense: re-validate before write
    _validate_prefs_dict(payload)
    ensure_private_file(cfg.parent, is_dir=True)
    tmp = cfg.with_suffix(cfg.suffix + ".tmp")
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp.write_text(text, encoding="utf-8")
    ensure_private_file(tmp, is_dir=False)
    os.replace(tmp, cfg)
    ensure_private_file(cfg, is_dir=False)
    return {"path": str(cfg), "overwrite": str(bool(overwrite))}
