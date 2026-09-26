"""The request guard against real network traffic: WebSockets and failing fetches."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from urllib.parse import quote

import pytest
import websockets
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from conftest import Server, pdf_text, requests_tagged
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


@pytest.mark.parametrize("redirect", [False, True], ids=["first-navigation", "redirect"])
async def test_popup_blocked_target_gets_zero_request_bytes(server: Server, redirect: bool) -> None:
    tag = f"popup-target-{redirect}"
    blocked = server.url(f"/secret?tag={tag}", host="127.0.0.1")
    popup_url = (
        server.url(f"/redirect?to={quote(blocked, safe='')}&tag=popup-source")
        if redirect
        else blocked
    )

    async def open_popup(page: Page) -> None:
        async with page.expect_popup(timeout=5000) as opened:
            await page.evaluate("url => window.open(url)", popup_url)
        popup = await opened.value
        with suppress(PlaywrightError):
            await popup.wait_for_load_state(timeout=1000)

    async with AsyncRenderer(allowed_hosts=["localhost"], on_blocked="skip") as renderer:
        doc = await renderer.from_html("<p>parent</p>", prepare=open_popup)

    assert requests_tagged(tag) == []
    assert doc.render_report is not None
    assert [url for url, _ in doc.render_report.blocked] == [blocked]


async def test_nested_frame_and_worker_blocked_targets_get_zero_bytes(server: Server) -> None:
    frame_url = server.url("/secret?tag=nested-frame", host="127.0.0.1")
    worker_url = server.url("/secret?tag=worker-fetch", host="127.0.0.1")

    async def open_targets(page: Page) -> None:
        async with page.expect_response(
            lambda response: response.url == frame_url, timeout=5000
        ) as pending:
            await page.evaluate(
                """url => {
                const outer = document.createElement('iframe');
                outer.srcdoc = `<iframe src="${url}"></iframe>`;
                document.body.append(outer);
            }""",
                frame_url,
            )
        assert (await pending.value).status == 403
        await page.evaluate(
            """url => new Promise(resolve => {
                const code = `fetch(${JSON.stringify(url)})
                    .then(() => postMessage('done'))
                    .catch(() => postMessage('done'))`;
                const worker = new Worker(URL.createObjectURL(
                    new Blob([code], {type: 'text/javascript'})
                ));
                worker.onmessage = () => { worker.terminate(); resolve(); };
            })""",
            worker_url,
        )

    async with AsyncRenderer(allowed_hosts=["localhost"], on_blocked="skip") as renderer:
        doc = await renderer.from_url(server.url("/secret"), prepare=open_targets)

    assert requests_tagged("nested-frame") == []
    assert requests_tagged("worker-fetch") == []
    assert doc.render_report is not None
    assert len(doc.render_report.blocked) == 2, doc.render_report.blocked
    # The proxy emits origin-only events; the parent may attribute two simultaneous
    # blocks on that origin to the last observed URL.
    assert all(url in {frame_url, worker_url} for url, _ in doc.render_report.blocked)
