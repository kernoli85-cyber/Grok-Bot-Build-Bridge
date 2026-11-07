"""FastMCP stdio server — official MCP Python SDK wire protocol only.

Stdout is the MCP JSON-RPC stream. All logs go to stderr.
No network listeners; transport is stdio exclusively from this entrypoint.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from .tools import TOOL_NAMES, INSTRUCTIONS, register_bridge_tools

__all__ = ["TOOL_NAMES", "create_server", "run_stdio", "main"]


def _configure_stderr_logging() -> None:
    """Ensure library chatter does not touch stdout (MCP wire)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("mcp").setLevel(logging.WARNING)


def create_server() -> Any:
    """Build the FastMCP app with the bounded read-only tool set."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "mcp package required for mcp-stdio "
            "(pip install 'grok-bot-build-bridge[mcp]' or mcp>=1.28,<2)"
        ) from exc

    mcp = FastMCP(
        "grok-bot-build-bridge",
        instructions=INSTRUCTIONS,
    )
    register_bridge_tools(mcp)
    return mcp


def run_stdio() -> None:
    """Run MCP over stdio. Never binds a network port."""
    _configure_stderr_logging()
    mcp = create_server()
    mcp.run(transport="stdio")


def main() -> None:
    run_stdio()


if __name__ == "__main__":
    main()
