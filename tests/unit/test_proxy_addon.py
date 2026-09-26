"""The proxy must answer every rejected flow before an upstream connection."""

from __future__ import annotations

import base64
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from mitmproxy import connection, ctx, http, tcp, udp

from dravenpdf.render._proxy_addon import ProxyAddon, _redirect_key
from dravenpdf.render._proxy_protocol import ProxyPolicy, send_event


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("HTTP://LOCALHOST:80/a/../chain?n=%7e#fragment", "http://localhost:80/chain?n=%7E"),
        ("https://EXAMPLE.COM:443/a/./chain", "https://example.com:443/a/chain"),
        ("http://[::1]/a", "http://[::1]:80/a"),
        ("http://täst.de/x", "http://xn--tst-qla.de:80/x"),
        ("http://LOCALHOST/a/%2e%2e/chain", "http://localhost:80/chain"),
    ],
)
def test_redirect_keys_normalize_browser_url_variants(target: str, expected: str) -> None:
    assert _redirect_key(target) == expected


async def resolve(host: str) -> list[str]:
    return {"public.example": ["93.184.216.34"], "other.example": ["10.0.0.4"]}[host]


def flow(
    method: str, url: str, *, host: str | None = None, credential: str = "token"
) -> http.HTTPFlow:
    client = connection.Client(peername=("127.0.0.1", 45678), sockname=("127.0.0.1", 8888))
    server = connection.Server(address=None)
    result = http.HTTPFlow(client, server)
    result.request = http.Request.make(method, url)
    if method == "CONNECT":
        result.request.authority = "public.example:443"
    elif host is not None:
        result.request.headers["Host"] = host
    if credential:
        encoded = base64.b64encode(f"dravenpdf:{credential}".encode()).decode()
        result.request.headers["Proxy-Authorization"] = "Basic " + encoded
    return result


def addon(
    *,
    allowed_hosts: list[str] | None = None,
    headers: dict[str, dict[str, str]] | None = None,
    bundle: dict[str, object] | None = None,
    resolver: Callable[[str], Awaitable[list[str]]] | None = None,
) -> tuple[ProxyAddon, list[dict[str, str]]]:
    events: list[dict[str, str]] = []
    policy = ProxyPolicy(
        allowed_hosts=allowed_hosts,
        allow_private_network=False,
        bundle=bundle,
        headers=headers or {},
        credential="token",
        deadline=None,
    )
    return ProxyAddon(policy, events.append, resolver=resolver or resolve), events


async def test_connect_to_private_host_is_blocked() -> None:
    proxy, events = addon(allowed_hosts=["public.example"])
    tunnel = flow("CONNECT", "https://public.example:443")
    tunnel.request.authority = "other.example:443"
    await proxy.http_connect(tunnel)
    assert tunnel.response is not None
    assert tunnel.response.status_code == 403
    assert tunnel.server_conn.address is None
    assert events == [
        {"kind": "alive", "target": "", "reason": ""},
        {"kind": "blocked", "target": "other.example:443", "reason": "policy"},
    ]


async def test_connect_event_never_echoes_malformed_authority() -> None:
    proxy, events = addon()
    tunnel = flow("CONNECT", "https://public.example:443")
    tunnel.request.authority = "secret header value"
    await proxy.http_connect(tunnel)
    assert tunnel.response is not None
    assert tunnel.response.status_code == 403
    assert events[-1]["target"] == "unknown"


async def test_allowed_connect_does_not_authorize_different_inner_authority() -> None:
    proxy, events = addon(allowed_hosts=["public.example"])
    tunnel = flow("CONNECT", "https://public.example:443")
    await proxy.http_connect(tunnel)
    assert tunnel.response is None
    inner = flow("GET", "https://other.example/", host="other.example")
    inner.client_conn = tunnel.client_conn
    await proxy.requestheaders(inner)
    assert inner.response is not None
    assert inner.response.status_code == 403
    assert inner.server_conn.address is None
    assert events[-1]["kind"] == "blocked"


async def test_headers_match_exact_origin_and_browser_cookie_is_untouched() -> None:
    proxy, _ = addon(
        allowed_hosts=["public.example"],
        headers={"https://public.example": {"x-secret": "secret"}},
    )
    same = flow("GET", "https://public.example/path", host="public.example")
    same.request.headers["Cookie"] = "browser=chosen"
    await proxy.requestheaders(same)
    assert same.response is None
    assert same.request.headers["x-secret"] == "secret"
    assert same.request.headers["Cookie"] == "browser=chosen"
    other_port = flow("GET", "https://public.example:8443/path", host="public.example:8443")
    await proxy.requestheaders(other_port)
    assert other_port.response is None
    assert "x-secret" not in other_port.request.headers
    assert "Cookie" not in other_port.request.headers


