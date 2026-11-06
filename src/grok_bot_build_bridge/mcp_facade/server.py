"""FastMCP stdio server — official MCP Python SDK wire protocol only.

Stdout is the MCP JSON-RPC stream. All logs go to stderr.
No network listeners; transport is stdio exclusively from this entrypoint.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

TOOL_NAMES: tuple[str, ...] = (
    "bridge_runtime_info",
    "bridge_discover",
    "bridge_onboard",
    "bridge_status",
    "bridge_job_status",
)


def _configure_stderr_logging() -> None:
    """Ensure library chatter does not touch stdout (MCP wire)."""
    root = logging.getLogger()
    # Replace handlers so nothing attaches to stdout.
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # Quieter third-party noise on stderr still OK; never stdout.
    logging.getLogger("mcp").setLevel(logging.WARNING)


def create_server() -> Any:
    """Build the FastMCP app with the bounded read-only tool set."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - exercised when extra missing
        raise SystemExit(
            "mcp package required for mcp-stdio "
            "(pip install 'grok-bot-build-bridge[mcp]' or mcp>=1.28,<2)"
        ) from exc

    from . import handlers

    mcp = FastMCP(
        "grok-bot-build-bridge",
        instructions=(
            "Bounded read-only companion facade. Tools return data only; "
            "they do not grant execute/enqueue/send/cancel authority. "
            "Runtime is typically the Grok Bot BOX (Linux), not the user's Windows host."
        ),
    )

    @mcp.tool(
        name="bridge_runtime_info",
        description=(
            "Honest runtime location marker (box/linux vs windows) and the "
            "allowlisted tool surface. No host mutation."
        ),
    )
    def bridge_runtime_info() -> dict[str, Any]:
        return handlers.handle_runtime_info()

    @mcp.tool(
        name="bridge_discover",
        description=(
            "Read-only Grok Build discovery. mode=live probes the process host "
            "(on Linux BOX expect kind=none); mode=fixture returns packaged demo facts."
        ),
    )
    def bridge_discover(mode: str = "live") -> dict[str, Any]:
        return handlers.handle_discover(mode=mode)

    @mcp.tool(
        name="bridge_onboard",
        description=(
            "Read-only onboarding plan. desired in {unset,native,wsl,both}; "
            "mode in {live,fixture}. Never installs or writes secrets/prefs."
        ),
    )
    def bridge_onboard(desired: str = "unset", mode: str = "fixture") -> dict[str, Any]:
        return handlers.handle_onboard(desired=desired, mode=mode)

    @mcp.tool(
        name="bridge_status",
        description=(
            "List jobs/outbox metadata from the server-scoped state.sqlite only. "
            "Does not create a missing database. No payloads/prompts/credentials."
        ),
    )
    def bridge_status() -> dict[str, Any]:
        return handlers.handle_status()

    @mcp.tool(
        name="bridge_job_status",
        description=(
            "Single-job status from the server-scoped state.sqlite. "
            "task_id must match ^[A-Za-z0-9._:-]{1,128}$. No payloads/prompts."
        ),
    )
    def bridge_job_status(task_id: str) -> dict[str, Any]:
        return handlers.handle_job_status(task_id=task_id)

    return mcp


def run_stdio() -> None:
    """Run MCP over stdio. Never binds a network port."""
    _configure_stderr_logging()
    # Import handlers only through create_server; opening DB is deferred to tool calls.
    mcp = create_server()
    # Explicit stdio — do not offer sse/streamable-http from this entrypoint.
    mcp.run(transport="stdio")


def main() -> None:
    run_stdio()


if __name__ == "__main__":
    main()
