"""HTTPS webhook transport for outbox events.

Secure defaults:
- HTTPS only, no URL userinfo
- Never follow redirects (Bearer must not hop)
- Default TLS verification (never CERT_NONE)
- DNS resolve-once with deadline; reject mixed/non-global answers; pin IP for TCP
  while preserving original hostname for SNI / cert verify / Host
- Env HTTP(S)_PROXY suppressed; no redirect relaxation
- Bounded connect/read timeouts + max response body
- Sanitized errors (no URL/key/IP leakage in SSRF codes)
- 2xx => http_accepted only (NOT bot_processed)
- 401/403 => permanent; 429/5xx/network => transient (Retry-After bounded)
"""
from __future__ import annotations

import json
import re
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from .ssrf import build_pinned_opener, host_blocked_for_webhook

from ..state.paths import ensure_private_file, user_config_root

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 10.0
DEFAULT_MAX_BODY = 64 * 1024
DEFAULT_MAX_RETRY_AFTER = 60.0


class NoRedirect(urllib.request.HTTPErrorProcessor):
    """Refuse redirects so Authorization is never sent to another host/path blindly."""

    def http_response(self, request, response):  # type: ignore[no-untyped-def]
        if 300 <= response.status < 400:
            raise urllib.error.HTTPError(
                # Do not include full URL in message consumers might log — use placeholder
                "https://<redacted>",
                response.status,
                f"redirect_refused:{response.status}",
                response.headers,
                response,
            )
        return response

    https_response = http_response


@dataclass(frozen=True)
class WebhookConfig:
    url: str
    # bearer kept only in memory / private config file — never written to outbox/DB
    bearer_token: str

    def redacted(self) -> dict[str, str]:
        return {"url_scheme_host": _scheme_host(self.url), "token": "<redacted>"}


@dataclass(frozen=True)
class TransportResult:
    kind: str  # accepted | transient | permanent | error
    status_code: int | None = None
    retry_after_sec: float | None = None
    sanitized_error: str | None = None


def validate_webhook_url(url: str) -> str:
    if not url or not isinstance(url, str):
        raise ValueError("url_empty")
    raw = url.strip()
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "https":
        raise ValueError("url_must_be_https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url_userinfo_forbidden")
    if not parsed.hostname:
        raise ValueError("url_host_required")
    if host_blocked_for_webhook(parsed.hostname):
        raise ValueError("url_host_forbidden")
    if parsed.scheme.lower() == "https" and raw.lower().startswith("https://"):
        return raw
    raise ValueError("url_invalid")


def _scheme_host(url: str) -> str:
    try:
        p = urlparse(url)
        host = p.hostname or "<none>"
        return f"{p.scheme}://{host}"
    except Exception:
        return "https://<invalid>"


def sanitize_error(message: str) -> str:
    """Remove likely secrets / full URLs from error text."""
    text = (message or "").replace("\n", " ").strip()
    if text in {"tls_error", "timeout", "response_body_too_large"}:
        return text
    if text.startswith(("redirect_refused:", "http_", "network:")):
        return text[:240]
    parts: list[str] = []
    tokens = text.split()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        low = token.lower()
        if token.startswith(("redirect_refused:", "http_")):
            parts.append(token[:80])
            i += 1
            continue
        if low == "bearer" or low.startswith("bearer"):
            parts.append("Bearer <redacted>")
            if i + 1 < len(tokens) and "://" not in tokens[i + 1]:
                i += 2
                continue
            i += 1
            continue
        if "://" in token:
            parts.append(_scheme_host(token) if token.lower().startswith("http") else "<redacted_url>")
        elif len(token) >= 16 and any(c.isdigit() for c in token) and any(c.isalpha() for c in token):
            parts.append("<redacted>")
        else:
            parts.append(token[:80])
        i += 1
    return " ".join(parts)[:240]



def classify_http_status(code: int) -> str:
    if 200 <= code < 300:
        return "accepted"
    if code in {401, 403, 400, 404, 410, 422}:
        return "permanent"
    if code == 429 or code >= 500:
        return "transient"
    if 300 <= code < 400:
        return "permanent"  # redirects refused / unexpected
    return "transient"


def parse_retry_after(headers: Mapping[str, str] | None, *, cap: float = DEFAULT_MAX_RETRY_AFTER) -> float | None:
    if not headers:
        return None
    # headers may be HTTPMessage — case-insensitive
    raw = None
    try:
        raw = headers.get("Retry-After")  # type: ignore[arg-type]
    except Exception:
        raw = None
    if raw is None:
        for k, v in dict(headers).items():
            if str(k).lower() == "retry-after":
                raw = v
                break
    if raw is None:
        return None
    try:
        sec = float(str(raw).strip())
    except ValueError:
        return None
    if sec < 0:
        return None
    return min(sec, cap)


def default_webhook_config_path() -> Path:
    return user_config_root() / "webhook.json"


