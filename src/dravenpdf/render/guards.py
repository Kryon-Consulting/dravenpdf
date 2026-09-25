"""Request filtering for renders (SSRF protection).

Every request a rendered page makes over HTTP(S) goes through :class:`RequestGuard`.
By default only hosts that resolve to public IP addresses are allowed; an optional
allowlist narrows that further (and is also how you opt specific internal hosts in).

Redirects need special care: Playwright only calls a route handler for the first URL
of a redirect chain, so a public URL could redirect the browser to an internal one
unseen. The guard therefore fetches HTTP(S) requests itself with redirects turned
off, checks every hop, and hands the browser only the final response.

WebSockets are routed separately by Playwright; the guard checks ``ws://`` and
``wss://`` URLs with the same host rules (as ``http``/``https``) and closes the ones
it blocks before any connection is made.

Credentials: when a render has :class:`~dravenpdf.render.auth.RenderAuth` headers,
the guard adds them per hop, only when that hop's exact origin (scheme, host, port)
is configured. Headers are rebuilt for every hop. The first hop carries exactly the
``Cookie`` header Chromium chose, or an empty one: without it, ``route.fetch`` would
add every stored cookie for the URL and skip SameSite, sending a page on another site
cookies Chromium withheld. After the first hop the browser's ``Cookie`` header is
dropped (Playwright then attaches the stored cookies for the new URL, without
SameSite), and ``Authorization`` is dropped once a redirect leaves the original
origin, as browsers do. So one origin's credentials never follow a redirect to another.

If the guard's own fetch fails (connection refused, DNS failure, reset), the request
is aborted as a network error, so the page sees a failed load instead of a request
that never finishes and holds the render until its deadline.

``file://`` URLs are blocked, except inside ``file_root`` (set by ``from_file`` to the
rendered file's folder, so its relative assets load but ``/etc/passwd`` does not).
Chromium also refuses ``file://`` loads from pages that are not ``file://`` pages.

Known limitation: the IP check and the real connection resolve DNS separately, so
a hostile DNS server could answer differently the second time (DNS rebinding).
Deployments that render untrusted content should also restrict outbound traffic
at the network level.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import url2pathname

from playwright.async_api import BrowserContext, Route, WebSocketRoute

from dravenpdf.errors import BlockedRequestError
from dravenpdf.render._redact import safe_message
from dravenpdf.render.auth import origin_of

if TYPE_CHECKING:
    from dravenpdf.render.assets import AssetBundle
    from dravenpdf.render.auth import RenderAuth

logger = logging.getLogger("dravenpdf.render.guards")

Resolver = Callable[[str], Awaitable[list[str]]]
BlockPolicy = Literal["fail", "skip"]

MAX_REDIRECTS = 10
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ALWAYS_ALLOWED_SCHEMES = frozenset({"data", "blob", "about"})
_WEBSOCKET_SCHEMES = {"ws": "http", "wss": "https"}
_POLICY_VIOLATION = 1008  # WebSocket close code
# Dropped from a request's own headers when a redirect leaves its original origin.
_ORIGIN_BOUND_HEADERS = frozenset({"authorization", "proxy-authorization"})
# Recomputed by Playwright for each fetch; never copied from the browser's request.
_TRANSPORT_HEADERS = frozenset({"host", "content-length"})


async def resolve_host(host: str) -> list[str]:
    """Resolve ``host`` to its IP addresses using the system resolver."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop IPv6 zone ids
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _normalize_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


