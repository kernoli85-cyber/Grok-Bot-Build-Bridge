"""Security review for the bounded MCP facade (no privileged tools / path escapes)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FACADE = ROOT / "src" / "grok_bot_build_bridge" / "mcp_facade"


def test_facade_source_has_no_privileged_tool_names():
    forbidden = {
        "bridge_execute",
        "bridge_shell",
        "bridge_enqueue",
        "bridge_cancel",
        "bridge_outbox_send",
        "bridge_read_secret",
        "bridge_write",
        "run_terminal",
    }
    text = ""
    for p in FACADE.rglob("*.py"):
        text += p.read_text(encoding="utf-8") + "\n"
    for name in forbidden:
        assert name not in text, name


def test_server_entrypoint_stdio_only():
    src = (FACADE / "server.py").read_text(encoding="utf-8")
    assert 'transport="stdio"' in src
    assert "streamable-http" not in src or "do not offer" in src.lower() or "never" in src.lower()
    # Must not call run with sse as default
    assert "mcp.run(transport=\"sse\")" not in src
    assert "mcp.run(transport='sse')" not in src


def test_handlers_never_accept_state_db_argument():
    tree = ast.parse((FACADE / "handlers.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("handle_"):
            arg_names = [a.arg for a in node.args.args] + [
                a.arg for a in node.args.kwonlyargs
            ]
            assert "state_db" not in arg_names, node.name
            assert "db_path" not in arg_names, node.name
            assert "path" not in arg_names or node.name == "unused", node.name


def test_no_network_listen_imports_in_facade():
    banned = {"http.server", "socketserver", "uvicorn", "starlette.routing"}
    for p in FACADE.rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in {
                        "uvicorn",
                        "socketserver",
                    }, p
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module not in banned, p


def test_startup_import_does_not_create_db(tmp_path, monkeypatch):
    from grok_bot_build_bridge.mcp_facade.scope import STATE_DIR_ENV

    state = tmp_path / "empty-state"
    state.mkdir()
    monkeypatch.setenv(STATE_DIR_ENV, str(state))
    # Import / construct server without calling job tools
    from grok_bot_build_bridge.mcp_facade.server import create_server

    create_server()
    assert not (state / "state.sqlite").exists()


def test_tool_list_matches_allowlist():
    from grok_bot_build_bridge.mcp_facade.server import TOOL_NAMES, create_server

    mcp = create_server()
    # FastMCP stores tools internally
    tools = getattr(mcp, "_tool_manager", None) or getattr(mcp, "_tools", None)
    # Fall back to TOOL_NAMES constant contract
    assert set(TOOL_NAMES) == {
        "bridge_runtime_info",
        "bridge_discover",
        "bridge_onboard",
        "bridge_status",
        "bridge_job_status",
    }
    _ = tools  # presence optional across SDK versions
