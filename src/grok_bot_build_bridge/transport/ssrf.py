"""DNS resolve-once + IP pin for HTTPS webhook SSRF defense.

Design (stdlib only):
1. Resolve hostname with a bounded deadline (thread + timeout).
2. Fail closed if *any* answer is non-global (private/loopback/link-local/
   multicast/reserved/unspecified/IPv4-mapped-to-non-global/zone scope).
3. Connect TCP to a selected approved sockaddr only (no second DNS).
4. TLS wrap with server_hostname=original hostname (SNI + cert verify).
5. HTTP Host remains the original hostname via HTTPSConnection.host.

Does not disable certificate verification, proxies, or redirect policy.
Does not bypass OS/network controls — code-level client protection only.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Callable, Sequence
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import HTTPSHandler, ProxyHandler, build_opener

DEFAULT_RESOLVE_DEADLINE_SEC = 5.0
DEFAULT_TOTAL_DEADLINE_SEC = 15.0
MAX_ADDR_CANDIDATES = 8

Resolver = Callable[..., list]


class SsrfError(URLError):
    """Sanitized SSRF/DNS failure — reason is a short code, never raw IPs/URLs."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.ssrf_code = code


def _unwrap_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address):
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def ip_is_globally_routable(ip_str: str) -> bool:
    """True only for global unicast suitable for webhook egress."""
    try:
        ip = _unwrap_ip(ipaddress.ip_address(ip_str))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and getattr(ip, "is_site_local", False):
        return False
    if isinstance(ip, ipaddress.IPv6Address) and (int(ip) >> 120) in (0xFC, 0xFD):
        return False
    # Prefer stdlib is_global (False for private/loopback/link-local/…)
    if hasattr(ip, "is_global"):
        return bool(ip.is_global)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )



def host_blocked_for_webhook(hostname: str) -> bool:
    """Block loopback/link-local/private/metadata literal hostnames."""
    h = (hostname or "").lower().rstrip(".")
    if not h:
        return True
    if h in {"localhost", "metadata", "metadata.google.internal"}:
        return True
    if h.endswith(".localhost") or h.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return not ip_is_globally_routable(str(ip))


@dataclass(frozen=True)
class ApprovedAddress:
    family: int
    sockaddr: tuple