def save_webhook_config(path: Path, cfg: WebhookConfig) -> dict[str, str]:
    """Write config privately. Does not POST. Returns permission note fields."""
    validate_webhook_url(cfg.url)
    if not cfg.bearer_token or not cfg.bearer_token.strip():
        raise ValueError("token_empty")
    path = Path(path)
    note = ensure_private_file(path.parent, is_dir=True)
    payload = {"url": cfg.url, "bearer_token": cfg.bearer_token.strip()}
    # atomic-ish write
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    file_note = ensure_private_file(tmp, is_dir=False)
    os.replace(tmp, path)
    ensure_private_file(path, is_dir=False)
    return {
        "path": str(path),
        "permission_platform": file_note.platform,
        "chmod_applied": str(file_note.chmod_applied),
        "permission_guarantee": file_note.guarantee,
    }


def load_webhook_config(path: Path) -> WebhookConfig:
    """Load from an explicit path only — callers must not point at unrelated app DBs."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    url = validate_webhook_url(str(data.get("url", "")))
    token = str(data.get("bearer_token", "")).strip()
    if not token:
        raise ValueError("token_empty")
    return WebhookConfig(url=url, bearer_token=token)


UrlOpener = Callable[..., Any]


class WebhookTransport:
    def __init__(
        self,
        config: WebhookConfig,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        max_body: int = DEFAULT_MAX_BODY,
        max_retry_after: float = DEFAULT_MAX_RETRY_AFTER,
        opener: UrlOpener | None = None,
    ) -> None:
        self.config = config
        validate_webhook_url(config.url)
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_body = max_body
        self.max_retry_after = max_retry_after
        if opener is not None:
            self._opener = opener
        else:
            ctx = ssl.create_default_context()
            self._opener = build_pinned_opener(context=ctx)

    def send_event_payload(self, payload: dict[str, Any]) -> TransportResult:
        """POST JSON status payload. Never includes bearer in returned errors."""
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            self.config.url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.bearer_token}",
                "User-Agent": "grok-bot-build-bridge-webhook/0.4",
                "Accept": "application/json, text/plain, */*",
            },
        )
        # Single float: http.client does not accept (connect, read) tuples.
        timeout = float(self.connect_timeout) + float(self.read_timeout)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                status = int(getattr(resp, "status", None) or resp.getcode())
                # bound body read
                raw = resp.read(self.max_body + 1)
                if len(raw) > self.max_body:
                    return TransportResult(
                        kind="transient",
                        status_code=status,
                        sanitized_error="response_body_too_large",
                    )
                kind = classify_http_status(status)
                if kind == "accepted":
                    return TransportResult(kind="accepted", status_code=status)
                return TransportResult(
                    kind=kind,
                    status_code=status,
                    sanitized_error=f"http_{status}",
                )
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            # drain bounded
            try:
                exc.read(self.max_body)
            except Exception:
                pass
            kind = classify_http_status(status)
            if status in {301, 302, 303, 307, 308} or "redirect_refused" in str(exc.reason):
                kind = "permanent"
                err = f"redirect_refused:{status}"
            else:
                err = f"http_{status}"
            retry_after = parse_retry_after(exc.headers, cap=self.max_retry_after)
            return TransportResult(
                kind=kind,
                status_code=status,
                retry_after_sec=retry_after if kind == "transient" else None,
                sanitized_error=sanitize_error(err),
            )
        except ssl.SSLError:
            return TransportResult(kind="permanent", sanitized_error="tls_error")
        except TimeoutError:
            return TransportResult(kind="transient", sanitized_error="timeout")
        except urllib.error.URLError as exc:
            code = getattr(exc, "ssrf_code", None)
            if isinstance(code, str) and code:
                kind = "permanent" if code.startswith("dns_") or code.endswith("_forbidden") else "transient"
                if code in {"dns_timeout", "connect_deadline", "connect_failed"}:
                    kind = "transient"
                return TransportResult(kind=kind, sanitized_error=sanitize_error(code))
            reason = sanitize_error(str(getattr(exc, "reason", exc)))
            low = reason.lower()
            if "timed out" in low or "timeout" in low:
                return TransportResult(kind="transient", sanitized_error="timeout")
            return TransportResult(kind="transient", sanitized_error=f"network:{reason}")
        except Exception as exc:  # noqa: BLE001 — boundary
            return TransportResult(
                kind="error",
                sanitized_error=sanitize_error(f"{type(exc).__name__}"),
            )


def build_outbox_post_body(event: Any) -> dict[str, Any]:
    """Small status metadata only — never prompt/transcript/secret."""
    payload = dict(getattr(event, "payload", {}) or {})
    for bad in ("prompt", "transcript", "secret", "token", "key", "authorization", "webhook_key", "bearer"):
        payload.pop(bad, None)
    return {
        "schema_version": int(getattr(event, "schema_version", 1)),
        "event_id": str(getattr(event, "event_id")),
        "correlation_id": str(getattr(event, "correlation_id")),
        "task_id": str(getattr(event, "task_id")),
        "event_type": str(getattr(event, "event_type")),
        "payload": payload,
    }
