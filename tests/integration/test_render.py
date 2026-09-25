"""End-to-end rendering with a real Chromium."""

from __future__ import annotations

import asyncio
import concurrent.futures
from pathlib import Path
from typing import Any

import pytest

from conftest import SVG, Server, page_size, pdf_text
from dravenpdf import (
    AsyncRenderer,
    BlockedRequestError,
    BrowserPool,
    HeaderFooter,
    Margins,
    PoolExhaustedError,
    Renderer,
    RenderError,
    RenderOptions,
    RenderTimeoutError,
)

pytestmark = pytest.mark.browser

A4 = (595.28, 841.89)
LETTER_LANDSCAPE = (792.0, 612.0)

TWO_PAGES = """<!doctype html><html><head><title>Two pages</title></head><body>
<p>First page</p><div style="break-before: page">Second page</div></body></html>"""


def assert_size(actual: tuple[float, float], expected: tuple[float, float]) -> None:
    assert actual == pytest.approx(expected, abs=1.0)


# ---------------------------------------------------------------- layout


async def test_html_to_a4(renderer: AsyncRenderer) -> None:
    doc = await renderer.from_html("<h1>Hello dravenpdf</h1>")

    assert doc.page_count == 1
    assert "Hello dravenpdf" in pdf_text(doc)[0]
    assert_size(page_size(doc), A4)


async def test_paper_and_orientation(renderer: AsyncRenderer) -> None:
    doc = await renderer.from_html("<p>x</p>", RenderOptions(paper="Letter", landscape=True))

    assert_size(page_size(doc), LETTER_LANDSCAPE)


async def test_custom_size(renderer: AsyncRenderer) -> None:
    doc = await renderer.from_html("<p>x</p>", RenderOptions(width="100mm", height="50mm"))

    assert_size(page_size(doc), (283.46, 141.73))


async def test_footer_page_numbers(renderer: AsyncRenderer) -> None:
    footer = HeaderFooter(
        html='<div style="font-size:10px;width:100%;text-align:center">'
        'Page <span class="pageNumber"></span> of <span class="totalPages"></span></div>'
    )
    doc = await renderer.from_html(TWO_PAGES, RenderOptions(footer=footer))

    text = pdf_text(doc)
    assert doc.page_count == 2
    assert "Page 1 of 2" in text[0]
    assert "Page 2 of 2" in text[1]


async def test_page_ranges(renderer: AsyncRenderer) -> None:
    doc = await renderer.from_html(TWO_PAGES, RenderOptions(page_ranges="2"))

    assert doc.page_count == 1
    assert "Second page" in pdf_text(doc)[0]


async def test_print_media_is_emulated(renderer: AsyncRenderer) -> None:
    html = """<style>.screen{display:block}.print{display:none}
    @media print{.screen{display:none}.print{display:block}}</style>
    <p class="screen">SCREEN</p><p class="print">PRINT</p>"""

    printed = pdf_text(await renderer.from_html(html))[0]
    screen = pdf_text(await renderer.from_html(html, RenderOptions(media="screen")))[0]

    assert "PRINT" in printed
    assert "SCREEN" not in printed
    assert "SCREEN" in screen
    assert "PRINT" not in screen


async def test_margins_move_content(renderer: AsyncRenderer) -> None:
    doc = await renderer.from_html(
        "<p>x</p>", RenderOptions(margins=Margins(top="0", right="0", bottom="0", left="0"))
    )
    assert doc.page_count == 1


# ---------------------------------------------------------------- waiting


async def test_wait_for_selector(renderer: AsyncRenderer) -> None:
    html = """<body><script>setTimeout(() => {
      const p = document.createElement('p'); p.id = 'late'; p.textContent = 'LATE CONTENT';
      document.body.append(p); }, 800);</script></body>"""

    doc = await renderer.from_html(html, RenderOptions(wait_for_selector="#late"))

    assert "LATE CONTENT" in pdf_text(doc)[0]


async def test_wait_for_ready_flag(renderer: AsyncRenderer) -> None:
    html = """<body><script>setTimeout(() => {
      document.body.append('CHART DRAWN'); window.__DRAVENPDF_READY__ = true; }, 800);
    </script></body>"""

    doc = await renderer.from_html(html, RenderOptions(wait_for_ready_flag=True))

    assert "CHART DRAWN" in pdf_text(doc)[0]


