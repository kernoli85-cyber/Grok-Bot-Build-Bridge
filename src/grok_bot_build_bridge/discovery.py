"""Pure discovery parsing — no subprocess, no installs, no config writes.

Callers supply already-captured CLI text / structured facts; this module
classifies backends for the guided installer matrix.
"""
from __future__ import annotations

import re
from typing import Iterable

from .models import (
    BackendState,
    DiscoveryKind,
    DiscoveryReport,
    NativeBackend,
    WslCandidate,
)

_VERSION_RE = re.compile(r"\bgrok\s+(\d+\.\d+\.\d+)\b", re.IGNORECASE)

_SAFE_IDENTITY = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def is_safe_identity(value: str) -> bool:
    """Distro/user names used in argv must be strict identities — no spaces/metachar."""
    return bool(value) and bool(_SAFE_IDENTITY.match(value))


_WSL_LIST_RE = re.compile(
    r"^(?P<star>\*)?\s*(?P<name>\S+)\s+(?P<state>Running|Stopped|Installing|Uninstalling)\s+(?P<ver>\d+)\s*$",
    re.IGNORECASE,
)


def parse_grok_version(text: str) -> str | None:
    m = _VERSION_RE.search(text or "")
    return m.group(1) if m else None


def parse_wsl_list_verbose(text: str) -> list[dict[str, str | bool]]:
    """Parse `wsl -l -v` style output into dicts (name, state, version, default).

    Accepts already-decoded text (including UTF-16→str with NULs stripped by caller).
    Distro names that fail is_safe_identity are skipped (untrusted device metadata).
    """
    rows: list[dict[str, str | bool]] = []
    cleaned = (text or "").replace("\x00", "").replace("\ufeff", "")
    for raw in cleaned.splitlines():
        line = raw.strip()
        if not line:
            continue
        # skip header (EN) and common localized leftovers that still start with NAME
        low = line.lower()
        if low.startswith("name") and "state" in low:
            continue
        if low == "name":
            continue
        line = re.sub(r"\s+", " ", line)
        m = _WSL_LIST_RE.match(line)
        if not m:
            continue
        name = m.group("name")
        if not is_safe_identity(name):
            continue
        rows.append(
            {
                "name": name,
                "state": m.group("state").capitalize(),
                "version": m.group("ver"),
                "default": bool(m.group("star")),
            }
        )
    return rows


def classify_discovery(
    *,
    native_path: str | None,
    native_version_text: str | None,
    wsl_list_text: str | None = None,
    wsl_candidates: Iterable[WslCandidate] | None = None,
) -> DiscoveryReport:
    """Build DiscoveryReport from facts. Never invents a ready backend."""
    warnings: list[str] = []

    if native_path and native_version_text:
        ver = parse_grok_version(native_version_text)
        if ver:
            native = NativeBackend(state=BackendState.READY, path=native_path, version=ver)
        else:
            native = NativeBackend(
                state=BackendState.NOT_READY, path=native_path, version=None
            )
            warnings.append("native_version_unparsed")
    elif native_path and not native_version_text:
        native = NativeBackend(state=BackendState.NOT_READY, path=native_path)
        warnings.append("native_path_without_version")
    else:
        native = NativeBackend(state=BackendState.NOT_FOUND)

    wsl_tuple: tuple[WslCandidate, ...]
    if wsl_candidates is not None:
        wsl_tuple = tuple(wsl_candidates)
    else:
        wsl_tuple = ()
        if wsl_list_text:
            # list-only: mark Running as STOPPED/NOT_READY until Nonroot probe supplied
            for row in parse_wsl_list_verbose(wsl_list_text):
                state = (
                    BackendState.STOPPED
                    if str(row["state"]).lower() == "stopped"
                    else BackendState.NOT_READY
                )
                wsl_tuple += (
                    WslCandidate(
                        distro=str(row["name"]),
                        user="",
                        uid=-1,
                        state=state,
                        notes=("awaiting_nonroot_probe",),
                    ),
                )
            warnings.append("wsl_list_without_nonroot_probe")

    # Root-only candidates never count as READY (caller must set NOT_READY)
    ready_native = native.state == BackendState.READY
    ready_wsl = any(c.state == BackendState.READY for c in wsl_tuple)

    if ready_native and ready_wsl:
        kind: DiscoveryKind = "both"
        default: str | None = None  # user must choose; do not auto-pick
    elif ready_native:
        kind = "native_only"
        default = "native"
    elif ready_wsl:
        kind = "wsl_only"
        default = "wsl"
    else:
        kind = "none"
        default = None

    return DiscoveryReport(
        native=native,
        wsl=wsl_tuple,
        kind=kind,  # type: ignore[arg-type]
        default_backend=default,  # type: ignore[arg-type]
        warnings=tuple(warnings),
    )


def nonroot_ready(
    distro: str,
    user: str,
    uid: int,
    distro_state: str,
    grok_path: str | None,
    version_text: str | None,
) -> WslCandidate:
    """Factory enforcing Nonroot + version rules used by guided discovery."""
    notes: list[str] = []
    if distro_state.lower() == "stopped":
        return WslCandidate(
            distro=distro,
            user=user,
            uid=uid,
            state=BackendState.STOPPED,
            notes=("distro_stopped_ask_before_start",),
        )
    if uid < 1000 or user in ("", "root"):
        notes.append("root_or_system_user_not_ready")
        return WslCandidate(
            distro=distro,
            user=user,
            uid=uid,
            state=BackendState.NOT_READY,
            grok_path=grok_path,
            notes=tuple(notes),
        )
    if not grok_path:
        return WslCandidate(
            distro=distro,
            user=user,
            uid=uid,
            state=BackendState.NOT_FOUND,
            notes=("grok_not_on_path",),
        )
    ver = parse_grok_version(version_text or "")
    if not ver:
        return WslCandidate(
            distro=distro,
            user=user,
            uid=uid,
            state=BackendState.NOT_READY,
            grok_path=grok_path,
            notes=("version_unparsed",),
        )
    return WslCandidate(
        distro=distro,
        user=user,
        uid=uid,
        state=BackendState.READY,
        grok_path=grok_path,
        version=ver,
    )
