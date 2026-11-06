"""Security review: HTTP MCP factory — no secret leaks, no wildcards, no shell tools."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FACADE = ROOT / "src" / "grok_bot_build_bridge" / "mcp_facade"
TEST_BEARER = "test-static-bearer-NOT-A-SECRET-0001"


def test_http_sources_never_log_authorization_header():
    for p in FACADE.glob("http_*.py"):
        text = p.read_text(encoding="utf-8")
        # crude: no logging of auth header values
        assert "Authorization]" not in text
        assert "bearer_token)" not in text or "len(" in text
        assert "logger.info(config.bearer" not in text
        assert "print(config.bearer" not in text


def test_http_app_has_no_listen_or_uvicorn_serve():
    text = (FACADE / "http_app.py").read_text(encoding="utf-8")
    assert "uvicorn" not in text
    assert ".serve(" not in text
    assert "bind(" not in text


def test_factory_does_not_accept_caller_state_paths():
    tree = ast.parse((FACADE / "http_app.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name.startswith("create_"):
            args = [a.arg for a in node.args.args + node.args.kwonlyargs]
            assert "state_db" not in args
            assert "db_path" not in args


def test_leak_scan_test_bearer_not_in_src_package():
    for p in (ROOT / "src").rglob("*"):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        assert TEST_BEARER not in text, p


def test_forwarded_headers_not_trusted_in_docs_and_code():
    cfg = (FACADE / "http_config.py").read_text(encoding="utf-8")
    assert "not_trusted" in cfg or "X-Forwarded" in cfg
    # No code reads X-Forwarded-For for allowlisting
    for p in FACADE.glob("http_*.py"):
        text = p.read_text(encoding="utf-8")
        assert "HTTP_X_FORWARDED" not in text
        assert "x-forwarded-host" not in text.lower() or "not" in text.lower()