@pytest.mark.parametrize("http2", [False, True])
async def test_omitted_authority_port_cannot_rewrite_nondefault_destination(http2: bool) -> None:
    proxy, events = addon(
        allowed_hosts=["public.example"],
        headers={"https://public.example": {"x-secret": "secret"}},
    )
    request = flow("GET", "https://public.example:8443/path", host="public.example")
    if http2:
        request.request.http_version = "HTTP/2.0"
        request.request.authority = "public.example"
    await proxy.requestheaders(request)
    assert request.response is not None
    assert request.response.status_code == 403
    assert "x-secret" not in request.request.headers
    assert events[-1]["kind"] == "blocked"


async def test_proxy_credential_is_removed_on_reused_connection() -> None:
    proxy, _ = addon(allowed_hosts=["public.example"])
    first = flow("GET", "https://public.example/", host="public.example")
    await proxy.requestheaders(first)
    assert first.response is None
    assert "Proxy-Authorization" not in first.request.headers
    second = flow("GET", "https://public.example/next", host="public.example")
    second.client_conn = first.client_conn
    await proxy.requestheaders(second)
    assert second.response is None
    assert "Proxy-Authorization" not in second.request.headers


async def test_bundle_hit_and_miss_never_open_upstream() -> None:
    proxy, _ = addon(bundle={"html": "PHA+ZG9jPC9wPg==", "files": {}})
    hit = flow("GET", "https://bundle.dravenpdf.invalid/", host="bundle.dravenpdf.invalid")
    await proxy.requestheaders(hit)
    assert hit.response is not None
    assert hit.response.status_code == 200
    assert hit.response.content == b"<p>doc</p>"
    assert hit.response.headers["Cache-Control"] == "no-store"
    assert hit.server_conn.address is None
    miss = flow("GET", "https://bundle.dravenpdf.invalid/missing", host="bundle.dravenpdf.invalid")
    await proxy.requestheaders(miss)
    assert miss.response is not None
    assert miss.response.status_code == 404
    assert miss.response.content == b"not in the asset bundle"
    assert miss.server_conn.address is None


async def test_missing_proxy_auth_is_blocked_and_reported() -> None:
    proxy, events = addon(allowed_hosts=["public.example"])
    request = flow("GET", "https://public.example/", host="public.example", credential="")
    await proxy.requestheaders(request)
    assert request.response is not None
    assert request.response.status_code == 407
    assert request.server_conn.address is None
    assert events == [
        {"kind": "alive", "target": "", "reason": ""},
        {"kind": "blocked", "target": "https://public.example", "reason": "credential"},
    ]


async def test_resolver_exception_fails_closed_and_reports_fatal() -> None:
    async def broken(_host: str) -> list[str]:
        raise RuntimeError("secret header value")

    proxy, events = addon(resolver=broken)
    request = flow("GET", "https://public.example/", host="public.example")
    await proxy.requestheaders(request)
    assert request.response is not None
    assert request.response.status_code == 502
    assert request.server_conn.address is None
    assert events == [
        {"kind": "alive", "target": "", "reason": ""},
        {"kind": "fatal", "target": "https://public.example", "reason": "proxy failure"},
    ]


@pytest.mark.parametrize(
    "error", [ValueError("bad DNS"), PermissionError("no DNS"), TimeoutError("DNS timeout")]
)
async def test_resolver_internal_errors_are_fatal(error: Exception) -> None:
    async def broken(_host: str) -> list[str]:
        raise error

    proxy, events = addon(resolver=broken)
    request = flow("GET", "https://public.example/", host="public.example")
    await proxy.requestheaders(request)
    assert request.response is not None
    assert request.response.status_code == 502
    assert events[-1]["kind"] == "fatal"
    assert events[-1]["reason"] == "proxy failure"


async def test_expired_deadline_is_fatal() -> None:
    proxy, events = addon(allowed_hosts=["public.example"])
    proxy.policy = ProxyPolicy(["public.example"], False, None, {}, "token", 0.0)
    request = flow("GET", "https://public.example/", host="public.example")
    await proxy.requestheaders(request)
    assert request.response is not None
    assert request.response.status_code == 502
    assert events[-1]["kind"] == "fatal"
    assert events[-1]["reason"] == "deadline"


async def test_closed_event_pipe_blocks_allowed_request_before_upstream() -> None:
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    proxy, _ = addon(allowed_hosts=["public.example"])
    proxy.emit = lambda event: send_event(
        write_fd,
        event["kind"],
        event["target"],
        event["reason"],  # type: ignore[arg-type]
    )
    try:
        request = flow("GET", "https://public.example/", host="public.example")
        await proxy.requestheaders(request)
        assert request.response is not None
        assert request.response.status_code == 502
        assert request.server_conn.address is None
    finally:
        os.close(write_fd)


