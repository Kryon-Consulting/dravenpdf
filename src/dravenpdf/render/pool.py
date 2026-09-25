"""One shared Chromium process with a cap on concurrent renders."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Browser, BrowserContext, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from dravenpdf.errors import PoolExhaustedError, RenderError

logger = logging.getLogger("dravenpdf.render.pool")

CHROMIUM_PATH_ENV = "DRAVENPDF_CHROMIUM_PATH"

DEFAULT_LAUNCH_ARGS: tuple[str, ...] = (
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-sync",
    "--disable-dev-shm-usage",
    "--no-first-run",
)


@dataclass
class _Slot:
    browser: Browser
    active: int = 0
    renders: int = 0
    retired: bool = False


class BrowserPool:
    """Owns Playwright and a Chromium process, and hands out fresh browser contexts.

    - At most ``max_concurrency`` contexts are open at once. Up to ``max_queue``
      more callers may wait for one; beyond that :class:`PoolExhaustedError` is raised.
    - If Chromium disconnects (crash, OOM kill), the next render launches a new one.
    - After ``recycle_after`` renders the browser is replaced, to cap memory growth.
      Renders already running on the old browser finish first.

    ``executable_path`` defaults to the ``DRAVENPDF_CHROMIUM_PATH`` environment
    variable, and otherwise to the Chromium that ``playwright install`` downloaded.
    """

    def __init__(
        self,
        *,
        max_concurrency: int = 4,
        max_queue: int = 16,
        recycle_after: int = 500,
        executable_path: str | None = None,
        launch_args: Sequence[str] = DEFAULT_LAUNCH_ARGS,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if max_queue < 0:
            raise ValueError("max_queue must not be negative")
        if recycle_after < 1:
            raise ValueError("recycle_after must be at least 1")
        self.max_concurrency = max_concurrency
        self.max_queue = max_queue
        self.recycle_after = recycle_after
        self._executable_path = executable_path or os.environ.get(CHROMIUM_PATH_ENV) or None
        self._launch_args = list(launch_args)
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._launching: asyncio.Task[_Slot] | None = None
        self._playwright: Playwright | None = None
        self._current: _Slot | None = None
        self._retired: list[_Slot] = []
        self._closing: set[asyncio.Task[None]] = set()
        self._waiting = 0
        self._active = 0
        self.launches = 0
        self.restarts = 0
        self.renders = 0

    # ------------------------------------------------------------------ lifecycle

    @property
    def is_running(self) -> bool:
        return self._playwright is not None

    @property
    def active(self) -> int:
        """Renders holding a context right now."""
        return self._active

    @property
    def waiting(self) -> int:
        """Renders queued for a free slot."""
        return self._waiting

    async def start(self) -> None:
        if self._playwright is not None:
            return
        self._playwright = await async_playwright().start()
        try:
            await self._ensure_browser()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self._launching is not None:
            # Let an in-flight launch finish so its browser is closed below, not leaked.
            with suppress(Exception):
                await asyncio.shield(self._launching)
        slots = [s for s in (self._current, *self._retired) if s is not None]
        self._current = None
        self._retired.clear()
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)
        for slot in slots:
            with suppress(PlaywrightError):
                await slot.browser.close()
        if self._playwright is not None:
            with suppress(PlaywrightError):
                await self._playwright.stop()
            self._playwright = None

    async def __aenter__(self) -> BrowserPool:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ browser

    async def _ensure_browser(self) -> _Slot:
        """The current browser, launching one if needed.

        Launches run as one shared background task: callers only wait for it, so a
        caller that gives up (its render deadline passed) doesn't cancel the launch.
        The browser still starts and serves the next render instead of leaking a
        half-launched Chromium, and concurrent callers never launch twice.
        """
        if self._playwright is None:
            raise RenderError("the browser pool is not running; call start() first")
        slot = self._current
        if slot is not None and slot.browser.is_connected() and not slot.retired:
            return slot
        if self._launching is None:
            if slot is not None:
                if slot.browser.is_connected():
                    logger.info("recycling Chromium after %d renders", slot.renders)
                else:
                    logger.warning("Chromium disconnected; launching a new one")
                    self.restarts += 1
                self._current = None
                self._retire(slot)
            task = asyncio.get_running_loop().create_task(self._launch())
            task.add_done_callback(self._launch_done)
            self._launching = task
        return await asyncio.shield(self._launching)

    async def _launch(self) -> _Slot:
        assert self._playwright is not None
        try:
            browser = await self._launch_browser(self._playwright)
        except PlaywrightError as exc:
            raise RenderError(f"could not launch Chromium: {exc.message}") from exc
        self.launches += 1
        self._current = _Slot(browser)
        return self._current

    async def _launch_browser(self, playwright: Playwright) -> Browser:
        return await playwright.chromium.launch(
            executable_path=self._executable_path,
            args=self._launch_args,
        )

    def _launch_done(self, task: asyncio.Task[_Slot]) -> None:
        self._launching = None
        if not task.cancelled():
            task.exception()  # mark it retrieved even if every waiter gave up

    def _retire(self, slot: _Slot) -> None:
        slot.retired = True
        if slot.active == 0:
            self._close_later(slot.browser)
        else:
            self._retired.append(slot)

    def _close_later(self, browser: Browser) -> None:
        task = asyncio.get_running_loop().create_task(self._close_quietly(browser))
        self._closing.add(task)  # keep a reference until it finishes
        task.add_done_callback(self._closing.discard)

    @staticmethod
    async def _close_quietly(browser: Browser) -> None:
        with suppress(PlaywrightError):
            await browser.close()

    async def _new_context(self, slot: _Slot, options: dict[str, Any]) -> BrowserContext:
        """``browser.new_context()`` that can't leak a context when cancelled.

        If the caller is cancelled (deadline) while Chromium is still creating the
        context, the context is closed as soon as it arrives.
        """
        task = asyncio.get_running_loop().create_task(slot.browser.new_context(**options))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(self._close_orphan_context)
            raise

    def _close_orphan_context(self, task: asyncio.Task[BrowserContext]) -> None:
        if task.cancelled() or task.exception() is not None:
            return
        closing = asyncio.get_running_loop().create_task(self._close_context(task.result()))
        self._closing.add(closing)
        closing.add_done_callback(self._closing.discard)

    @staticmethod
    async def _close_context(ctx: BrowserContext) -> None:
        with suppress(PlaywrightError):
            await ctx.close()

    # ------------------------------------------------------------------ contexts

    @asynccontextmanager
    async def context(
        self, *, deadline: float | None = None, **options: Any
    ) -> AsyncIterator[BrowserContext]:
        """A new, isolated browser context; closed when the block exits.

        ``deadline`` (event-loop time, as from ``loop.time()``) bounds everything
        before the block runs: waiting for a free slot, launching Chromium if needed,
        and creating the context. Past it, :class:`TimeoutError` is raised. Other
        keyword arguments go to Playwright's ``browser.new_context()``.
        """
        if self._semaphore.locked() and self._waiting >= self.max_queue:
            raise PoolExhaustedError(
                f"{self._active} renders running and {self._waiting} waiting; try again later"
            )
        self._waiting += 1
        try:
            async with asyncio.timeout_at(deadline):
                await self._semaphore.acquire()
        finally:
            self._waiting -= 1
        self._active += 1
        try:
            async with asyncio.timeout_at(deadline):
                slot, ctx = await self._open(options)
            try:
                yield ctx
            finally:
                with suppress(PlaywrightError):
                    await ctx.close()
                self._release(slot, rendered=True)
        finally:
            self._active -= 1
            self._semaphore.release()

    async def _open(self, options: dict[str, Any]) -> tuple[_Slot, BrowserContext]:
        """Reserve the current browser and open a context on it.

        If the browser turns out to have just died, retries once on a new one. The
        reservation is released on any failure, including cancellation.
        """
        for attempt in (1, 2):
            slot = await self._ensure_browser()
            slot.active += 1  # reserve it before any await, so it can't be closed under us
            try:
                return slot, await self._new_context(slot, options)
            except PlaywrightError:
                self._release(slot, rendered=False)
                if attempt == 2 or slot.browser.is_connected():
                    raise
            except BaseException:
                self._release(slot, rendered=False)
                raise
        raise AssertionError("unreachable")

    def _release(self, slot: _Slot, *, rendered: bool) -> None:
        slot.active -= 1
        if rendered:
            slot.renders += 1
            self.renders += 1
            if slot.renders >= self.recycle_after:
                slot.retired = True
        if slot in self._retired and slot.active == 0:
            self._retired.remove(slot)
            self._close_later(slot.browser)
