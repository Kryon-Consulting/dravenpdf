"""Fail-closed mitmdump addon for one render's HTTP and WebSocket traffic."""

from __future__ import annotations

import base64
import hmac
import os
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from mitmproxy import ctx, http, tcp, udp

from dravenpdf.errors import BlockedRequestError
from dravenpdf.render._proxy_protocol import ProxyPolicy, send_event
from dravenpdf.render.assets import AssetBundle
from dravenpdf.render.auth import origin_of
from dravenpdf.render.guards import NetworkPolicy, Resolver, resolve_host

EventSink = Callable[[dict[str, str]], None]


def _authority(value: str, *, default_port: int | None = None) -> tuple[str, int]:
    parts = urlsplit("//" + value)
    if parts.username or parts.password or parts.path or parts.query or parts.fragment:
        raise ValueError("malformed authority")
    host = (parts.hostname or "").lower().rstrip(".")
    port = parts.port if parts.port is not None else default_port
    if (
        not host
        or any(ord(char) <= 32 for char in host)
        or port is None
        or port < 1
        or port > 65535
    ):
        raise ValueError("malformed authority")
    return host, port


class ProxyAddon:
    def __init__(
        self,
        policy: ProxyPolicy,
        emit: EventSink,
        *,
        resolver: Resolver,
    ) -> None:
        self.policy = policy
        self.emit = emit
        self.bundle = (
            AssetBundle.from_snapshot(policy.bundle) if policy.bundle is not None else None
        )
        self.network = NetworkPolicy(
            allowed_hosts=policy.allowed_hosts,
            allow_private_network=policy.allow_private_network,
            bundle=self.bundle,
            resolver=resolver,
        )
        self.tunnels: dict[str, tuple[str, int]] = {}
        self.authorized: set[str] = set()
        self.control_alive = True

    def running(self) -> None:
        """Announce readiness only with the pre-connection security options in force."""
        try:
            options = ctx.options
            if (
                options.connection_strategy != "lazy"
                or options.upstream_cert is not False
                or options.ssl_insecure is not False
                or options.ignore_hosts != []
            ):
                raise RuntimeError("unsafe proxy options")
            self._event("ready", "", "")
        except BaseException:
            self.control_alive = False
            with suppress(BaseException):
                self._event("fatal", "", "proxy failure")
            raise

    def _event(self, kind: str, target: str, reason: str) -> None:
        try:
            self.emit({"kind": kind, "target": target, "reason": reason})
        except BaseException:
            self.control_alive = False
            raise

    @staticmethod
    def _target(flow: http.HTTPFlow, *, connect: bool = False) -> str:
        if connect:
            try:
                host, port = _authority(flow.request.authority)
            except ValueError:
                return "unknown"
            return f"{host}:{port}"
        return origin_of(flow.request.pretty_url) or "unknown"

    def _deny(self, flow: http.HTTPFlow, kind: str, target: str, reason: str) -> None:
        # Install the response first: an event-channel failure must not let traffic through.
        status = 407 if reason == "credential" else 502 if kind == "fatal" else 403
        flow.response = http.Response.make(status, b"blocked by dravenpdf")
        with suppress(BaseException):
            self._event(kind, target, reason)

    def _check_credential(self, flow: http.HTTPFlow) -> None:
        supplied = flow.request.headers.pop("Proxy-Authorization", "")
        if flow.client_conn.id in self.authorized:
            return
        token = base64.b64encode(f"dravenpdf:{self.policy.credential}".encode()).decode()
        if not hmac.compare_digest(supplied, f"Basic {token}"):
            raise PermissionError("credential")
        self.authorized.add(flow.client_conn.id)

    async def http_connect(self, flow: http.HTTPFlow) -> None:
        target = "unknown"
        try:
            target = self._target(flow, connect=True)
            if not self.control_alive:
                raise RuntimeError("control channel closed")
            if flow.client_conn.transport_protocol != "tcp":
                raise ValueError("unsupported transport")
            if self.policy.deadline is not None and time.monotonic() >= self.policy.deadline:
                raise TimeoutError("deadline")
            self._check_credential(flow)
            host, port = _authority(flow.request.authority)
            if (flow.request.host.lower().rstrip("."), flow.request.port) != (host, port):
                raise ValueError("CONNECT authority mismatch")
            await self.network.check(f"https://{flow.request.authority}/")
            self.tunnels[flow.client_conn.id] = (host, port)
        except (BlockedRequestError, PermissionError, ValueError, TimeoutError) as exc:
            reason = "credential" if isinstance(exc, PermissionError) else "policy"
            self._deny(flow, "blocked", target, reason)
        except BaseException:
            self._deny(flow, "fatal", target, "proxy failure")

    async def requestheaders(self, flow: http.HTTPFlow) -> None:
        target = "unknown"
        try:
            target = self._target(flow)
            if not self.control_alive:
                raise RuntimeError("control channel closed")
            request = flow.request
            if flow.client_conn.transport_protocol != "tcp":
                raise ValueError("unsupported transport")
            if self.policy.deadline is not None and time.monotonic() >= self.policy.deadline:
                raise TimeoutError("deadline")
            self._check_credential(flow)
            if request.scheme not in ("http", "https"):
                raise ValueError("unsupported scheme")
            host, port = _authority(request.host_header or "", default_port=request.port)
            if (request.host.lower().rstrip("."), request.port) != (host, port):
                raise ValueError("Host authority mismatch")
            tunnel = self.tunnels.get(flow.client_conn.id)
            if tunnel is not None and tunnel != (host, port):
                raise ValueError("CONNECT authority mismatch")
            url = request.pretty_url
            await self.network.check(url)
            if self.bundle is not None and self.bundle.owns(url):
                found = self.bundle.lookup(url)
                if found is None:
                    flow.response = http.Response.make(404, b"not in the asset bundle")
                else:
                    body, content_type = found
                    flow.response = http.Response.make(
                        200,
                        body,
                        {"Content-Type": content_type, "Cache-Control": "no-store"},
                    )
                return
            for name, value in self.policy.headers.get(origin_of(url) or "", {}).items():
                if name.lower() != "cookie":
                    request.headers[name] = value
        except (BlockedRequestError, PermissionError, ValueError, TimeoutError) as exc:
            reason = "credential" if isinstance(exc, PermissionError) else "policy"
            self._deny(flow, "blocked", target, reason)
        except BaseException:
            self._deny(flow, "fatal", target, "proxy failure")

    def websocket_start(self, flow: http.HTTPFlow) -> None:
        # The WebSocket handshake has already passed requestheaders.
        try:
            if not self.control_alive or flow.client_conn.id not in self.authorized:
                raise RuntimeError("unauthorized WebSocket")
        except BaseException:
            self._deny(flow, "fatal", "unknown", "proxy failure")

    def tcp_start(self, flow: tcp.TCPFlow) -> None:
        self._reject_raw(flow)

    def udp_start(self, flow: udp.UDPFlow) -> None:
        self._reject_raw(flow)

    def _reject_raw(self, flow: tcp.TCPFlow | udp.UDPFlow) -> None:
        with suppress(BaseException):
            flow.kill()  # type: ignore[no-untyped-call]
        with suppress(BaseException):
            self._event("fatal", "unknown", "proxy failure")


def _load_addon() -> ProxyAddon:
    policy = ProxyPolicy.read(Path(os.environ["DRAVENPDF_PROXY_POLICY"]))
    fd = int(os.environ["DRAVENPDF_PROXY_EVENTS_FD"])

    def emit(event: dict[str, str]) -> None:
        send_event(fd, event["kind"], event["target"], event["reason"])  # type: ignore[arg-type]

    return ProxyAddon(policy, emit, resolver=resolve_host)


if "DRAVENPDF_PROXY_POLICY" in os.environ:
    addons = [_load_addon()]
