"""Network policy shared by the renderer and the per-render proxy process.

The proxy subprocess (mitmdump) imports this for every render, so it must stay light:
the standard library and ``dravenpdf.errors`` only. Never import pydantic, playwright,
pikepdf or other dravenpdf modules here (tests/unit/test_proxy_imports.py checks).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from dravenpdf.errors import BlockedRequestError

if TYPE_CHECKING:
    from dravenpdf.render.assets import AssetBundle

Resolver = Callable[[str], Awaitable[list[str]]]

_ALWAYS_ALLOWED_SCHEMES = frozenset({"data", "blob", "about"})
_WEBSOCKET_SCHEMES = {"ws": "http", "wss": "https"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonical_origin(value: str) -> str:
    """``scheme://host[:port]`` with a lowercase host and the default port dropped.

    Raises ValueError for anything that isn't a bare http(s) origin.
    """
    parts = urlsplit(value.strip())
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise ValueError("origin must start with http:// or https://")
    if parts.username or parts.password:
        raise ValueError("origin must not contain a user name or password")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("origin must be scheme://host[:port], without a path or query")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("origin has no host")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("origin has an invalid port") from None
    if ":" in host:
        host = f"[{host}]"
    if port is None or port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def origin_of(url: str) -> str | None:
    """The canonical origin of an http(s) URL, or None for other URLs."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    try:
        return canonical_origin(f"{scheme}://{parts.netloc.rsplit('@', 1)[-1]}")
    except ValueError:
        return None


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
