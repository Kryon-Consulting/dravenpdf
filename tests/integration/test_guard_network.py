"""The request guard against real network traffic: WebSockets and failing fetches."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import websockets

from conftest import pdf_text
from dravenpdf import AsyncRenderer, BlockedRequestError, RenderOptions

pytestmark = pytest.mark.browser

READY = RenderOptions(wait_for_ready_flag=True, timeout_ms=10_000)


@pytest.fixture
async def ws_port() -> AsyncIterator[int]:
    """A WebSocket server on 127.0.0.1 that answers any message with LOCAL_SOCKET_OK."""

    async def handler(ws: websockets.ServerConnection) -> None:
        async for _ in ws:
            await ws.send("LOCAL_SOCKET_OK")

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        yield next(iter(server.sockets)).getsockname()[1]


def socket_page(url: str) -> str:
    return f"""<p id="out">waiting</p><script>
    const out = document.getElementById('out');
    const done = text => {{ out.textContent = text; window.__DRAVENPDF_READY__ = true; }};
    const ws = new WebSocket('{url}');
    ws.onopen = () => ws.send('hi');
    ws.onmessage = e => done(e.data);
    ws.onclose = () => {{ if (out.textContent === 'waiting') done('WS_CLOSED'); }};
    </script>"""


async def test_private_websocket_is_blocked(renderer: AsyncRenderer, ws_port: int) -> None:
    with pytest.raises(BlockedRequestError, match=r"ws://127\.0\.0\.1"):
        await renderer.from_html(socket_page(f"ws://127.0.0.1:{ws_port}/"), READY)


async def test_blocked_websocket_never_connects(ws_port: int) -> None:
    async with AsyncRenderer(on_blocked="skip") as r:
        doc = await r.from_html(socket_page(f"ws://127.0.0.1:{ws_port}/"), READY)

    text = pdf_text(doc)[0]
    assert "LOCAL_SOCKET_OK" not in text
    assert "WS_CLOSED" in text


async def test_allowlisted_websocket_works(ws_port: int) -> None:
    async with AsyncRenderer(allowed_hosts=["localhost"]) as r:
        doc = await r.from_html(socket_page(f"ws://localhost:{ws_port}/"), READY)

    assert "LOCAL_SOCKET_OK" in pdf_text(doc)[0]


async def test_failed_fetch_does_not_hang_the_render() -> None:
    # Nothing listens on port 1. The guard's fetch fails; the browser must be told
    # at once, not left waiting until the render deadline.
    html = """<p>body</p><img src="http://localhost:1/missing.png"
    onerror="document.body.append('IMG-FAILED')">"""
    loop = asyncio.get_running_loop()

    async with AsyncRenderer(allowed_hosts=["localhost"]) as r:
        started = loop.time()
        doc = await r.from_html(html, RenderOptions(timeout_ms=10_000))

    assert loop.time() - started < 5
    assert "IMG-FAILED" in pdf_text(doc)[0]
