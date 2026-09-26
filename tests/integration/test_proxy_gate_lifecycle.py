"""The per-render proxy is a required transport, not an optional observer."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import Page

from conftest import Server, requests_tagged
from dravenpdf import AsyncRenderer, RenderAuth, RenderError
from dravenpdf.render._proxy_ca import ProxyCA
from dravenpdf.render._proxy_gate import ProxyGate
from dravenpdf.render.guards import RequestGuard


async def test_proxy_is_ready_and_uses_private_credentials() -> None:
    ca = ProxyCA.create()
    try:
        gate = await ProxyGate.start(
            RequestGuard(allow_private_network=True).proxy_policy("test-secret", None),
            ca,
            asyncio.get_running_loop().time() + 15,
        )
        try:
            assert gate.proxy_options["server"].startswith("http://127.0.0.1:")
            assert gate.proxy_options["bypass"] == "<-loopback>"
            assert gate.proxy_options["username"] == "dravenpdf"
            assert gate.proxy_options["password"] == "test-secret"
            assert not gate.fatal
        finally:
            await gate.close()
    finally:
        ca.close()


async def test_start_requires_control_channel_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def fail_probe(self: ProxyGate) -> None:
        nonlocal calls
        calls += 1
        raise RenderError("control probe failed")

    monkeypatch.setattr(ProxyGate, "synchronize", fail_probe)
    ca = ProxyCA.create()
    try:
        with pytest.raises(RenderError, match="could not start network proxy"):
            await ProxyGate.start(
                RequestGuard(allow_private_network=True).proxy_policy("test-secret", None),
                ca,
                asyncio.get_running_loop().time() + 15,
            )
        assert calls > 0
    finally:
        ca.close()


async def test_unauthenticated_client_sends_no_destination_bytes(server: Server) -> None:
    ca = ProxyCA.create()
    try:
        gate = await ProxyGate.start(
            RequestGuard(allow_private_network=True).proxy_policy("only-this-render", None),
            ca,
            asyncio.get_running_loop().time() + 15,
        )
        try:
            port = int(gate.proxy_options["server"].rsplit(":", 1)[1])
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            url = server.url("/img.svg?tag=unauthorized")
            writer.write(f"GET {url} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            await writer.drain()
            assert b" 407 " in await reader.readline()
            writer.close()
            await writer.wait_closed()
            assert requests_tagged("unauthorized") == []
        finally:
            await gate.close()
    finally:
        ca.close()


async def test_policy_block_sends_no_destination_bytes(server: Server) -> None:
    ca = ProxyCA.create()
    try:
        gate = await ProxyGate.start(
            RequestGuard(allowed_hosts=[]).proxy_policy("only-this-render", None),
            ca,
            asyncio.get_running_loop().time() + 15,
        )
        try:
            port = int(gate.proxy_options["server"].rsplit(":", 1)[1])
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            url = server.url("/img.svg?tag=policy-block")
            token = base64.b64encode(b"dravenpdf:only-this-render").decode()
            writer.write(
                (
                    f"GET {url} HTTP/1.1\r\nHost: localhost:{server.port}\r\n"
                    f"Proxy-Authorization: Basic {token}\r\n\r\n"
                ).encode()
            )
            await writer.drain()
            assert b" 403 " in await reader.readline()
            await gate.synchronize()
            assert gate.blocked
            assert requests_tagged("policy-block") == []
            writer.close()
            await writer.wait_closed()
        finally:
            await gate.close()
    finally:
        ca.close()


@pytest.mark.browser
async def test_proxy_startup_failure_creates_no_context(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(*args: object, **kwargs: object) -> None:
        raise RenderError("proxy unavailable")

    monkeypatch.setattr(ProxyGate, "start", fail)
    async with AsyncRenderer() as renderer:
        with pytest.raises(RenderError, match="proxy unavailable"):
            await renderer.from_html("<p>hello</p>")
        assert renderer.pool.active == 0
        assert renderer.pool.renders == 0


@pytest.mark.browser
async def test_cancellation_closes_context_then_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    gates: list[ProxyGate] = []
    original = ProxyGate.start

    async def capture(*args: Any, **kwargs: Any) -> ProxyGate:
        gate = await original(*args, **kwargs)
        gates.append(gate)
        return gate

    monkeypatch.setattr(ProxyGate, "start", capture)
    entered = asyncio.Event()

    async def hang(_page: Page) -> None:
        entered.set()
        await asyncio.Event().wait()

    async with AsyncRenderer() as renderer:
        task = asyncio.create_task(renderer.from_html("<p>hello</p>", prepare=hang))
        await asyncio.wait_for(entered.wait(), 15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert renderer.pool.active == 0
        assert len(gates) == 1
        assert gates[0]._process.returncode is not None


@pytest.mark.browser
async def test_lost_control_reader_denies_next_request(
    monkeypatch: pytest.MonkeyPatch, server: Server
) -> None:
    gates: list[ProxyGate] = []
    original = ProxyGate.start

    async def capture(*args: Any, **kwargs: Any) -> ProxyGate:
        gate = await original(*args, **kwargs)
        gates.append(gate)
        return gate

    monkeypatch.setattr(ProxyGate, "start", capture)

    async def lose_reader(page: Page) -> None:
        gates[0]._transport.close()
        await page.evaluate("url => fetch(url).catch(() => null)", server.url("/img.svg?tag=lost"))

    async with AsyncRenderer(allow_private_network=True) as renderer:
        with pytest.raises(RenderError):
            await renderer.from_html("<p>hello</p>", prepare=lose_reader)
    assert requests_tagged("lost") == []


@pytest.mark.browser
async def test_auth_warmup_does_not_change_source_origin(tmp_path: Path) -> None:
    urls: list[str] = []

    async def remember(page: Page) -> None:
        urls.append(page.url)

    page = tmp_path / "page.html"
    page.write_text("<p>hello</p>")
    async with AsyncRenderer() as renderer:
        await renderer.from_html("<p>hello</p>", prepare=remember)
        await renderer.from_file(page, prepare=remember)
    assert urls == ["about:blank", page.as_uri()]


@pytest.mark.browser
async def test_concurrent_renders_keep_headers_and_blocks_separate(server: Server) -> None:
    a = server.url("/img.svg?tag=render-a")
    b = server.url("/img.svg?tag=render-b")
    blocked = server.url("/img.svg?tag=render-block", host="127.0.0.1")
    origin = server.url("")
    async with AsyncRenderer(allowed_hosts=["localhost"], on_blocked="skip") as renderer:
        first, second = await asyncio.gather(
            renderer.from_html(
                f'<img src="{a}"><img src="{blocked}">',
                auth=RenderAuth(headers={origin: {"X-Tenant": "render-a"}}),
            ),
            renderer.from_html(
                f'<img src="{b}">',
                auth=RenderAuth(headers={origin: {"X-Tenant": "render-b"}}),
            ),
        )
    assert first.render_report is not None
    assert second.render_report is not None
    assert len(first.render_report.blocked) == 1
    assert second.render_report.blocked == []
    assert requests_tagged("render-block") == []
    assert [h["x-tenant"] for _, _, h in requests_tagged("render-a")] == ["render-a"]
    assert [h["x-tenant"] for _, _, h in requests_tagged("render-b")] == ["render-b"]
