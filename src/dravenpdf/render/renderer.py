"""AsyncRenderer: HTML, URLs and local files to PDF with headless Chromium."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from html import escape
from os import PathLike
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from dravenpdf.document.pdf import PdfDocument
from dravenpdf.errors import AssetError, BlockedRequestError, RenderError, RenderTimeoutError
from dravenpdf.options import RenderOptions
from dravenpdf.render.assets import AssetBundle
from dravenpdf.render.guards import BlockPolicy, RequestGuard
from dravenpdf.render.pool import BrowserPool
from dravenpdf.render.report import ReportCollector
from dravenpdf.render.templates import render_template
from dravenpdf.render.waits import wait_until_ready

logger = logging.getLogger("dravenpdf.render")

Loader = Callable[[Page], Awaitable[None]]

_HEAD_OPEN = re.compile(r"<head(\s[^>]*)?>", re.IGNORECASE)


def inject_base_url(html: str, base_url: str) -> str:
    """Add ``<base href>`` so relative URLs in ``html`` resolve against ``base_url``."""
    tag = f'<base href="{escape(base_url, quote=True)}">'
    match = _HEAD_OPEN.search(html)
    if match:
        return html[: match.end()] + tag + html[match.end() :]
    return tag + html


class AsyncRenderer:
    """Renders HTML to PDF. Use as an async context manager, or call start()/close().

    Args:
        max_concurrency: renders running at once (each is a Chromium page).
        max_queue: renders allowed to wait for a slot before PoolExhaustedError.
        recycle_after: replace Chromium after this many renders.
        allowed_hosts: only these hosts may be loaded (see RequestGuard).
        allow_private_network: allow private/loopback addresses. Trusted input only.
        on_blocked: ``"fail"`` raises BlockedRequestError when any request was
            blocked; ``"skip"`` renders without the blocked resources.
        executable_path: Chromium binary; defaults to $DRAVENPDF_CHROMIUM_PATH or
            the one ``playwright install chromium`` downloaded.
        pool: share an existing BrowserPool instead of creating one. The renderer
            then does not start or close it.
    """

    def __init__(
        self,
        *,
        max_concurrency: int = 4,
        max_queue: int = 16,
        recycle_after: int = 500,
        allowed_hosts: Iterable[str] | None = None,
        allow_private_network: bool = False,
        on_blocked: BlockPolicy = "fail",
        executable_path: str | None = None,
        pool: BrowserPool | None = None,
    ) -> None:
        self._owns_pool = pool is None
        self.pool = pool or BrowserPool(
            max_concurrency=max_concurrency,
            max_queue=max_queue,
            recycle_after=recycle_after,
            executable_path=executable_path,
        )
        self._allowed_hosts = None if allowed_hosts is None else list(allowed_hosts)
        self._allow_private = allow_private_network
        self._on_blocked = on_blocked

    @property
    def is_running(self) -> bool:
        return self.pool.is_running

    async def start(self) -> None:
        if self._owns_pool:
            await self.pool.start()

    async def close(self) -> None:
        if self._owns_pool:
            await self.pool.close()

    async def __aenter__(self) -> AsyncRenderer:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    def _new_guard(
        self, file_root: Path | None = None, bundle: AssetBundle | None = None
    ) -> RequestGuard:
        return RequestGuard(
            allowed_hosts=self._allowed_hosts,
            allow_private_network=self._allow_private,
            file_root=file_root,
            bundle=bundle,
        )

    # ------------------------------------------------------------------ public API

    async def from_html(
        self,
        html: str,
        options: RenderOptions | None = None,
        *,
        base_url: str | None = None,
        assets: Mapping[str, bytes] | None = None,
    ) -> PdfDocument:
        """Render an HTML string.

        ``base_url`` resolves relative links against a web address. ``assets`` instead
        supplies the files the HTML refers to, keyed by relative path
        (``{"css/site.css": b"...", "img/logo.png": b"..."}``); they are served from
        memory, and anything missing is a 404 in the render report.
        """
        opts = options or RenderOptions()
        if assets is not None:
            if base_url is not None:
                raise AssetError("pass either base_url or assets, not both")
            bundle = AssetBundle(html, assets)

            async def load_bundle(page: Page) -> None:
                await page.goto(bundle.document_url, wait_until=opts.wait_until)

            return await self._render(load_bundle, opts, self._new_guard(bundle=bundle))
        if base_url is not None:
            await self._new_guard().check(base_url)
            html = inject_base_url(html, base_url)

        async def load(page: Page) -> None:
            await page.set_content(html, wait_until=opts.wait_until)

        return await self._render(load, opts)

    async def from_url(self, url: str, options: RenderOptions | None = None) -> PdfDocument:
        """Render a web page. The URL and everything it loads go through the guard."""
        opts = options or RenderOptions()
        await self._new_guard().check(url)

        async def load(page: Page) -> None:
            response = await page.goto(url, wait_until=opts.wait_until)
            if response is not None and response.status >= 400:
                raise RenderError(f"{url} returned HTTP {response.status}")

        return await self._render(load, opts)

    async def from_file(
        self, path: str | PathLike[str], options: RenderOptions | None = None
    ) -> PdfDocument:
        """Render a local HTML file; relative assets resolve from its folder.

        The page may load local files from that folder (and below) only. HTTP(S)
        requests it makes go through the guard like any other render.
        """
        opts = options or RenderOptions()
        file = await asyncio.to_thread(Path(path).resolve)
        if not await asyncio.to_thread(file.is_file):
            raise RenderError(f"no such file: {file}")
        url = file.as_uri()

        async def load(page: Page) -> None:
            await page.goto(url, wait_until=opts.wait_until)

        return await self._render(load, opts, self._new_guard(file_root=file.parent))

    async def from_template(
        self,
        template: str,
        data: Mapping[str, Any],
        options: RenderOptions | None = None,
        *,
        template_dir: str | PathLike[str] | None = None,
        base_url: str | None = None,
        assets: Mapping[str, bytes] | None = None,
    ) -> PdfDocument:
        """Render a Jinja2 template (sandboxed, autoescaped) to PDF.

        With ``template_dir``, ``template`` is a file name in that folder; relative
        assets (CSS, images) resolve from the folder, and the page may load files from
        it only. Without it, ``template`` is the template source. ``base_url`` makes
        relative assets resolve against a web URL instead, and ``assets`` supplies them
        from memory (see :meth:`from_html`).
        """
        if assets is not None and template_dir is not None:
            raise AssetError("pass either template_dir or assets, not both")
        opts = options or RenderOptions()
        html = await asyncio.to_thread(render_template, template, data, template_dir=template_dir)
        if template_dir is None or base_url is not None or assets is not None:
            return await self.from_html(html, opts, base_url=base_url, assets=assets)
        folder = await asyncio.to_thread(Path(template_dir).resolve)
        folder_url = folder.as_uri() + "/"

        async def load(page: Page) -> None:
            # Give the document a file:// address in the template folder, so relative
            # asset URLs resolve there, then swap in the rendered HTML.
            await page.goto(folder_url, wait_until="domcontentloaded")
            await page.set_content(html, wait_until=opts.wait_until)

        return await self._render(load, opts, self._new_guard(file_root=folder))

    # ------------------------------------------------------------------ internals

    async def _render(
        self, load: Loader, opts: RenderOptions, guard: RequestGuard | None = None
    ) -> PdfDocument:
        guard = guard or self._new_guard()
        collector = ReportCollector()
        # One deadline for the whole render: waiting for a browser slot, launching
        # Chromium, creating the context and page, loading, waiting and printing.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + opts.timeout_ms / 1000
        try:
            async with (
                asyncio.timeout_at(deadline),
                self.pool.context(
                    deadline=deadline, service_workers="block", accept_downloads=False
                ) as ctx,
            ):
                try:
                    await guard.install(ctx)
                    page = await ctx.new_page()
                    collector.attach(page)
                    page.set_default_timeout(max(1, (deadline - loop.time()) * 1000))
                    await page.emulate_media(media=opts.media)
                    await load(page)
                    await wait_until_ready(page, opts)
                    if self._on_blocked == "fail":
                        guard.raise_if_blocked()
                    collector.check(
                        fail_on_resource_errors=opts.fail_on_resource_errors,
                        fail_on_page_errors=opts.fail_on_page_errors,
                    )
                    data = await page.pdf(**opts.to_pdf_kwargs())
                except PlaywrightError as exc:
                    if isinstance(exc, PlaywrightTimeoutError):
                        raise
                    if guard.blocked and self._on_blocked == "fail":
                        try:
                            guard.raise_if_blocked()
                        except BlockedRequestError as blocked:
                            raise blocked from exc
                    raise RenderError(f"render failed: {exc.message}") from exc
        except (TimeoutError, PlaywrightTimeoutError) as exc:
            raise RenderTimeoutError(
                f"render did not finish within {opts.timeout_ms} ms",
                timeout_ms=opts.timeout_ms,
            ) from exc
        report = collector.report
        report.blocked = list(guard.blocked)
        if not report.ok:
            logger.info("rendered with problems: %s", report.summary())
        doc = await asyncio.to_thread(PdfDocument.from_bytes, data)
        doc.render_report = report
        return doc
