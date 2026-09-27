"""The per-render proxy is a required transport, not an optional observer."""

from __future__ import annotations

import asyncio
import base64
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import Page

from conftest import Server, requests_tagged
from dravenpdf import AsyncRenderer, BlockedRequestError, RenderAuth, RenderError
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
async def test_proxy_exit_fails_render_without_direct_fallback(
    monkeypatch: pytest.MonkeyPatch, server: Server
) -> None:
    gates: list[ProxyGate] = []
    original = ProxyGate.start

    async def capture(*args: Any, **kwargs: Any) -> ProxyGate:
        gate = await original(*args, **kwargs)
        gates.append(gate)
        return gate

    monkeypatch.setattr(ProxyGate, "start", capture)
    url = server.url("/secret?tag=proxy-exit")

    async def stop_proxy(page: Page) -> None:
        gates[0]._process.terminate()
        await gates[0]._process.wait()
        await page.evaluate("url => fetch(url).catch(() => null)", url)

    async with AsyncRenderer(allowed_hosts=["localhost"]) as renderer:
        with pytest.raises(RenderError):
            await renderer.from_html("<p>page</p>", prepare=stop_proxy)
        assert renderer.pool.active == 0
    assert requests_tagged("proxy-exit") == []


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


@pytest.mark.browser
@pytest.mark.parametrize("fragment", [False, True])
@pytest.mark.parametrize("absolute_uppercase", [False, True])
@pytest.mark.parametrize("redirect_path", ["plain", "encoded_dot", "backslash"])
@pytest.mark.parametrize("popup", [False, True])
async def test_eleventh_redirect_destination_gets_zero_bytes_even_when_skipping(
    fragment: bool,
    absolute_uppercase: bool,
    redirect_path: str,
    popup: bool,
) -> None:
    seen: list[int] = []

    class Redirects(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            hop = int(parse_qs(urlsplit(self.path).query)["n"][0])
            seen.append(hop)
            if hop < 11:
                self.send_response(302)
                path = {
                    "plain": "/chain",
                    "encoded_dot": "/a/%2e%2e/chain",
                    "backslash": "/a\\..\\chain",
                }[redirect_path]
                location = f"{path}?n={hop + 1}" + ("#fragment" if fragment else "")
                if absolute_uppercase:
                    location = f"http://LOCALHOST:{httpd.server_port}{location}"
                self.send_header("Location", location)
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<p>too far</p>")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Redirects)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        async with AsyncRenderer(allow_private_network=True, on_blocked="skip") as renderer:
            with suppress(RenderError):
                url = f"http://127.0.0.1:{httpd.server_port}/chain?n=0"
                if popup:

                    async def open_popup(page: Page) -> None:
                        async with page.expect_popup() as pending:
                            await page.evaluate("url => window.open(url)", url)
                        child = await pending.value
                        with suppress(Exception):
                            await child.wait_for_load_state()

                    await renderer.from_html("<p>parent</p>", prepare=open_popup)
                else:
                    await renderer.from_url(url)
        assert seen == list(range(11))
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()


@pytest.mark.browser
async def test_blocked_https_connect_reports_chromium_url() -> None:
    url = "https://127.0.0.1:9443/private.png?case=connect"
    async with AsyncRenderer(allowed_hosts=["localhost"]) as renderer:
        with pytest.raises(BlockedRequestError) as blocked:
            await renderer.from_html(f'<img src="{url}">')
    assert blocked.value.url == url

    async with AsyncRenderer(allowed_hosts=["localhost"], on_blocked="skip") as renderer:
        doc = await renderer.from_html(f'<img src="{url}">')
    assert doc.render_report is not None
    assert [target for target, _ in doc.render_report.blocked] == [url]


@pytest.mark.browser
@pytest.mark.parametrize("on_blocked", ["fail", "skip"])
async def test_final_block_check_runs_after_context_closes(
    monkeypatch: pytest.MonkeyPatch,
    on_blocked: str,
) -> None:
    renderer = AsyncRenderer(on_blocked=on_blocked)  # type: ignore[arg-type]
    original = ProxyGate.synchronize
    calls = 0

    async def late_block(gate: ProxyGate) -> None:
        nonlocal calls
        await original(gate)
        calls += 1
        if calls == 3:  # startup probe, pre-PDF check, final close check
            assert renderer.pool.active == 1  # admission remains held through proxy teardown
            assert gate._process.returncode is None
            gate.blocked.append(("http://127.0.0.1:9", "policy"))

    monkeypatch.setattr(ProxyGate, "synchronize", late_block)
    async with renderer:
        if on_blocked == "fail":
            with pytest.raises(BlockedRequestError):
                await renderer.from_html("<p>hello</p>")
        else:
            doc = await renderer.from_html("<p>hello</p>")
            assert doc.render_report is not None
            assert doc.render_report.blocked == [("http://127.0.0.1:9", "policy")]
    assert calls == 3
    assert renderer.pool.active == 0


@pytest.mark.browser
async def test_eleven_independent_redirecting_images_succeed():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            parts = urlsplit(self.path)
            seen.append(self.path)
            if parts.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/end?" + parts.query)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
            self.end_headers()
            if parts.path == "/end":
                self.wfile.write(b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with AsyncRenderer(allow_private_network=True, on_blocked="skip") as renderer:
            doc = await renderer.from_html(
                "".join(
                    f'<img src="http://127.0.0.1:{server.server_port}/start?n={n}">'
                    for n in range(11)
                )
            )
        assert len([p for p in seen if p.startswith("/end?")]) == 11, seen
        assert doc.render_report.blocked == []
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.browser
async def test_lost_redirect_session_fails_render(monkeypatch: pytest.MonkeyPatch) -> None:
    from dravenpdf.render._redirect_gate import RedirectGate

    gates = []
    start = RedirectGate.start.__func__

    async def capture(cls, *args, **kwargs):
        gate = await start(cls, *args, **kwargs)
        gates.append(gate)
        return gate

    monkeypatch.setattr(RedirectGate, "start", classmethod(capture))

    async def detach(page: Page) -> None:
        await gates[0].session.detach()

    async with AsyncRenderer(on_blocked="skip") as renderer:
        with pytest.raises(RenderError, match="redirect control failed"):
            await renderer.from_html("<p>test</p>", prepare=detach)
    assert gates[0].fatal


@pytest.mark.browser
async def test_redirect_session_closes_before_browser_slot_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dravenpdf.render._redirect_gate import RedirectGate
    from dravenpdf.render.pool import BrowserPool

    events: list[str] = []
    release = BrowserPool._release

    def record_release(self: BrowserPool, *args: Any, **kwargs: Any) -> None:
        events.append("release")
        release(self, *args, **kwargs)

    async def record_close(self: RedirectGate) -> None:
        events.append("redirect close")

    monkeypatch.setattr(BrowserPool, "_release", record_release)
    monkeypatch.setattr(RedirectGate, "close", record_close)

    async with AsyncRenderer() as renderer:
        await renderer.from_html("<p>ready</p>")

    assert events.index("redirect close") < events.index("release")


@pytest.mark.browser
async def test_redirect_overflow_is_reported_once() -> None:
    class Redirects(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

        def do_GET(self) -> None:
            hop = int(parse_qs(urlsplit(self.path).query)["n"][0])
            self.send_response(302)
            self.send_header("Location", f"/chain?n={hop + 1}")
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Redirects)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        async with AsyncRenderer(allow_private_network=True) as renderer:
            with pytest.raises(BlockedRequestError) as blocked:
                await renderer.from_url(f"http://127.0.0.1:{httpd.server_port}/chain?n=0")
        assert "more than 10 redirects" in str(blocked.value)
        assert "more)" not in str(blocked.value)
    finally:
        httpd.shutdown()
        thread.join()
        httpd.server_close()
