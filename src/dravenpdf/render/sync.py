"""Renderer: a blocking wrapper around AsyncRenderer for scripts and sync web apps.

It runs an event loop in a background thread and forwards each call to it, so it
works in code that already has no loop (scripts, Django views, Celery tasks).
All rendering logic lives in AsyncRenderer; keep this file a thin wrapper.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine, Iterable, Mapping
from os import PathLike
from typing import Any, TypeVar

from dravenpdf.document.pdf import PdfDocument
from dravenpdf.options import RenderOptions
from dravenpdf.render.auth import RenderAuth
from dravenpdf.render.renderer import AsyncRenderer

T = TypeVar("T")


class Renderer:
    """Same constructor arguments and methods as AsyncRenderer, without ``await``.

    Thread-safe: several threads may call it at once; renders run concurrently up
    to ``max_concurrency``.
    """

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._renderer: AsyncRenderer | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._loop is not None:
                return
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, name="dravenpdf", daemon=True)
            thread.start()
            self._loop, self._thread = loop, thread
        try:
            self._renderer = self._run(self._create())
        except BaseException:
            self._stop_loop()
            raise

    async def _create(self) -> AsyncRenderer:
        renderer = AsyncRenderer(**self._kwargs)
        await renderer.start()
        return renderer

    def close(self) -> None:
        if self._loop is None:
            return
        try:
            if self._renderer is not None:
                self._run(self._renderer.close())
        finally:
            self._renderer = None
            self._stop_loop()

    def _stop_loop(self) -> None:
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join()
            loop.close()

    def __enter__(self) -> Renderer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        if self._loop is None:
            coro.close()
            raise RuntimeError("Renderer is not started; use `with Renderer() as r:`")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def _async(self) -> AsyncRenderer:
        if self._renderer is None:
            raise RuntimeError("Renderer is not started; use `with Renderer() as r:`")
        return self._renderer

    def from_html(
        self,
        html: str,
        options: RenderOptions | None = None,
        *,
        base_url: str | None = None,
        assets: Mapping[str, bytes] | None = None,
    ) -> PdfDocument:
        return self._run(self._async().from_html(html, options, base_url=base_url, assets=assets))

    def from_url(
        self, url: str, options: RenderOptions | None = None, *, auth: RenderAuth | None = None
    ) -> PdfDocument:
        return self._run(self._async().from_url(url, options, auth=auth))

    def from_file(
        self, path: str | PathLike[str], options: RenderOptions | None = None
    ) -> PdfDocument:
        return self._run(self._async().from_file(path, options))

    def from_template(
        self,
        template: str,
        data: Mapping[str, Any],
        options: RenderOptions | None = None,
        *,
        template_dir: str | PathLike[str] | None = None,
        base_url: str | None = None,
        assets: Mapping[str, bytes] | None = None,
    ) -> PdfDocument:
        return self._run(
            self._async().from_template(
                template,
                data,
                options,
                template_dir=template_dir,
                base_url=base_url,
                assets=assets,
            )
        )

    def stamp_html(
        self,
        document: PdfDocument,
        html: str,
        *,
        opacity: float = 1.0,
        pages: Iterable[int] | None = None,
        under: bool = False,
        base_url: str | None = None,
    ) -> PdfDocument:
        """Sync form of ``await document.stamp_html(async_renderer, html, ...)``."""
        return self._run(
            document.stamp_html(
                self._async(), html, opacity=opacity, pages=pages, under=under, base_url=base_url
            )
        )
