from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StorePaths:
    db_path: Path

    @staticmethod
    def default() -> "StorePaths":
        # Platform-appropriate user data dir (Linux/mac); Windows uses LOCALAPPDATA when set.
        local = os.environ.get("LOCALAPPDATA")
        if local:
            root = Path(local) / "grok-bot-build-bridge"
        else:
            xdg = os.environ.get("XDG_DATA_HOME")
            if xdg:
                root = Path(xdg) / "grok-bot-build-bridge"
            else:
                root = Path.home() / ".local" / "share" / "grok-bot-build-bridge"
        return StorePaths(db_path=root / "state.sqlite")


def ensure_private_db_file(path: Path) -> None:
    """Create parent dir 0700 and ensure DB file mode 0600 when present."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, stat.S_IRWXU)
    except OSError:
        pass
    if path.exists():
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
