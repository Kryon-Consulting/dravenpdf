"""BrowserPool deadlines and launches, with a fake Chromium (no browser needed)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from playwright.async_api import Error as PlaywrightError

from dravenpdf import AsyncRenderer, BrowserPool, RenderError, RenderOptions, RenderTimeoutError
from dravenpdf.render import renderer as renderer_module
from dravenpdf.render._proxy_gate import ProxyGate


class FakeContext:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, context_delay: float) -> None:
        self.connected = True
        self.context_delay = context_delay
        self.contexts: list[FakeContext] = []

    def is_connected(self) -> bool:
        return self.connected

    async def new_context(self, **_: Any) -> FakeContext:
        await asyncio.sleep(self.context_delay)
        context = FakeContext()
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        self.connected = False


class FakePlaywright:
    async def stop(self) -> None:
        pass


class FakePool(BrowserPool):
    """Launches FakeBrowsers after ``launch_delay`` seconds; can fail launches."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.launch_delay = 0.0
        self.context_delay = 0.0
        self.fail_next_launch = False
        self.browsers: list[FakeBrowser] = []
        self._playwright = FakePlaywright()  # type: ignore[assignment]

    async def _launch_browser(self, playwright: Any) -> Any:
        await asyncio.sleep(self.launch_delay)
        if self.fail_next_launch:
            self.fail_next_launch = False
            raise PlaywrightError("no chromium here")
        browser = FakeBrowser(self.context_delay)
        self.browsers.append(browser)
        return browser


def deadline_in(seconds: float) -> float:
    return asyncio.get_running_loop().time() + seconds


async def test_deadline_covers_a_slow_launch() -> None:
    pool = FakePool(max_concurrency=1)
    pool.launch_delay = 0.5
    loop = asyncio.get_running_loop()
    started = loop.time()

    with pytest.raises(TimeoutError):
        async with pool.context(deadline=deadline_in(0.1)):
            pass

    assert loop.time() - started < 0.3
    assert (pool.active, pool.waiting) == (0, 0)
    # The launch was not cancelled: the next caller gets that browser, not a new one.
    async with pool.context() as ctx:
        assert isinstance(ctx, FakeContext)
    assert pool.launches == 1
    assert len(pool.browsers) == 1


async def test_concurrent_callers_share_one_launch() -> None:
    pool = FakePool(max_concurrency=3)
    pool.launch_delay = 0.1

    async def use() -> None:
        async with pool.context():
            await asyncio.sleep(0.01)

    await asyncio.gather(use(), use(), use())

    assert pool.launches == 1


async def test_deadline_covers_context_creation_without_leaking() -> None:
    pool = FakePool()
    pool.context_delay = 0.3
    async with pool.context():  # launch first, so only context creation is slow
        pass

    with pytest.raises(TimeoutError):
        async with pool.context(deadline=deadline_in(0.1)):
            pass
    await asyncio.sleep(0.4)

    browser = pool.browsers[0]
    assert len(browser.contexts) == 2
    assert all(c.closed for c in browser.contexts)  # the late one was closed on arrival
    assert pool._current is None
    assert not browser.connected


async def test_cancelled_context_creation_returns_promptly_then_closes_orphan() -> None:
    pool = FakePool()
    pool.context_delay = 0.2
    async with pool.context():
        pass

    async def open_context() -> None:
        async with pool.context():
            pass

    task = asyncio.create_task(open_context())
    await asyncio.sleep(0.05)
    started = asyncio.get_running_loop().time()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert asyncio.get_running_loop().time() - started < 0.1

    await asyncio.sleep(0.25)
    browser = pool.browsers[0]
    assert len(browser.contexts) == 2
    assert browser.contexts[-1].closed


async def test_stalled_context_does_not_delay_render_timeout_or_keep_proxy_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    pool.context_delay = 3600
    pool._proxy_ca = object()  # type: ignore[assignment]

    class FakeGate:
        def __init__(self) -> None:
            self.proxy_options = {"server": "http://127.0.0.1:1", "bypass": "<-loopback>"}
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    gate = FakeGate()

    async def start(*_: Any) -> FakeGate:
        return gate

    monkeypatch.setattr(ProxyGate, "start", start)
    monkeypatch.setattr(renderer_module, "_ORPHAN_PROXY_GRACE", 0.05)
    renderer = AsyncRenderer(pool=pool)
    started = asyncio.get_running_loop().time()
    with pytest.raises(RenderTimeoutError):
        await renderer.from_html("<p>hello</p>", RenderOptions(timeout_ms=50))
    assert asyncio.get_running_loop().time() - started < 0.2
    assert not gate.closed
    assert pool._current is None  # a stalled Chromium is not reused
    await asyncio.sleep(0.1)
    assert gate.closed


async def test_failed_launch_is_retried_by_the_next_caller() -> None:
    pool = FakePool()
    pool.fail_next_launch = True

    with pytest.raises(RenderError, match="could not launch Chromium"):
        async with pool.context():
            pass
    async with pool.context():
        pass

    assert pool.launches == 1
    assert pool.active == 0


async def test_close_waits_for_an_inflight_launch() -> None:
    pool = FakePool()
    pool.launch_delay = 0.2
    waiter = asyncio.create_task(pool._ensure_browser())
    await asyncio.sleep(0.05)
    waiter.cancel()

    await pool.close()

    assert len(pool.browsers) == 1
    assert not pool.browsers[0].connected  # launched late, but still closed
