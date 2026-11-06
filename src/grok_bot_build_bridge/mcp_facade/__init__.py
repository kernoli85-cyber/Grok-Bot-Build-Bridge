"""Bounded read-only MCP facade for the companion (stdio on bot box).

Exposes discover / onboard / status / job-status only. No shell, write,
execute, enqueue, outbox-send, secret-read, or cancel tools.
"""

from __future__ import annotations

__all__ = ["create_server", "run_stdio", "TOOL_NAMES"]

from .server import TOOL_NAMES, create_server, run_stdio