async def test_closed_event_pipe_blocks_reused_https_tunnel() -> None:
    read_fd, write_fd = os.pipe()
    proxy, _ = addon(allowed_hosts=["public.example"])
    proxy.emit = lambda event: send_event(
        write_fd,
        event["kind"],
        event["target"],
        event["reason"],  # type: ignore[arg-type]
    )
    try:
        tunnel = flow("CONNECT", "https://public.example:443")
        await proxy.http_connect(tunnel)
        assert tunnel.response is None
        os.close(read_fd)
        request = flow("GET", "https://public.example/", host="public.example")
        request.client_conn = tunnel.client_conn
        await proxy.requestheaders(request)
        assert request.response is not None
        assert request.response.status_code == 502
        assert request.server_conn.address is None
    finally:
        os.close(write_fd)


async def test_host_mismatch_and_broken_control_channel_block() -> None:
    proxy, events = addon(allowed_hosts=["public.example"])
    mismatch = flow("GET", "https://public.example/", host="other.example")
    await proxy.requestheaders(mismatch)
    assert mismatch.response is not None
    assert mismatch.response.status_code == 403
    assert events[-1]["kind"] == "blocked"

    def broken_sink(_event: dict[str, str]) -> None:
        raise BrokenPipeError

    proxy.emit = broken_sink
    first = flow("GET", "https://other.example/", host="other.example")
    await proxy.requestheaders(first)
    assert first.response is not None
    assert first.response.status_code == 502
    second = flow("GET", "https://public.example/", host="public.example")
    await proxy.requestheaders(second)
    assert second.response is not None
    assert second.response.status_code == 502


async def test_malformed_authority_still_gets_a_blocking_response() -> None:
    proxy, events = addon(allowed_hosts=["public.example"])
    request = flow("GET", "https://public.example/", host="public.example:bad")
    await proxy.requestheaders(request)
    assert request.response is not None
    assert request.response.status_code == 403
    assert events[-1]["kind"] == "blocked"


async def test_zero_port_host_header_is_rejected() -> None:
    proxy, events = addon(allowed_hosts=["public.example"])
    request = flow("GET", "https://public.example/", host="public.example:0")
    await proxy.requestheaders(request)
    assert request.response is not None
    assert request.response.status_code == 403
    assert events[-1]["kind"] == "blocked"


def test_policy_file_requires_private_permissions(tmp_path: Path) -> None:
    policy = ProxyPolicy(None, False, None, {}, "credential", None)
    path = tmp_path / "policy.json"
    policy.write(path)
    assert ProxyPolicy.read(path) == policy
    path.chmod(0o644)
    with pytest.raises(PermissionError):
        ProxyPolicy.read(path)


def test_policy_repr_hides_credentials_and_headers() -> None:
    policy = ProxyPolicy(
        None,
        False,
        None,
        {"https://example.com": {"authorization": "Bearer secret"}},
        "proxy-secret",
        None,
    )
    rendered = repr(policy)
    assert "Bearer secret" not in rendered
    assert "proxy-secret" not in rendered


@pytest.mark.parametrize("flow_type", [tcp.TCPFlow, udp.UDPFlow])
def test_raw_flows_are_killed(flow_type: type[tcp.TCPFlow] | type[udp.UDPFlow]) -> None:
    proxy, events = addon()
    raw = flow_type(
        connection.Client(peername=("127.0.0.1", 1), sockname=("127.0.0.1", 2)),
        connection.Server(address=None),
        live=True,
    )
    if isinstance(raw, tcp.TCPFlow):
        proxy.tcp_start(raw)
    else:
        proxy.udp_start(raw)
    assert raw.error is not None
    assert raw.live is False
    assert events[-1]["kind"] == "fatal"


def test_websocket_start_kills_unauthorized_flow() -> None:
    proxy, events = addon()
    ws = flow("GET", "https://public.example/ws", host="public.example")
    ws.live = True
    proxy.websocket_start(ws)
    assert ws.error is not None
    assert ws.live is False
    assert events[-1]["kind"] == "fatal"


async def test_ready_requires_lazy_connections_and_strict_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy, events = addon()
    options = SimpleNamespace(
        connection_strategy="lazy",
        upstream_cert=False,
        ssl_insecure=False,
        ignore_hosts=[],
    )
    monkeypatch.setattr(ctx, "options", options, raising=False)
    proxy.running()
    assert events == [{"kind": "ready", "target": "", "reason": ""}]
    options.connection_strategy = "eager"
    with pytest.raises(RuntimeError, match="unsafe proxy options"):
        proxy.running()
    assert events[-1] == {"kind": "fatal", "target": "", "reason": "proxy failure"}
    request = flow("GET", "https://public.example/", host="public.example")
    await proxy.requestheaders(request)
    assert request.response is not None
    assert request.response.status_code == 502
