"""In-process ASGI tests for authenticated streamable-HTTP MCP (no bind)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("mcp")
import httpx

from grok_bot_build_bridge.mcp_facade.http_app import create_streamable_http_app
from grok_bot_build_bridge.mcp_facade.http_config import (
    ALLOW_INSECURE_LOOPBACK_ENV,
    ALLOWED_HOSTS_ENV,
    ALLOWED_ORIGINS_ENV,
    BEARER_ENV,
    BEARER_FILE_ENV,
    HttpConfigError,
    RESOURCE_URL_ENV,
    ISSUER_URL_ENV,
    check_http_config,
    load_http_config_from_environ,
)
from grok_bot_build_bridge.mcp_facade.tools import TOOL_NAMES

# Fake static token for tests only — not a real user secret.
TEST_BEARER = "test-static-bearer-NOT-A-SECRET-0001"
RESOURCE = "http://127.0.0.1/mcp"
HOST = "127.0.0.1"

INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    },
}


def _env(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    monkeypatch.setenv(BEARER_ENV, TEST_BEARER)
    monkeypatch.setenv(RESOURCE_URL_ENV, RESOURCE)
    monkeypatch.setenv(ISSUER_URL_ENV, "http://127.0.0.1/")
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, HOST)
    monkeypatch.setenv(ALLOWED_ORIGINS_ENV, "http://127.0.0.1")
    monkeypatch.setenv(ALLOW_INSECURE_LOOPBACK_ENV, "1")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def _auth_headers(**more: str) -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {TEST_BEARER}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Host": HOST,
    }
    h.update(more)
    return h


async def _with_app(app, coro_fn):
    # AuthHygieneMiddleware wraps Starlette — lifespan on inner .app
    starlette = app.app
    async with starlette.router.lifespan_context(starlette):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await coro_fn(client)


def test_fail_closed_without_bearer(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(BEARER_ENV, raising=False)
    monkeypatch.delenv(BEARER_FILE_ENV, raising=False)
    monkeypatch.setenv(RESOURCE_URL_ENV, "https://mcp.example/mcp")
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "mcp.example")
    out = check_http_config()
    assert out["ok"] is False
    assert out["error"]["code"] == "bearer_required"
    assert out["listener"] is False


def test_cleartext_non_loopback_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(BEARER_ENV, TEST_BEARER)
    monkeypatch.setenv(RESOURCE_URL_ENV, "http://mcp.example/mcp")
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, "mcp.example")
    monkeypatch.delenv(ALLOW_INSECURE_LOOPBACK_ENV, raising=False)
    with pytest.raises(HttpConfigError) as ei:
        load_http_config_from_environ()
    assert ei.value.code == "cleartext_forbidden"


def test_wildcard_hosts_rejected(monkeypatch: pytest.MonkeyPatch):
    _env(monkeypatch, **{ALLOWED_HOSTS_ENV: "mcp.example:*"})
    with pytest.raises(HttpConfigError) as ei:
        load_http_config_from_environ()
    assert ei.value.code == "wildcard_forbidden"


def test_bearer_file_and_check_redacts_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    token_path = tmp_path / "bearer.txt"
    token_path.write_text(TEST_BEARER + "\n", encoding="utf-8")
    monkeypatch.delenv(BEARER_ENV, raising=False)
    monkeypatch.setenv(BEARER_FILE_ENV, str(token_path))
    monkeypatch.setenv(RESOURCE_URL_ENV, RESOURCE)
    monkeypatch.setenv(ALLOWED_HOSTS_ENV, HOST)
    monkeypatch.setenv(ALLOW_INSECURE_LOOPBACK_ENV, "1")
    pub = check_http_config()
    assert pub["ok"] is True
    blob = json.dumps(pub)
    assert TEST_BEARER not in blob
    assert pub["bearer_configured"] is True
    assert pub["listener"] is False


def test_unauthorized_initialize_list_call_and_session_reuse(monkeypatch: pytest.MonkeyPatch):
    _env(monkeypatch)
    app = create_streamable_http_app()

    async def body(client: httpx.AsyncClient):
        # No auth
        r = await client.post("/mcp", headers={"Host": HOST, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}, json=INIT_BODY)
        assert r.status_code == 401
        assert TEST_BEARER not in r.text
        assert "test-static" not in r.text

        # Wrong token
        r_bad = await client.post(
            "/mcp",
            headers={**_auth_headers(), "Authorization": "Bearer " + ("x" * len(TEST_BEARER))},
            json=INIT_BODY,
        )
        assert r_bad.status_code == 401
        assert TEST_BEARER not in r_bad.text

        # Malformed bearer
        r_mal = await client.post(
            "/mcp",
            headers={**_auth_headers(), "Authorization": "Bearer"},
            json=INIT_BODY,
        )
        assert r_mal.status_code == 401

        # Query credential rejected
        r_q = await client.post(
            f"/mcp?access_token={TEST_BEARER}",
            headers={"Host": HOST, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            json=INIT_BODY,
        )
        assert r_q.status_code == 401
        assert r_q.json()["error_description"] == "credentials_in_query_forbidden"

        # Authorized initialize
        r_ok = await client.post("/mcp", headers=_auth_headers(), json=INIT_BODY)
        assert r_ok.status_code == 200
        sid = r_ok.headers.get("mcp-session-id")
        assert sid
        assert TEST_BEARER not in r_ok.text

        # Session id reuse without Authorization must fail
        r_reuse = await client.post(
            "/mcp",
            headers={
                "Host": HOST,
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "mcp-session-id": sid,
            },
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert r_reuse.status_code == 401

        # tools/list with auth + session
        r_list = await client.post(
            "/mcp",
            headers={**_auth_headers(), "mcp-session-id": sid},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert r_list.status_code == 200
        # initialized notification may be required by protocol — if error, still no secret leak
        assert TEST_BEARER not in r_list.text

        # Bad Host
        r_host = await client.post("/mcp", headers={**_auth_headers(), "Host": "evil.example"}, json=INIT_BODY)
        assert r_host.status_code in {421, 400, 403}

        return True

    assert asyncio.run(_with_app(app, body)) is True


def test_duplicate_authorization_rejected(monkeypatch: pytest.MonkeyPatch):
    _env(monkeypatch)
    app = create_streamable_http_app()

    async def body():
        # Craft raw ASGI-level duplicate via httpx is hard; call middleware scope directly
        from grok_bot_build_bridge.mcp_facade.http_auth import AuthHygieneMiddleware

        statuses: list[int] = []

        async def send(msg):
            if msg["type"] == "http.response.start":
                statuses.append(msg["status"])

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "query_string": b"",
            "headers": [
                (b"authorization", f"Bearer {TEST_BEARER}".encode()),
                (b"authorization", f"Bearer {TEST_BEARER}".encode()),
                (b"host", HOST.encode()),
                (b"content-type", b"application/json"),
                (b"accept", b"application/json"),
            ],
            "client": ("127.0.0.1", 123),
            "server": ("127.0.0.1", 80),
            "scheme": "http",
        }

        async def never_app(scope, receive, send):
            raise AssertionError("inner must not run")

        mw = AuthHygieneMiddleware(never_app)
        await mw(scope, receive, send)
        assert statuses == [401]
        return True

    assert asyncio.run(body()) is True


def test_mcp_client_initialize_and_list_tools(monkeypatch: pytest.MonkeyPatch):
    """Real MCP streamable-HTTP client against in-process ASGI app."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    _env(monkeypatch)
    app = create_streamable_http_app()

    async def body():
        starlette = app.app
        async with starlette.router.lifespan_context(starlette):
            transport = httpx.ASGITransport(app=app)
            headers = {"Authorization": f"Bearer {TEST_BEARER}", "Host": HOST}
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
                headers=headers,
            ) as http_client:
                async with streamable_http_client("http://127.0.0.1/mcp", http_client=http_client) as (
                    read,
                    write,
                    _get_sid,
                ):
                    async with ClientSession(read, write) as session:
                        init = await session.initialize()
                        assert "grok-bot-build-bridge" in (init.serverInfo.name or "")
                        tools = await session.list_tools()
                        names = {t.name for t in tools.tools}
                        assert names == set(TOOL_NAMES)
                        result = await session.call_tool("bridge_runtime_info", {})
                        assert result.isError is False
                        # structured or text — must not echo bearer
                        blob = str(result)
                        assert TEST_BEARER not in blob
                        # unauthorized second client without header cannot use protocol
        # Fresh app path: client without auth gets HTTP 401 on initialize
        app2 = create_streamable_http_app()
        starlette2 = app2.app
        async with starlette2.router.lifespan_context(starlette2):
            transport2 = httpx.ASGITransport(app=app2)
            async with httpx.AsyncClient(transport=transport2, base_url="http://127.0.0.1") as bare:
                r = await bare.post(
                    "/mcp",
                    headers={
                        "Host": HOST,
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                    },
                    json=INIT_BODY,
                )
                assert r.status_code == 401

    asyncio.run(body())


def test_cli_mcp_http_check(monkeypatch: pytest.MonkeyPatch, capsys):
    from grok_bot_build_bridge.cli import main

    monkeypatch.delenv(BEARER_ENV, raising=False)
    monkeypatch.delenv(BEARER_FILE_ENV, raising=False)
    assert main(["mcp-http-check"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["listener"] is False

    _env(monkeypatch)
    assert main(["mcp-http-check"]) == 0
    out2 = json.loads(capsys.readouterr().out)
    assert out2["ok"] is True
    assert TEST_BEARER not in json.dumps(out2)
    assert out2["auth_mode"] == "static_bearer"


def test_no_serve_entrypoint_accidental_bind():
    """CLI must not expose a default listener start command in this slice."""
    from grok_bot_build_bridge.cli import build_parser

    help_text = build_parser().format_help()
    assert "mcp-http-check" in help_text
    assert "mcp-http-serve" not in help_text
    assert "uvicorn" not in help_text.lower()
