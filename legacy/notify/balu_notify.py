#!/usr/bin/env python3
"""Notify Balu via a Grok Bot webhook routine. Standalone; no hooks auto-install.

Reads URL + key from a mode-600 JSON config. Never prints secrets.
Sends only a small fixed JSON body (optional explicit --status text).
HTTPS only; redirects disabled; bounded timeout.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path.home() / ".config" / "balu-grok-build" / "webhook.json"
MAX_STATUS_CHARS = 500
TIMEOUT_SEC = 10
USER_AGENT = "balu-notify/1.0"


class NoRedirect(urllib.request.HTTPErrorProcessor):
    """Refuse 3xx; webhook target must be the final HTTPS URL."""

    def http_response(self, request, response):  # type: ignore[no-untyped-def]
        if 300 <= response.status < 400:
            raise urllib.error.HTTPError(
                request.full_url,
                response.status,
                f"redirect refused ({response.status})",
                response.headers,
                response,
            )
        return response

    https_response = http_response


def config_path_from_env() -> Path:
    override = os.environ.get("BALU_WEBHOOK_CONFIG")
    return Path(override) if override else DEFAULT_CONFIG


def load_config(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise SystemExit(
            f"config missing: {path}\nRun balu-notify-setup first."
        )
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise SystemExit(
            f"config permissions too open ({oct(mode)}): {path}\n"
            "Expected mode 600 (owner read/write only)."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"config is not valid JSON: {path}: {exc}") from exc
    url = data.get("url")
    key = data.get("key")
    if not isinstance(url, str) or not url.strip():
        raise SystemExit("config field 'url' missing or empty")
    if not isinstance(key, str) or not key.strip():
        raise SystemExit("config field 'key' missing or empty")
    url = url.strip()
    key = key.strip()
    if not url.lower().startswith("https://"):
        raise SystemExit("url must be https:// (http and other schemes refused)")
    return {"url": url, "key": key}


def build_payload(status: str | None, source: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "source": source,
        "event": "balu_notify",
        "version": 1,
    }
    if status is not None:
        text = status.strip()
        if len(text) > MAX_STATUS_CHARS:
            raise SystemExit(
                f"--status longer than {MAX_STATUS_CHARS} characters"
            )
        body["status"] = text
    else:
        body["status"] = "ping"
    return body


def post_webhook(
    url: str,
    key: str,
    payload: dict[str, Any],
    *,
    timeout: float = TIMEOUT_SEC,
    opener: urllib.request.OpenerDirector | None = None,
) -> int:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {key}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        },
    )
    ctx = ssl.create_default_context()
    handlers: list[Any] = [NoRedirect()]
    if opener is None:
        opener = urllib.request.build_opener(
            *handlers,
            urllib.request.HTTPSHandler(context=ctx),
        )
    try:
        with opener.open(req, timeout=timeout) as resp:
            code = int(resp.status)
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        # Do not print response body (may echo secrets in some setups).
        print(f"HTTP {code}", file=sys.stderr)
        return code
    except urllib.error.URLError as exc:
        # Never include request headers / key in error text.
        reason = type(exc.reason).__name__ if exc.reason else "URLError"
        print(f"request failed: {reason}", file=sys.stderr)
        return 0
    except TimeoutError:
        print("request timed out", file=sys.stderr)
        return 0
    print(f"HTTP {code}")
    return code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="POST a small fixed JSON status to the Balu webhook routine."
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"config path (default: {DEFAULT_CONFIG} or $BALU_WEBHOOK_CONFIG)",
    )
    p.add_argument(
        "--status",
        default=None,
        help=f"explicit short status text (max {MAX_STATUS_CHARS} chars). "
        "Default status is 'ping'. Never pipes session transcripts.",
    )
    p.add_argument(
        "--source",
        default="balu-notify",
        help="value for JSON field 'source' (default: balu-notify)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="validate config and print payload shape without posting "
        "(never prints url/key)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = args.config if args.config is not None else config_path_from_env()
    cfg = load_config(path)
    payload = build_payload(args.status, args.source)
    if args.dry_run:
        # Show only non-secret shape.
        print(
            json.dumps(
                {
                    "ok": True,
                    "config": str(path),
                    "https": True,
                    "payload_keys": sorted(payload.keys()),
                    "status_len": len(str(payload.get("status", ""))),
                },
                ensure_ascii=False,
            )
        )
        return 0
    code = post_webhook(cfg["url"], cfg["key"], payload)
    return 0 if code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