async def test_timeout(renderer: AsyncRenderer) -> None:
    with pytest.raises(RenderTimeoutError) as info:
        await renderer.from_html(
            "<p>never ready</p>", RenderOptions(wait_for_selector="#nope", timeout_ms=1500)
        )
    assert info.value.timeout_ms == 1500


async def test_web_fonts_and_images_finish_loading(
    local_renderer: AsyncRenderer, server: Server
) -> None:
    doc = await local_renderer.from_url(server.url("/page.html"))

    text = pdf_text(doc)[0]
    assert "Served page" in text
    assert "IMG-OK" in text


# ---------------------------------------------------------------- sources


async def test_from_url_404_is_an_error(local_renderer: AsyncRenderer, server: Server) -> None:
    with pytest.raises(RenderError, match="HTTP 404"):
        await local_renderer.from_url(server.url("/missing"))


async def test_base_url_resolves_relative_assets(
    local_renderer: AsyncRenderer, server: Server
) -> None:
    html = """<p>with base</p><img src="img.svg" onload="document.body.append('IMG-OK')"
    onerror="document.body.append('IMG-FAIL')">"""

    doc = await local_renderer.from_html(html, base_url=server.url("/"))

    assert "IMG-OK" in pdf_text(doc)[0]


async def test_from_file_with_relative_asset(renderer: AsyncRenderer, tmp_path: Path) -> None:
    (tmp_path / "logo.svg").write_bytes(SVG)
    page = tmp_path / "report.html"
    page.write_text(
        """<h1>From file</h1><img src="logo.svg" onload="document.body.append('IMG-OK')"
        onerror="document.body.append('IMG-FAIL')">"""
    )

    doc = await renderer.from_file(page)

    text = pdf_text(doc)[0]
    assert "From file" in text
    assert "IMG-OK" in text


