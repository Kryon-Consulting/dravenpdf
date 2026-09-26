"""Per-chain compatibility bound in a dedicated Chromium process.

The mandatory proxy independently enforces destination policy, including if CDP
fails and Chromium releases an intercepted request. CDP failure fails the render.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from playwright.async_api import BrowserContext, CDPSession

from dravenpdf.errors import RenderError
from dravenpdf.render.guards import MAX_REDIRECTS


class RedirectGate:
    def __init__(
        self, context: BrowserContext, session: CDPSession, block: Callable[[str, str], None]
    ) -> None:
        self.context = context
        self.session = session
        self.block = block
        self.fatal = False
        self.closing = False
        self.depths: dict[str, int] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        session.on("Fetch.requestPaused", self._paused)
        session.on("close", self._lost)

    @classmethod
    async def start(
        cls, context: BrowserContext, block: Callable[[str, str], None]
    ) -> RedirectGate:
        assert context.browser is not None
        session = await context.browser.new_browser_cdp_session()
        gate = cls(context, session, block)
        try:
            await session.send(
                "Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}
            )
        except BaseException:
            await gate.close()
            raise
        return gate

    def _track(self, task: asyncio.Task[None]) -> None:
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _lost(self, *_args: object) -> None:
        if not self.closing and not self.fatal:
            self.fatal = True
            self._track(asyncio.create_task(self._close_context()))

    async def _close_context(self) -> None:
        with suppress(Exception):
            await self.context.close()

    def _paused(self, event: dict[str, Any]) -> None:
        self._track(asyncio.create_task(self._handle(event)))

    async def _handle(self, event: dict[str, Any]) -> None:
        try:
            request_id = event["requestId"]
            parent = event.get("redirectedRequestId")
            # Fetch identifiers follow Chromium's own chain, without URL parsing.
            depth = 0 if parent is None else self.depths[parent] + 1
            self.depths[request_id] = depth
            if depth > MAX_REDIRECTS:
                self.block(event["request"]["url"], f"more than {MAX_REDIRECTS} redirects")
                await self.session.send(
                    "Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}
                )
            else:
                await self.session.send("Fetch.continueRequest", {"requestId": request_id})
        except Exception:
            self._lost()

    async def check(self) -> None:
        if not self.fatal:
            try:
                await self.session.send("Browser.getVersion")
            except Exception:
                self._lost()
        if self.fatal:
            raise RenderError("Chromium redirect control failed")

    async def close(self) -> None:
        self.closing = True
        with suppress(Exception):
            await self.session.detach()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
