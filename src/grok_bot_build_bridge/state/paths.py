from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path


def user_data_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "grok-bot-build-bridge"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "grok-bot-build-bridge"
    return Path.home() / ".local" / "share" / "grok-bot-build-bridge"


def user_config_root() -> Path:
    # Config (secrets) separate from state DB data dir.
    if os.environ.get("LOCALAPPDATA"):
        # Windows: still under LocalAppData; do NOT claim NTFS ACL lockdown via chmod.
        return Path(os.environ["LOCALAPPDATA"]) / "grok-bot-build-bridge"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "grok-bot-build-bridge"
    return Path.home() / ".config" / "grok-bot-build-bridge"


@dataclass(frozen=True)
class StorePaths:
    db_path: Path

    @staticmethod
    def default() -> "StorePaths":
        return StorePaths(db_path=user_data_root() / "state.sqlite")


@dataclass(frozen=True)
class PermissionNote:
    platform: str
    chmod_applied: bool
    guarantee: str


def ensure_private_file(path: Path, *, is_dir: bool = False) -> PermissionNote:
    """Best-effort owner-only permissions.

    On POSIX, applies 0700 (dir) / 0600 (file) when possible.
    On Windows, mkdir/existence only — this code does **not** configure NTFS ACLs
    and must never claim equivalent secrecy guarantees from chmod alone.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if is_dir:
        path.mkdir(parents=True, exist_ok=True)

    platform = sys.platform
    chmod_applied = False
    if platform.startswith("win"):
        return PermissionNote(
            platform=platform,
            chmod_applied=False,
            guarantee=(
                "windows_no_acl_claim: chmod not used as a security guarantee; "
                "protect the directory with NTFS ACLs / user profile isolation outside this tool"
            ),
        )

    try:
        if is_dir or path.is_dir():
            os.chmod(path if path.is_dir() else path.parent, stat.S_IRWXU)
        if path.exists() and path.is_file():
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
            chmod_applied = True
        elif not path.exists():
            # parent dir mode
            os.chmod(path.parent, stat.S_IRWXU)
            chmod_applied = True
    except OSError:
        chmod_applied = False

    return PermissionNote(
        platform=platform,
        chmod_applied=chmod_applied,
        guarantee="posix_owner_rw_best_effort" if chmod_applied else "posix_chmod_failed_or_skipped",
    )


def ensure_private_db_file(path: Path) -> PermissionNote:
    note = ensure_private_file(path.parent, is_dir=True)
    if path.exists():
        note = ensure_private_file(path, is_dir=False)
    return note