async def test_from_file_cannot_read_outside_its_folder(
    renderer: AsyncRenderer, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.svg"
    secret.write_bytes(SVG)
    (tmp_path / "site").mkdir()
    page = tmp_path / "site" / "index.html"
    page.write_text('<img src="../secret.svg">')

    with pytest.raises(BlockedRequestError, match="outside"):
        await renderer.from_file(page)


async def test_from_file_missing(renderer: AsyncRenderer, tmp_path: Path) -> None:
    with pytest.raises(RenderError, match="no such file"):
        await renderer.from_file(tmp_path / "nope.html")


# ---------------------------------------------------------------- SSRF guard


async def test_private_url_blocked_by_default(renderer: AsyncRenderer, server: Server) -> None:
    with pytest.raises(BlockedRequestError, match="non-public"):
        await renderer.from_url(server.url("/page.html", host="127.0.0.1"))


async def test_private_subresource_blocked_by_default(
    renderer: AsyncRenderer, server: Server
) -> None:
    html = f'<img src="{server.url("/secret", host="127.0.0.1")}">'

    with pytest.raises(BlockedRequestError, match=r"127\.0\.0\.1"):
        await renderer.from_html(html)


async def test_skip_policy_renders_without_blocked_resource(server: Server) -> None:
    html = f"""<p>still rendered</p><img src="{server.url("/img.svg", host="127.0.0.1")}"
    onerror="document.body.append('IMG-BLOCKED')">"""

    async with AsyncRenderer(on_blocked="skip") as r:
        doc = await r.from_html(html)

    text = pdf_text(doc)[0]
    assert "still rendered" in text
    assert "IMG-BLOCKED" in text


async def test_redirect_to_disallowed_host_is_blocked(
    local_renderer: AsyncRenderer, server: Server
) -> None:
    secret = server.url("/secret", host="127.0.0.1")
    html = f'<iframe src="{server.url("/redirect?to=" + secret)}"></iframe>'

    with pytest.raises(BlockedRequestError, match="redirected from"):
        await local_renderer.from_html(html)


async def test_navigation_redirect_to_disallowed_host_is_blocked(
    local_renderer: AsyncRenderer, server: Server
) -> None:
    secret = server.url("/secret", host="127.0.0.1")

    with pytest.raises(BlockedRequestError, match="redirected from"):
        await local_renderer.from_url(server.url("/redirect?to=" + secret))


async def test_allowed_redirect_is_followed(local_renderer: AsyncRenderer, server: Server) -> None:
    html = f"""<img src="{server.url("/redirect?to=/img.svg")}"
    onload="document.body.append('IMG-OK')" onerror="document.body.append('IMG-FAIL')">"""

    doc = await local_renderer.from_html(html)

    assert "IMG-OK" in pdf_text(doc)[0]


async def test_allow_private_network(server: Server) -> None:
    async with AsyncRenderer(allow_private_network=True) as r:
        doc = await r.from_url(server.url("/page.html", host="127.0.0.1"))

    assert "IMG-OK" in pdf_text(doc)[0]


# ---------------------------------------------------------------- pool behaviour


async def test_concurrent_renders() -> None:
    async with AsyncRenderer(max_concurrency=2) as r:
        docs = await asyncio.gather(*(r.from_html(f"<p>doc {i}</p>") for i in range(6)))

        assert [f"doc {i}" in pdf_text(d)[0] for i, d in enumerate(docs)] == [True] * 6
        assert r.pool.renders == 6
        assert r.pool.active == 0


async def test_queue_full_raises_pool_exhausted() -> None:
    slow = RenderOptions(wait_for_ready_flag=True, timeout_ms=5000)
    html = "<script>setTimeout(() => window.__DRAVENPDF_READY__ = true, 1500)</script>"

    async with AsyncRenderer(max_concurrency=1, max_queue=0) as r:
        first = asyncio.create_task(r.from_html(html, slow))
        while r.pool.active == 0:
            await asyncio.sleep(0.01)

        with pytest.raises(PoolExhaustedError):
            await r.from_html("<p>too many</p>")
        assert (await first).page_count == 1


async def test_timeout_counts_time_waiting_for_a_slot() -> None:
    slow = RenderOptions(wait_for_ready_flag=True, timeout_ms=5000)
    html = "<script>setTimeout(() => window.__DRAVENPDF_READY__ = true, 1500)</script>"

    async with AsyncRenderer(max_concurrency=1) as r:
        first = asyncio.create_task(r.from_html(html, slow))
        while r.pool.active == 0:
            await asyncio.sleep(0.01)

        started = asyncio.get_running_loop().time()
        with pytest.raises(RenderTimeoutError):
            await r.from_html("<p>queued</p>", RenderOptions(timeout_ms=300))
        assert asyncio.get_running_loop().time() - started < 1.0
        assert r.pool.waiting == 0
        assert (await first).page_count == 1
        assert (await r.from_html("<p>after</p>")).page_count == 1


async def test_browser_is_recycled() -> None:
    async with AsyncRenderer(recycle_after=2) as r:
        for i in range(5):
            await r.from_html(f"<p>{i}</p>")

        assert r.pool.launches == 3
        assert r.pool.restarts == 0


async def test_recovers_after_browser_crash() -> None:
    async with AsyncRenderer() as r:
        await r.from_html("<p>before</p>")
        slot = r.pool._current
        assert slot is not None
        await slot.browser.close()  # simulate Chromium dying

        doc = await r.from_html("<p>after crash</p>")

        assert "after crash" in pdf_text(doc)[0]
        assert r.pool.restarts == 1


class _SlowLaunchPool(BrowserPool):
    delay = 0.0

    async def _launch_browser(self, playwright: Any) -> Any:
        await asyncio.sleep(self.delay)
        return await super()._launch_browser(playwright)


async def test_deadline_covers_a_slow_browser_launch() -> None:
    pool = _SlowLaunchPool()
    async with pool, AsyncRenderer(pool=pool) as r:
        await r.from_html("<p>warm up</p>")
        slot = pool._current
        assert slot is not None
        await slot.browser.close()  # the next render has to relaunch Chromium
        pool.delay = 3.0
        loop = asyncio.get_running_loop()
        started = loop.time()

        with pytest.raises(RenderTimeoutError):
            await r.from_html("<p>x</p>", RenderOptions(timeout_ms=500))

        assert loop.time() - started < 1.5
        doc = await r.from_html("<p>after</p>")  # reuses the launch that kept going
        assert "after" in pdf_text(doc)[0]
        assert pool.launches == 2


# ---------------------------------------------------------------- sync wrapper


def test_sync_renderer_from_threads() -> None:
    with Renderer(max_concurrency=2) as r, concurrent.futures.ThreadPoolExecutor(4) as pool:
        docs = list(pool.map(lambda i: r.from_html(f"<p>thread {i}</p>"), range(4)))

    assert [f"thread {i}" in pdf_text(d)[0] for i, d in enumerate(docs)] == [True] * 4


def test_sync_renderer_saves(tmp_path: Path) -> None:
    out = tmp_path / "out.pdf"
    with Renderer() as r:
        r.from_html("<p>saved</p>").save(out)

    assert out.read_bytes().startswith(b"%PDF-")
