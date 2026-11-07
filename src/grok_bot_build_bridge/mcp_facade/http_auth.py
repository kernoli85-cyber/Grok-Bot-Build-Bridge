"""Static bearer TokenVerifier + ASGI hygiene (no token in query/logs)."""

from __future__ import annotations

import hmac
import logging
from typing import Any
from urllib.parse import parse_qs

from .http_config import HttpMcpConfig

logger = logging.getLogger("grok_bot_build_bridge.mcp_http")


class StaticBearerTokenVerifier:
    """Official-SDK TokenVerifier protocol: constant-time compare to configured secret.

    Does not implement OAuth. Suitable for AddMcpServer static Authorization headers.
    """

    def __init__(self, config: HttpMcpConfig) -> None:
        self._expected = config.bearer_token.encode("utf-8")
        self._resource = config.resource_url

    async def verify_token(self, token: str) -> Any | None:
        if not isinstance(token, str):
            return None
        # Reject obviously malformed before compare (still constant-time vs expected)
        candidate = token.encode("utf-8")
        # hmac.compare_digest requires equal length — pad/hash approach:
        # compare digests of both sides so length differs don't leak via exception path
        # while still failing closed for wrong tokens.
        ok = hmac.compare_digest(
            hmac.new(b"bridge-static-bearer", candidate, "sha256").digest(),
            hmac.new(b"bridge-static-bearer", self._expected, "sha256").digest(),
        )
        # Also require exact equality of raw bytes when lengths match to avoid
        # accepting a different preimage with same HMAC (theoretical); primary gate is HMAC.
        if not ok:
            return None
        if not hmac.compare_digest(candidate, self._expected):
            return None
        # Import lazily so stdio-only installs without pulling auth models at import time
        from mcp.server.auth.provider import AccessToken

        return AccessToken(
            token="accepted",  # never echo the real secret back through models/logs
            client_id="static-bearer",
            scopes=[],
            resource=self._resource,
            subject="static-bearer",
        )


class AuthHygieneMiddleware:
    """Reject duplicate Authorization, token-shaped query params, before MCP app.

    Does not log Authorization values or tokens.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers") or []
        auth_values = [v for (k, v) in headers if k.lower() == b"authorization"]
        if len(auth_values) > 1:
            await _send_json(send, 401, {"error": "invalid_request", "error_description": "duplicate_authorization"})
            return

        if len(auth_values) == 1:
            raw = auth_values[0]
            # Malformed schemes / empty bearer
            lower = raw.lower()
            if lower.startswith(b"bearer"):
                if not lower.startswith(b"bearer ") or len(raw) <= len(b"Bearer "):
                    await _send_json(
                        send,
                        401,
                        {"error": "invalid_token", "error_description": "malformed_authorization"},
                    )
                    return

        qs = scope.get("query_string") or b""
        if qs:
            params = parse_qs(qs.decode("utf-8", errors="replace"), keep_blank_values=True)
            banned = {"access_token", "token", "authorization", "bearer", "api_key", "apikey"}
            if banned.intersection(k.lower() for k in params):
                await _send_json(
                    send,
                    401,
                    {"error": "invalid_request", "error_description": "credentials_in_query_forbidden"},
                )
                return

        await self.app(scope, receive, send)


async def _send_json(send: Any, status: int, body: dict[str, str]) -> None:
    import json

    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(raw)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": raw})
