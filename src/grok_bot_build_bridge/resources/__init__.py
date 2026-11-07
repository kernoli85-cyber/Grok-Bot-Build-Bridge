"""Packaged non-secret resources (fixtures, draft skill text)."""
from __future__ import annotations

from importlib import resources


def fixture_text(name: str) -> str:
    """Load a UTF-8 fixture shipped with the package (not tests/)."""
    base = resources.files("grok_bot_build_bridge.resources")
    return (base / "fixtures" / name).read_text(encoding="utf-8")


def draft_skill_text() -> str:
    base = resources.files("grok_bot_build_bridge.resources")
    return (base / "skill" / "grok-bot-build-bridge-einrichtung" / "SKILL.md").read_text(
        encoding="utf-8"
    )


def draft_skill_path_hint() -> str:
    """Repo-relative hint for developers; installed wheels use resources API."""
    return "drafts/skills/grok-bot-build-bridge-einrichtung/SKILL.md"
