from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_IN_SRC = [
    re.compile(r"-----BEGIN (RSA |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
]


def _iter_text_files():
    skip_dirs = {".git", ".venv", "__pycache__", ".pytest_cache", "dist", "build", ".eggs"}
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bin", ".pyc", ".sqlite"}:
            continue
        yield path


def test_full_tree_no_private_key_material():
    hits = []
    for path in _iter_text_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pat in FORBIDDEN_IN_SRC:
            if pat.search(text):
                hits.append(f"{path.relative_to(ROOT)}:{pat.pattern}")
    assert hits == [], f"secret-like material: {hits}"


def test_diff_against_main_no_token_shaped_additions():
    """Scan added lines in branch diff for token-shaped secrets."""
    try:
        diff = subprocess.check_output(
            ["git", "diff", "origin/main...HEAD", "--", "*.py", "*.md", "*.yml", "*.toml"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("git diff unavailable")
    added = [ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    blob = "\n".join(added)
    for pat in FORBIDDEN_IN_SRC:
        assert not pat.search(blob), f"diff adds secret-like pattern {pat.pattern}"
