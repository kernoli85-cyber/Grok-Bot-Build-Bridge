"""DNS resolve-once + IP pin: no second DNS; TLS keeps original hostname."""
from __future__ import annotations

import socket
import ssl
from unittest.mock import MagicMock, patch

import pytest

from grok_bot_build_bridge.transport.ssrf import (
    ApprovedAddress,
    DnsPinnedHTTPSHandler,
    PinnedHTTPSConnection,
    SsrfError,
    build_pinned_opener,
    host_blocked_for_webhook,
    ip_is_globally_routable,
    resolve_approved_addresses,
)
from grok_bot_build_bridge.transport.webhook import WebhookConfig, WebhookTransport


def test_ip_policy_rejects_private_mapped_loopback():
    assert ip_is_globally_routable("8.8.8.8")
    assert not ip_is_globally_routable("10.1.2.3")
    assert not ip_is_globally_routable("127.0.0.1")
    assert not ip_is_globally_routable("169.254.169.254")
    assert not ip_is_globally_routable("::1")
    assert not ip_is_globally_routable("::ffff:10.0.0.1")
    assert not ip_is_globally_routable("::ffff:127.0.0.1")
    assert not ip_is_globally_routable("fc00::1")
    assert host_blocked_for_webhook("127.0.0.1")
    assert host_blocked_for_webhook("localhost")
    assert not host_blocked_for_webhook("hooks.example.com")


def test_resolve_rejects_mixed_global_and_private():
    def mixed(host, port, type=0, *a, **k):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port)),
        ]

    with pytest.raises(SsrfError) as ei:
        resolve_approved_addresses("hooks.example.com", 443, resolver=mixed)
    assert ei.value.ssrf_code == "dns_mixed_or_blocked"


def test_resolve_rejects_ipv6_zone_scope():
    def zoned(host, port, type=0, *a, **k):
        return [
            (
                socket.AF_INET6,
                socket.SOCK_STREAM,
                6,
                "",
                ("fe80::1", port, 0, 3),
            )
        ]

    with pytest.raises(SsrfError) as ei:
        resolve_approved_addresses("hooks.example.com", 443, resolver=zoned)
    assert ei.value.ssrf_code == "dns_no_global"


def test_pinned_connect_uses_ip_and_hostname_sni_no_second_dns():
    connect_args: list = []

    class FakeSock:
        def settimeout(self, t):
            pass

        def connect(self, addr):
            connect_args.append(addr)

        def close(self):
            pass

    def fake_socket(family, type=socket.SOCK_STREAM, proto=0, *a, **k):
        assert family == socket.AF_INET
        return FakeSock()

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    wrap_args: list = []

    def fake_wrap(sock, server_hostname=None, **kwargs):
        wrap_args.append({"server_hostname": server_hostname})
        return MagicMock()

    ctx.wrap_socket = fake_wrap  # type: ignore[method-assign]

    pinned = ApprovedAddress(family=socket.AF_INET, sockaddr=("8.8.8.8", 443))
    conn = PinnedHTTPSConnection(
        "hooks.example.com",
        port=443,
        pinned=pinned,
        timeout=2.0,
        context=ctx,
    )
    with patch("grok_bot_build_bridge.transport.ssrf.socket.socket", fake_socket):
        conn.connect()

    assert connect_args == [("8.8.8.8", 443)]
    assert wrap_args == [{"server_hostname": "hooks.example.com"}]


def test_handler_resolve_once_then_pin_no_second_getaddrinfo():
    resolve_calls: list = []

    def resolver(host, port, type=0, *a, **k):
        resolve_calls.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

    ctx = ssl.create_default_context()
    handler = DnsPinnedHTTPSHandler(
        context=ctx,
        resolver=resolver,
        resolve_deadline_sec=2.0,
        total_deadline_sec=5.0,
    )

    def capturing_do_open(factory, req, **kw):
        conn = factory("hooks.example.com", timeout=1.0)
        assert isinstance(conn, PinnedHTTPSConnection)
        assert conn.host == "hooks.example.com"
        assert conn._pinned.sockaddr == ("8.8.8.8", 443)
        before = len(resolve_calls)
        with patch(
            "grok_bot_build_bridge.transport.ssrf.socket.socket"
        ) as sock_cls:
            sock = MagicMock()
            sock_cls.return_value = sock
            handler._context.wrap_socket = MagicMock(return_value=MagicMock())  # type: ignore[method-assign]
            conn.connect()
            sock.connect.assert_called_once_with(("8.8.8.8", 443))
            assert handler._context.wrap_socket.call_args.kwargs["server_hostname"] == (
                "hooks.example.com"
            )
        assert len(resolve_calls) == before
        resp = MagicMock()
        resp.status = 204
        return resp

    import urllib.request

    req = urllib.request.Request(
        "https://hooks.example.com/hook",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with patch.object(handler, "do_open", side_effect=capturing_do_open):
        out = handler.https_open(req)
    assert out.status == 204
    assert resolve_calls == [("hooks.example.com", 443)]


def test_multi_address_retries_only_approved():
    def resolver(host, port, type=0, *a, **k):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
        ]

    ctx = ssl.create_default_context()
    handler = DnsPinnedHTTPSHandler(
        context=ctx, resolver=resolver, total_deadline_sec=5.0
    )
    attempted: list = []

    def do_open_side_effect(factory, req, **kw):
        conn = factory("hooks.example.com", timeout=1.0)
        attempted.append(conn._pinned.sockaddr[0])
        if conn._pinned.sockaddr[0] == "1.1.1.1":
            raise OSError("simulated refuse")
        resp = MagicMock()
        resp.status = 200
        return resp

    import urllib.request

    req = urllib.request.Request(
        "https://hooks.example.com/h", data=b"{}", method="POST"
    )
    with patch.object(handler, "do_open", side_effect=do_open_side_effect):
        out = handler.https_open(req)
    assert out.status == 200
    assert attempted == ["1.1.1.1", "8.8.8.8"]


def test_transport_ssrf_error_sanitized_no_ip_leak():
    def priv(host, port, type=0, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]

    cfg = WebhookConfig(
        url="https://hooks.example.com/hook", bearer_token="sekrit-token-value"
    )
    opener = build_pinned_opener(resolver=priv)
    tr = WebhookTransport(cfg, opener=opener)
    result = tr.send_event_payload({"event_id": "e"})
    assert result.kind == "permanent"
    assert result.sanitized_error == "dns_no_global"
    assert "10.0.0.5" not in (result.sanitized_error or "")
    assert "sekrit" not in str(result)


def test_refuse_cert_none_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError, match="tls_verify_required"):
        DnsPinnedHTTPSHandler(context=ctx)


def test_default_webhook_transport_uses_dns_pinned_handler():
    cfg = WebhookConfig(url="https://hooks.example.com/hook", bearer_token="tok")
    tr = WebhookTransport(cfg)
    handlers = tr._opener.handlers  # type: ignore[attr-defined]
    assert any(isinstance(h, DnsPinnedHTTPSHandler) for h in handlers)