class RequestGuard:
    """Decides which URLs a render may load, and enforces it on a browser context.

    Args:
        allowed_hosts: if given, only these hosts are allowed (and they are allowed
            even if they resolve to private addresses). ``"example.com"`` matches
            exactly; ``"*.example.com"`` matches any subdomain but not the apex.
        allow_private_network: allow any host, including private, loopback and
            link-local addresses. Only for trusted input. Ignored when
            ``allowed_hosts`` is set.
        file_root: allow ``file://`` URLs for files inside this folder.
        bundle: answer requests for the bundle origin from this in-memory bundle.
        auth: add its headers to requests for their exact origins (never others).
        resolver: DNS lookup function; replaceable in tests.
    """

    def __init__(
        self,
        *,
        allowed_hosts: Iterable[str] | None = None,
        allow_private_network: bool = False,
        file_root: str | os.PathLike[str] | None = None,
        bundle: AssetBundle | None = None,
        auth: RenderAuth | None = None,
        resolver: Resolver = resolve_host,
    ) -> None:
        self._bundle = bundle
        self._auth = auth
        self._exact: set[str] = set()
        self._suffixes: list[str] = []
        self._has_allowlist = allowed_hosts is not None
        for entry in allowed_hosts or ():
            host = _normalize_host(entry)
            if host.startswith("*."):
                self._suffixes.append(host[1:])  # keep the leading dot
            elif host:
                self._exact.add(host)
        self._allow_private = allow_private_network
        self._file_root = None if file_root is None else os.path.realpath(file_root)
        self._resolver = resolver
        self._dns_cache: dict[str, list[str]] = {}
        self.blocked: list[tuple[str, str]] = []

    @property
    def checks_requests(self) -> bool:
        """False when every URL is allowed, so no interception is needed."""
        return (
            self._has_allowlist
            or not self._allow_private
            or self._file_root is not None
            or self._bundle is not None
            or (self._auth is not None and self._auth.has_headers)
        )

    def _host_allowlisted(self, host: str) -> bool:
        return host in self._exact or any(host.endswith(s) for s in self._suffixes)

    async def check(self, url: str) -> None:
        """Raise :class:`BlockedRequestError` if ``url`` may not be loaded."""
        reason = await self._reason_to_block(url)
        if reason is not None:
            raise BlockedRequestError(f"blocked {url}: {reason}", url=url)

    async def _reason_to_block(self, url: str) -> str | None:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        scheme = _WEBSOCKET_SCHEMES.get(scheme, scheme)  # same host rules as HTTP
        if scheme in _ALWAYS_ALLOWED_SCHEMES:
            return None
        if scheme == "file" and self._file_root is not None:
            return await asyncio.to_thread(self._reason_to_block_file, parts.path)
        if scheme not in ("http", "https"):
            return f"scheme {scheme or '(none)'!r} is not allowed"
        host = _normalize_host(parts.hostname or "")
        if self._bundle is not None and self._bundle.owns(url):
            return None  # answered from memory by _handle
        if not host:
            return "URL has no host"
        if self._has_allowlist:
            return None if self._host_allowlisted(host) else "host is not on the allowlist"
        if self._allow_private:
            return None
        try:
            addresses = await self._resolve(host)
        except OSError as exc:
            return f"could not resolve host ({exc})"
        private = [a for a in addresses if not _is_public(a)]
        if private or not addresses:
            return f"host resolves to a non-public address ({', '.join(private) or 'none'})"
        return None

    def _reason_to_block_file(self, url_path: str) -> str | None:
        assert self._file_root is not None
        path = os.path.realpath(url2pathname(unquote(url_path)))
        if os.path.commonpath([path, self._file_root]) == self._file_root:
            return None
        return f"local file is outside {self._file_root}"

    async def _resolve(self, host: str) -> list[str]:
        try:
            return [str(ipaddress.ip_address(host.strip("[]")))]
        except ValueError:
            pass
        if host not in self._dns_cache:
            self._dns_cache[host] = await self._resolver(host)
        return self._dns_cache[host]

    def _block(self, url: str, reason: str) -> None:
        logger.warning("blocked request to %s: %s", url, reason)
        self.blocked.append((url, reason))

    def raise_if_blocked(self) -> None:
        """Raise for the first blocked request, if any."""
        if self.blocked:
            url, reason = self.blocked[0]
            more = f" (and {len(self.blocked) - 1} more)" if len(self.blocked) > 1 else ""
            raise BlockedRequestError(f"blocked {url}: {reason}{more}", url=url)

    async def install(self, context: BrowserContext) -> None:
        """Route every request and WebSocket the context makes through this guard."""
        if self.checks_requests:
            await context.route("**/*", self._handle)
            await context.route_web_socket(lambda _url: True, self._handle_websocket)

    async def _handle(self, route: Route) -> None:
        url = route.request.url
        try:
            if self._bundle is not None and self._bundle.owns(url):
                await self._bundle.fulfill(route)  # from memory, never the network
                return
            reason = await self._reason_to_block(url)
            if reason is not None:
                self._block(url, reason)
                await route.abort("blockedbyclient")
                return
            if urlsplit(url).scheme.lower() not in ("http", "https"):
                await route.continue_()
                return
            await self._fetch_checking_redirects(route, url)
        except Exception as exc:
            # Our fetch failed (refused, reset, DNS...) or the page went away. Always
            # answer the browser: an unanswered route hangs the render to its deadline.
            logger.info("request to %s failed: %s", url, safe_message(exc))
            with suppress(Exception):
                await route.abort("failed")

    async def _handle_websocket(self, ws: WebSocketRoute) -> None:
        reason = await self._reason_to_block(ws.url)
        if reason is not None:
            self._block(ws.url, reason)
            await ws.close(code=_POLICY_VIOLATION, reason="blocked by dravenpdf")
            return
        ws.connect_to_server()  # messages are forwarded both ways from here on

    def hop_headers(
        self, request_headers: dict[str, str], hop_url: str, first_url: str, *, first_hop: bool
    ) -> dict[str, str]:
        """The headers to send for one hop of a request (see the module docstring)."""
        cross_origin = origin_of(hop_url) != origin_of(first_url)
        headers: dict[str, str] = {}
        for name, value in request_headers.items():
            lowered = name.lower()
            if lowered in _TRANSPORT_HEADERS:
                continue
            if lowered == "cookie" and not first_hop:
                continue  # the cookie jar supplies the right cookies for this URL
            if cross_origin and lowered in _ORIGIN_BOUND_HEADERS:
                continue
            headers[lowered] = value
        if first_hop:
            # Exactly the cookies Chromium chose (SameSite, Secure, ...): with no Cookie
            # header, route.fetch would add every stored cookie for the URL itself.
            headers.setdefault("cookie", "")
        if self._auth is not None:
            headers.update(self._auth.headers_for(hop_url))
        return headers

    async def _fetch_checking_redirects(self, route: Route, url: str) -> None:
        method = route.request.method
        request_headers = route.request.headers
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            # Headers are rebuilt for every hop, so credentials meant for one origin
            # never reach another (route.fetch would otherwise reuse the first hop's).
            headers = self.hop_headers(request_headers, current, url, first_hop=hop == 0)
            if hop == 0:
                response = await route.fetch(headers=headers, max_redirects=0)
            else:
                response = await route.fetch(
                    url=current, method=method, headers=headers, max_redirects=0
                )
            location = response.headers.get("location")
            if response.status not in _REDIRECT_STATUSES or not location:
                await route.fulfill(response=response)
                return
            target = urljoin(current, location)
            reason = await self._reason_to_block(target)
            if reason is not None:
                self._block(target, f"{reason} (redirected from {url})")
                await route.abort("blockedbyclient")
                return
            if response.status == 303 or (response.status in (301, 302) and method == "POST"):
                method = "GET"
            current = target
        self._block(url, f"more than {MAX_REDIRECTS} redirects")
        await route.abort("blockedbyclient")
