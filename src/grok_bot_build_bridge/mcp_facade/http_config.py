"""Fail-closed HTTP MCP configuration (static bearer + Host/Origin allowlists)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

BEARER_ENV = "GROK_BOT_BUILD_BRIDGE_MCP_BEARER_TOKEN"
BEARER_FILE_ENV = "GROK_BOT_BUILD_BRIDGE_MCP_BEARER_TOKEN_FILE"
RESOURCE_URL_ENV = "GROK_BOT_BUILD_BRIDGE_MCP_RESOURCE_URL"
ISSUER_URL_ENV = "GROK_BOT_BUILD_BRIDGE_MCP_ISSUER_URL"
ALLOWED_HOSTS_ENV = "GROK_BOT_BUILD_BRIDGE_MCP_ALLOWED_HOSTS"
ALLOWED_ORIGINS_ENV = "GROK_BOT_BUILD_BRIDGE_MCP_ALLOWED_ORIGINS"
ALLOW_INSECURE_LOOPBACK_ENV = "GROK_BOT_BUILD_BRIDGE_MCP_ALLOW_INSECURE_LOOPBACK"
STREAM_PATH_ENV = "GROK_BOT_BUILD_BRIDGE_MCP_HTTP_PATH"

DEFAULT_STREAM_PATH = "/mcp"
MIN_TOKEN_LEN = 16
MAX_TOKEN_LEN = 4096

_HOST_RE = re.compile(r"^[A-Za-z0-9._\[\]:-]{1,253}$")


class HttpConfigError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class HttpMcpConfig:
    """Validated settings for the streamable-HTTP ASGI factory.

    Auth model: **static bearer** only (AddMcpServer-style Authorization header).
    `issuer_url` / `resource_server_url` exist because the official MCP Python SDK
    AuthSettings requires them for WWW-Authenticate / protected-resource metadata —
    this is **not** marketplace OAuth and this package does not run an OAuth AS.
    """

    bearer_token: str
    resource_url: str
    issuer_url: str
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    stream_path: str
    allow_insecure_loopback: bool
    max_request_body_size: int = 1_048_576  # 1 MiB (stricter than SDK default)
    max_sessions: int = 32
    session_idle_timeout: float = 300.0

    def redacted_public(self) -> dict[str, Any]:
        return {
            "auth_mode": "static_bearer",
            "auth_note": (
                "Static bearer via Authorization header only. Not marketplace OAuth. "
                "SDK AuthSettings issuer/resource URLs are metadata scaffolding for "
                "WWW-Authenticate / protected-resource routes required by the SDK when "
                "token_verifier is enabled — not an authorization server we operate."
            ),
            "resource_url": self.resource_url,
            "issuer_url": self.issuer_url,
            "allowed_hosts": list(self.allowed_hosts),
            "allowed_origins": list(self.allowed_origins),
            "stream_path": self.stream_path,
            "allow_insecure_loopback": self.allow_insecure_loopback,
            "max_request_body_size": self.max_request_body_size,
            "max_sessions": self.max_sessions,
            "session_idle_timeout": self.session_idle_timeout,
            "bearer_configured": True,
            "bearer_length": len(self.bearer_token),
            "forwarded_headers": (
                "not_trusted: do not accept X-Forwarded-* from arbitrary clients; "
                "only a trusted reverse proxy may set Host / TLS semantics"
            ),
            "cleartext_policy": (
                "resource_url must be https unless allow_insecure_loopback and "
                "loopback Host only"
            ),
            "cors": "no_wildcard; Origin allowlist only (absent Origin allowed by SDK)",
            "listener": "factory_returns_asgi_only_no_bind",
        }


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if raw is None or raw.strip() == "":
        return ()
    out: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if item:
            out.append(item)
    return tuple(out)


def _reject_wildcards(values: tuple[str, ...], *, kind: str) -> None:
    for v in values:
        if "*" in v or v == "":
            raise HttpConfigError(
                "wildcard_forbidden",
                f"{kind} allowlist must not contain wildcards or empties: {v!r}",
            )


def _validate_hosts(hosts: tuple[str, ...]) -> None:
    if not hosts:
        raise HttpConfigError("hosts_required", "allowed_hosts must be non-empty")
    _reject_wildcards(hosts, kind="hosts")
    for h in hosts:
        if not _HOST_RE.match(h):
            raise HttpConfigError("invalid_host", f"invalid Host allowlist entry: {h!r}")
        if h.endswith(":*"):
            raise HttpConfigError("wildcard_forbidden", "host:* patterns are not allowed")


def _validate_origins(origins: tuple[str, ...]) -> None:
    # Empty origins is OK: browser Origin present will fail closed; non-browser OK.
    _reject_wildcards(origins, kind="origins")
    for o in origins:
        parsed = urlparse(o)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HttpConfigError("invalid_origin", f"invalid Origin allowlist entry: {o!r}")
        if "*" in o:
            raise HttpConfigError("wildcard_forbidden", "origin wildcards are not allowed")


def _is_loopback_host(host: str) -> bool:
    base = host.split(":")[0].strip("[]").lower()
    return base in {"127.0.0.1", "localhost", "::1"}


def _load_bearer() -> str:
    file_raw = os.environ.get(BEARER_FILE_ENV)
    env_raw = os.environ.get(BEARER_ENV)
    if file_raw and env_raw:
        raise HttpConfigError(
            "bearer_ambiguous",
            f"set only one of {BEARER_ENV} or {BEARER_FILE_ENV}",
        )
    if file_raw:
        text = file_raw.strip()
        if "\x00" in text or ".." in Path(text).parts or not Path(text).is_absolute():
            raise HttpConfigError(
                "invalid_bearer_file",
                f"{BEARER_FILE_ENV} must be an absolute path without '..'",
            )
        path = Path(text)
        if not path.is_file():
            raise HttpConfigError("bearer_file_absent", "bearer token file does not exist")
        # Read once; strip one trailing newline only (common secret-file convention).
        data = path.read_bytes()
        if b"\x00" in data:
            raise HttpConfigError("invalid_bearer", "NUL in bearer token file")
        token = data.decode("utf-8")
        if token.endswith("\n"):
            token = token[:-1]
        if token.endswith("\r"):
            token = token[:-1]
    elif env_raw is not None:
        token = env_raw
    else:
        raise HttpConfigError(
            "bearer_required",
            f"fail-closed: configure {BEARER_ENV} or {BEARER_FILE_ENV}",
        )

    if not token or token.strip() != token:
        raise HttpConfigError(
            "invalid_bearer",
            "bearer must be non-empty without leading/trailing whitespace",
        )
    if len(token) < MIN_TOKEN_LEN or len(token) > MAX_TOKEN_LEN:
        raise HttpConfigError(
            "invalid_bearer",
            f"bearer length must be between {MIN_TOKEN_LEN} and {MAX_TOKEN_LEN}",
        )
    if any(ord(ch) < 32 for ch in token):
        raise HttpConfigError("invalid_bearer", "bearer must not contain control characters")
    return token


def load_http_config_from_environ() -> HttpMcpConfig:
    """Load config from environment. Never generates or retrieves user secrets."""
    bearer = _load_bearer()
    allow_insecure = os.environ.get(ALLOW_INSECURE_LOOPBACK_ENV, "").strip() in {
        "1",
        "true",
        "yes",
    }
    resource = os.environ.get(RESOURCE_URL_ENV, "").strip()
    if not resource:
        raise HttpConfigError(
            "resource_url_required",
            f"fail-closed: set {RESOURCE_URL_ENV} to the public MCP resource URL",
        )
    # Normalize without forcing trailing slash inconsistently — store canonical form.
    parsed_r = urlparse(resource)
    if parsed_r.scheme not in {"http", "https"} or not parsed_r.netloc:
        raise HttpConfigError("invalid_resource_url", "resource_url must be absolute http(s)")
    if parsed_r.query or parsed_r.fragment:
        raise HttpConfigError("invalid_resource_url", "resource_url must not include query/fragment")
    host = parsed_r.hostname or ""
    if parsed_r.scheme == "http" and not (allow_insecure and _is_loopback_host(host)):
        raise HttpConfigError(
            "cleartext_forbidden",
            "http resource_url only allowed for loopback with "
            f"{ALLOW_INSECURE_LOOPBACK_ENV}=1; production requires https",
        )

    issuer = os.environ.get(ISSUER_URL_ENV, "").strip() or f"{parsed_r.scheme}://{parsed_r.netloc}/"
    parsed_i = urlparse(issuer)
    if parsed_i.scheme not in {"http", "https"} or not parsed_i.netloc:
        raise HttpConfigError("invalid_issuer_url", "issuer_url must be absolute http(s)")
    if parsed_i.scheme == "http" and not (allow_insecure and _is_loopback_host(parsed_i.hostname or "")):
        raise HttpConfigError(
            "cleartext_forbidden",
            "http issuer_url only allowed for loopback with insecure loopback flag",
        )

    hosts = _split_csv(os.environ.get(ALLOWED_HOSTS_ENV))
    origins = _split_csv(os.environ.get(ALLOWED_ORIGINS_ENV))
    _validate_hosts(hosts)
    _validate_origins(origins)

    path = os.environ.get(STREAM_PATH_ENV, DEFAULT_STREAM_PATH).strip() or DEFAULT_STREAM_PATH
    if not path.startswith("/") or ".." in path or "?" in path or "#" in path or " " in path:
        raise HttpConfigError("invalid_stream_path", "stream path must be a simple absolute path")

    return HttpMcpConfig(
        bearer_token=bearer,
        resource_url=resource,
        issuer_url=issuer,
        allowed_hosts=hosts,
        allowed_origins=origins,
        stream_path=path,
        allow_insecure_loopback=allow_insecure,
    )


def check_http_config() -> dict[str, Any]:
    """Dry-run config validation for CLI — never starts a listener."""
    try:
        cfg = load_http_config_from_environ()
    except HttpConfigError as exc:
        return {
            "ok": False,
            "error": {"code": exc.code, "message": exc.message},
            "listener": False,
        }
    pub = cfg.redacted_public()
    pub["ok"] = True
    pub["listener"] = False
    return pub
