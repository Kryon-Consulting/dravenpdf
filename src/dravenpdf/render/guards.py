"""Shared render network policy and Chromium's local-file guard.

Every network request is checked by the per-render mitmproxy addon before reaching
its destination. This module freezes a policy snapshot for that process and keeps
Playwright's local-file root restriction, which HTTP proxies cannot enforce.
Chromium redirect chains are observed for reporting. A dedicated browser CDP
interceptor enforces the per-chain redirect cap while the proxy independently
checks every network destination.

The DNS check and the upstream connection resolve separately; deployments rendering
untrusted content should also restrict outbound network traffic.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from playwright.async_api import BrowserContext, Page, Route, WebSocket

from dravenpdf.errors import BlockedRequestError
from dravenpdf.render.auth import origin_of

if TYPE_CHECKING:
    from dravenpdf.render._proxy_protocol import ProxyPolicy
    from dravenpdf.render.assets import AssetBundle
    from dravenpdf.render.auth import RenderAuth

logger = logging.getLogger("dravenpdf.render.guards")

Resolver = Callable[[str], Awaitable[list[str]]]
BlockPolicy = Literal["fail", "skip"]

MAX_REDIRECTS = 10
_ALWAYS_ALLOWED_SCHEMES = frozenset({"data", "blob", "about"})
_WEBSOCKET_SCHEMES = {"ws": "http", "wss": "https"}


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


def _host_port(url: str) -> str | None:
    """``host:port`` as the proxy reports CONNECT targets, or None without a host."""
    parts = urlsplit(url)
    if parts.hostname is None:
        return None
    port = parts.port or (443 if parts.scheme in ("https", "wss") else 80)
    return f"{parts.hostname}:{port}"


def _raise_if(url: str, reason: str | None) -> None:
    if reason is not None:
        raise BlockedRequestError(f"blocked {url}: {reason}", url=url)


class NetworkPolicy:
    """The shared DNS and allowlist decision used by browser and proxy guards."""

    def __init__(
        self,
        *,
        allowed_hosts: Iterable[str] | None,
        allow_private_network: bool,
        bundle: AssetBundle | None,
        resolver: Resolver = resolve_host,
    ) -> None:
        self.exact: set[str] = set()
        self.suffixes: list[str] = []
        self.has_allowlist = allowed_hosts is not None
        for entry in allowed_hosts or ():
            host = _normalize_host(entry)
            if host.startswith("*."):
                self.suffixes.append(host[1:])
            elif host:
                self.exact.add(host)
        self.allow_private = allow_private_network
        self.bundle = bundle
        self.resolver = resolver
        self.dns_cache: dict[str, list[str]] = {}

    async def reason(self, url: str) -> str | None:
        parts = urlsplit(url)
        scheme = _WEBSOCKET_SCHEMES.get(parts.scheme.lower(), parts.scheme.lower())
        if scheme in _ALWAYS_ALLOWED_SCHEMES:
            return None
        if scheme not in ("http", "https"):
            return f"scheme {scheme or '(none)'!r} is not allowed"
        host = _normalize_host(parts.hostname or "")
        if self.bundle is not None and self.bundle.owns(url):
            return None
        if not host:
            return "URL has no host"
        if self.has_allowlist:
            allowed = host in self.exact or any(host.endswith(s) for s in self.suffixes)
            return None if allowed else "host is not on the allowlist"
        if self.allow_private:
            return None
        try:
            addresses = await self.resolve(host)
        except (PermissionError, TimeoutError):
            raise
        except OSError as exc:
            return f"could not resolve host ({exc})"
        private = [address for address in addresses if not _is_public(address)]
        if private or not addresses:
            return f"host resolves to a non-public address ({', '.join(private) or 'none'})"
        return None

    async def check(self, url: str) -> None:
        _raise_if(url, await self.reason(url))

    async def resolve(self, host: str) -> list[str]:
        try:
            return [str(ipaddress.ip_address(host.strip("[]")))]
        except ValueError:
            pass
        if host not in self.dns_cache:
            self.dns_cache[host] = await self.resolver(host)
        return self.dns_cache[host]


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
        self.network = NetworkPolicy(
            allowed_hosts=allowed_hosts,
            allow_private_network=allow_private_network,
            bundle=bundle,
            resolver=resolver,
        )
        self._file_root = None if file_root is None else os.path.realpath(file_root)
        self.blocked: list[tuple[str, str]] = []
        self._redirect_sources: dict[str, str] = {}
        self._observed_urls: dict[str, str] = {}

    def proxy_policy(self, credential: str, deadline: float | None) -> ProxyPolicy:
        """Freeze the network policy and response data for one proxy process."""
        from dravenpdf.render._proxy_protocol import ProxyPolicy

        allowed = None
        if self.network.has_allowlist:
            allowed = sorted(self.network.exact) + [
                f"*{suffix}" for suffix in self.network.suffixes
            ]
        headers = (
            {
                origin: {name: value.get_secret_value() for name, value in values.items()}
                for origin, values in self._auth.headers.items()
            }
            if self._auth is not None
            else {}
        )
        return ProxyPolicy(
            allowed_hosts=allowed,
            allow_private_network=self.network.allow_private,
            bundle=self._bundle.snapshot() if self._bundle is not None else None,
            headers=headers,
            credential=credential,
            deadline=deadline,
        )

    async def check(self, url: str) -> None:
        """Raise :class:`BlockedRequestError` if ``url`` may not be loaded."""
        _raise_if(url, await self._reason_to_block(url))

    async def _reason_to_block(self, url: str) -> str | None:
        parts = urlsplit(url)
        if parts.scheme.lower() == "file" and self._file_root is not None:
            return await asyncio.to_thread(self._reason_to_block_file, parts.path)
        return await self.network.reason(url)

    def _reason_to_block_file(self, url_path: str) -> str | None:
        assert self._file_root is not None
        path = os.path.realpath(url2pathname(unquote(url_path)))
        if os.path.commonpath([path, self._file_root]) == self._file_root:
            return None
        return f"local file is outside {self._file_root}"

    def _block(self, url: str, reason: str) -> None:
        logger.warning("blocked request to %s: %s", url, reason)
        self.blocked.append((url, reason))

    def blocked_error(self) -> BlockedRequestError | None:
        """The error for the first blocked request, or None if nothing was blocked."""
        if not self.blocked:
            return None
        url, reason = self.blocked[0]
        more = f" (and {len(self.blocked) - 1} more)" if len(self.blocked) > 1 else ""
        return BlockedRequestError(f"blocked {url}: {reason}{more}", url=url)

    def raise_if_blocked(self) -> None:
        """Raise for the first blocked request, if any."""
        if (error := self.blocked_error()) is not None:
            raise error

    async def install(self, context: BrowserContext) -> None:
        """Keep local-file limits and observe redirects; the proxy guards network."""
        if self._file_root is not None:
            await context.route("**/*", self._handle_local)
        context.on("request", self._observe_request)

    def observe_page(self, page: Page) -> None:
        page.on("websocket", self._observe_websocket)

    def _observe_websocket(self, ws: WebSocket) -> None:
        if (target := _host_port(ws.url)) is not None:
            self._observed_urls[target] = ws.url

    async def _handle_local(self, route: Route) -> None:
        url = route.request.url
        if urlsplit(url).scheme.lower() == "file":
            reason = await self._reason_to_block(url)
            if reason is not None:
                self._block(url, reason)
                await route.abort("blockedbyclient")
                return
        await route.continue_()

    def _observe_request(self, request: object) -> None:
        """Record chains past the public ten-redirect limit."""
        from playwright.async_api import Request

        assert isinstance(request, Request)
        parent = request.redirected_from
        origin = origin_of(request.url)
        if origin is not None:
            self._observed_urls[origin] = request.url
            if (target := _host_port(request.url)) is not None:
                self._observed_urls[target] = request.url
        if parent is not None and origin is not None:
            self._redirect_sources[origin] = origin_of(parent.url) or "unknown"
        hops = 0
        while parent is not None:
            hops += 1
            parent = parent.redirected_from
        if hops > MAX_REDIRECTS:
            self._block(request.url, f"more than {MAX_REDIRECTS} redirects")

    def add_proxy_blocks(self, events: list[tuple[str, str]]) -> None:
        for target, reason in events:
            observed = self._observed_urls.get(target, target)
            source = self._redirect_sources.get(target)
            if source is None:
                source = self._redirect_sources.get(origin_of(observed) or "")
            if source is not None:
                reason = f"{reason} (redirected from {source})"
            self.blocked.append((observed, reason))
