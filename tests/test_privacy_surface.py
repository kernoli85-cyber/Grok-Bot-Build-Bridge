"""Current tree stays inside the synthetic privacy allowlist."""
from pathlib import Path

from tests.privacy_scan import scan_tree

ROOT = Path(__file__).resolve().parents[1]


def test_source_tree_privacy_categories():
    hits = scan_tree(ROOT)
    assert hits == [], "privacy categories: " + ", ".join(hits)
