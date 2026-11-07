#!/usr/bin/env python3
"""Interactive local setup for balu-notify. Writes mode-600 JSON config.

Reads webhook URL (visible) and key via getpass (hidden). Never echoes the key.
Does not POST. Does not print the key after save.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import sys
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".config" / "balu-grok-build" / "webhook.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Save Balu webhook URL + key to a mode-600 config file."
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"config path (default: {DEFAULT_CONFIG})",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = args.config or DEFAULT_CONFIG
    print(f"Config path: {path}")
    print(
        "Paste POST-to URL and key from the Grok Bot routine panel "
        "(desktop). Key input is hidden."
    )
    try:
        url = input("Webhook URL (https://…): ").strip()
    except EOFError:
        print("aborted: no URL", file=sys.stderr)
        return 1
    if not url.lower().startswith("https://"):
        print("refused: URL must start with https://", file=sys.stderr)
        return 1
    try:
        key = getpass.getpass("Webhook key (hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\naborted: no key", file=sys.stderr)
        return 1
    if not key:
        print("refused: empty key", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write privately: create/truncate with 600 before writing content.
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"url": url, "key": key}, fh, indent=2)
            fh.write("\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    mode = path.stat().st_mode & 0o777
    print(f"Wrote config mode {oct(mode)} at {path}")
    print("Key not shown. Run: balu-notify --dry-run")
    print("Then test POST: balu-notify --status 'bridge-smoke'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