def resolve_approved_addresses(
    hostname: str,
    port: int,
    *,
    deadline_sec: float = DEFAULT_RESOLVE_DEADLINE_SEC,
    resolver: Resolver | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[ApprovedAddress, ...]:
    """Resolve once; reject mixed/non-global/zone answers entirely."""
    if not hostname or host_blocked_for_webhook(hostname):
        raise SsrfError("dns_host_forbidden")
    if port <= 0 or port > 65535:
        raise SsrfError("dns_port_invalid")
    if deadline_sec <= 0 or deadline_sec > 60:
        raise SsrfError("dns_deadline_invalid")

    resolve = resolver or socket.getaddrinfo
    remaining = deadline_sec
    started = clock()

    def _call() -> list:
        return resolve(hostname, port, type=socket.SOCK_STREAM)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_call)
            infos = fut.result(timeout=remaining)
    except FuturesTimeout as exc:
        raise SsrfError("dns_timeout") from exc
    except socket.gaierror as exc:
        raise SsrfError("dns_resolve_failed") from exc
    except OSError as exc:
        raise SsrfError("dns_resolve_failed") from exc

    if clock() - started > deadline_sec + 0.5:
        # Extreme overrun — fail closed
        raise SsrfError("dns_timeout")

    if not infos:
        raise SsrfError("dns_empty")

    approved: list[ApprovedAddress] = []
    saw_bad = False
    for family, _type, _proto, _canon, sockaddr in infos:
        if family not in (socket.AF_INET, socket.AF_INET6):
            saw_bad = True
            continue
        if family == socket.AF_INET6:
            # (addr, port, flowinfo, scope_id)
            if len(sockaddr) >= 4 and int(sockaddr[3]) != 0:
                saw_bad = True  # zone id
                continue
            ip_str = sockaddr[0]
        else:
            ip_str = sockaddr[0]
        if "%" in str(ip_str):  # textual zone
            saw_bad = True
            continue
        if not ip_is_globally_routable(str(ip_str)):
            saw_bad = True
            continue
        approved.append(ApprovedAddress(family=family, sockaddr=tuple(sockaddr)))

    if not approved:
        raise SsrfError("dns_no_global")
    if saw_bad:
        # Any non-global alongside globals → fail closed (DNS rebinding)
        raise SsrfError("dns_mixed_or_blocked")
    return tuple(approved[:MAX_ADDR_CANDIDATES])


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TCP to pinned sockaddr; TLS/SNI/cert verify for original hostname."""

    def __init__(
        self,
        host: str,
        port: int | None = None,
        *,
        pinned: ApprovedAddress,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
        **kwargs,
    ) -> None:
        # host must be the original hostname (Host header + server_hostname)
        super().__init__(host, port=port, timeout=timeout, context=context, **kwargs)
        self._pinned = pinned

    def connect(self) -> None:  # noqa: D401 — overrides stdlib
        """Connect to pinned IP only — never resolve self.host again."""
        timeout = self.timeout
        if timeout is socket._GLOBAL_DEFAULT_TIMEOUT:  # type: ignore[attr-defined]
            timeout = socket.getdefaulttimeout()
        sock = socket.socket(self._pinned.family, socket.SOCK_STREAM)
        try:
            if timeout is not None:
                sock.settimeout(timeout)
            sock.connect(self._pinned.sockaddr)
            server_hostname = self._tunnel_host or self.host
            # Preserve default verification — context from HTTPSHandler
            ctx = self._context
            if ctx is None:
                ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(sock, server_hostname=server_hostname)
            sock = None  # ownership transferred
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


class DnsPinnedHTTPSHandler(HTTPSHandler):
    """urllib HTTPS handler: resolve once, pin IP, keep hostname TLS identity."""

    def __init__(
        self,
        *,
        context: ssl.SSLContext | None = None,
        resolve_deadline_sec: float = DEFAULT_RESOLVE_DEADLINE_SEC,
        total_deadline_sec: float = DEFAULT_TOTAL_DEADLINE_SEC,
        resolver: Resolver | None = None,
        clock: Callable[[], float] = time.monotonic,
        check_hostname: bool = True,
    ) -> None:
        if context is None:
            context = ssl.create_default_context()
        # Never relax verification
        if context.verify_mode == ssl.CERT_NONE:
            raise ValueError("tls_verify_required")
        if check_hostname and hasattr(context, "check_hostname"):
            context.check_hostname = True
        super().__init__(context=context)
        self._resolve_deadline_sec = resolve_deadline_sec
        self._total_deadline_sec = total_deadline_sec
        self._resolver = resolver
        self._clock = clock

    def https_open(self, req):  # type: ignore[no-untyped-def]
        host = req.host
        if not host:
            raise SsrfError("dns_host_forbidden")
        # req.host may be "name:port"
        hostname, port = _split_hostport(host, default_port=443)
        if host_blocked_for_webhook(hostname):
            raise SsrfError("dns_host_forbidden")

        started = self._clock()
        total_deadline = started + self._total_deadline_sec
        resolve_budget = min(
            self._resolve_deadline_sec, max(0.05, total_deadline - self._clock())
        )
        approved = resolve_approved_addresses(
            hostname,
            port,
            deadline_sec=resolve_budget,
            resolver=self._resolver,
            clock=self._clock,
        )

        last_err: BaseException | None = None
        for addr in approved:
            if self._clock() >= total_deadline:
                raise SsrfError("connect_deadline")
            remaining = max(0.05, total_deadline - self._clock())

            def _factory(
                _host: str,
                _port: int | None = None,
                timeout: float | object = socket._GLOBAL_DEFAULT_TIMEOUT,  # type: ignore[attr-defined]
                **kwargs,
            ) -> PinnedHTTPSConnection:
                # Ignore urllib's host for TCP; pin IP; keep TLS name = hostname
                to = timeout
                if to is socket._GLOBAL_DEFAULT_TIMEOUT:  # type: ignore[attr-defined]
                    to = remaining
                elif isinstance(to, (int, float)):
                    to = min(float(to), remaining)
                else:
                    to = remaining
                return PinnedHTTPSConnection(
                    hostname,
                    port=port,
                    pinned=addr,
                    timeout=float(to) if to is not None else None,
                    context=self._context,
                )

            try:
                return self.do_open(_factory, req)
            except SsrfError:
                raise
            except ssl.SSLError as exc:
                # Cert mismatch / TLS failure is permanent for this target
                raise
            except OSError as exc:
                last_err = exc
                continue
            except URLError as exc:
                # Connection-level — try next approved address
                reason = getattr(exc, "reason", exc)
                if isinstance(reason, ssl.SSLError):
                    raise
                last_err = exc
                continue
        if last_err is not None:
            raise SsrfError("connect_failed") from last_err
        raise SsrfError("connect_failed")


def _split_hostport(host: str, *, default_port: int) -> tuple[str, int]:
    # Handle [ipv6]:port and name:port
    if host.startswith("["):
        # [addr]:port or [addr]
        end = host.find("]")
        if end < 0:
            raise SsrfError("dns_host_forbidden")
        hostname = host[1:end]
        rest = host[end + 1 :]
        if rest.startswith(":"):
            return hostname, int(rest[1:])
        return hostname, default_port
    if host.count(":") == 1:
        name, _, ps = host.partition(":")
        return name, int(ps)
    return host, default_port


def build_pinned_opener(
    *,
    context: ssl.SSLContext | None = None,
    resolve_deadline_sec: float = DEFAULT_RESOLVE_DEADLINE_SEC,
    total_deadline_sec: float = DEFAULT_TOTAL_DEADLINE_SEC,
    resolver: Resolver | None = None,
    clock: Callable[[], float] = time.monotonic,
):
    """Default webhook opener: no env proxy, no redirects, DNS-pinned HTTPS."""
    from .webhook import NoRedirect  # lazy: avoid import cycle

    https = DnsPinnedHTTPSHandler(
        context=context,
        resolve_deadline_sec=resolve_deadline_sec,
        total_deadline_sec=total_deadline_sec,
        resolver=resolver,
        clock=clock,
    )
    proxy = ProxyHandler({})
    return build_opener(proxy, https, NoRedirect)


def hostname_port_from_url(url: str) -> tuple[str, int]:
    p = urlparse(url)
    host = p.hostname
    if not host:
        raise SsrfError("dns_host_forbidden")
    port = p.port or 443
    return host, int(port)
