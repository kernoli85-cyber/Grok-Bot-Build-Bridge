"""Authenticated streamable-HTTP ASGI app factory (no listener)."""

from __future__ import annotations

import logging
from typing import Any

from .http_auth import AuthHygieneMiddleware, StaticBearerTokenVerifier
from .http_config import HttpConfigError, HttpMcpConfig, load_http_config_from_environ
from .tools import INSTRUCTIONS, register_bridge_tools

logger = logging.getLogger("grok_bot_build_bridge.mcp_http")


def create_streamable_http_app(
    config: HttpMcpConfig | None = None,
    *,
    _config: HttpMcpConfig | None = None,
) -> Any:
    """Return a Starlette ASGI app for MCP Streamable HTTP.

    Does **not** bind a socket. Mount behind a trusted HTTPS reverse proxy
    (or private-CA TLS terminator). Cleartext non-loopback is rejected at config load.

    Fail-closed: missing/invalid static bearer or allowlists raise HttpConfigError.
    """
    if _config is not None and config is not None:
        raise TypeError("pass only one config")
    cfg = _config or config or load_http_config_from_environ()

    try:
        from mcp.server.auth.settings import AuthSettings
        from mcp.server.fastmcp import FastMCP
        from mcp.server.transport_security import TransportSecuritySettings
        from pydantic import AnyHttpUrl
    except ImportError as exc:  # pragma: no cover
        raise HttpConfigError(
            "mcp_extra_missing",
            "pip install 'grok-bot-build-bridge[mcp]' (mcp>=1.28,<2)",
        ) from exc

    # Never log the bearer. Keep mcp auth log noise away from stdout.
    logging.getLogger("mcp.server.auth").setLevel(logging.WARNING)

    verifier = StaticBearerTokenVerifier(cfg)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(cfg.allowed_hosts),
        allowed_origins=list(cfg.allowed_origins),
    )

    # AuthSettings is required by FastMCP whenever token_verifier is set.
    # issuer_url / resource_server_url feed WWW-Authenticate + protected-resource
    # metadata routes in the SDK — NOT a claim that we operate marketplace OAuth.
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(cfg.issuer_url),
        resource_server_url=AnyHttpUrl(cfg.resource_url),
        # Verifier sets AccessToken.resource; enable SDK audience check too.
        validate_token_resource=True,
        required_scopes=None,
    )

    mcp = FastMCP(
        "grok-bot-build-bridge",
        instructions=INSTRUCTIONS,
        token_verifier=verifier,
        auth=auth,
        transport_security=security,
        # host only influences FastMCP's default security when None; we pass explicit security.
        host="127.0.0.1",
        streamable_http_path=cfg.stream_path,
        json_response=True,
        stateless_http=False,
        max_request_body_size=cfg.max_request_body_size,
        max_sessions=cfg.max_sessions,
        session_idle_timeout=cfg.session_idle_timeout,
    )
    register_bridge_tools(mcp)
    inner = mcp.streamable_http_app()
    return AuthHygieneMiddleware(inner)


def create_streamable_http_app_from_environ() -> Any:
    return create_streamable_http_app()
