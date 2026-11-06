"""Bounded read-only MCP facade for the companion (stdio + HTTP ASGI factory).

Exposes discover / onboard / status / job-status only. No shell, write,
execute, enqueue, outbox-send, secret-read, or cancel tools.
"""

from __future__ import annotations

__all__ = [
    "TOOL_NAMES",
    "create_server",
    "run_stdio",
    "create_streamable_http_app",
    "check_http_config",
]

from .http_app import create_streamable_http_app
from .http_config import check_http_config
from .server import TOOL_NAMES, create_server, run_stdio
