"""The FastAPI application factory.

Run with ``dravenpdf serve`` or
``uvicorn dravenpdf.server.app:create_app --factory``. Settings come from
DRAVENPDF_* environment variables (see docs/http-api.md).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from dravenpdf import __version__
from dravenpdf.render.pool import BrowserPool
from dravenpdf.render.renderer import AsyncRenderer
from dravenpdf.server import errors
from dravenpdf.server.config import Settings
from dravenpdf.server.metrics import Metrics
from dravenpdf.server.middleware import BodyLimitMiddleware, RequestIdMiddleware
from dravenpdf.server.routes import convert, documents, health, render

logger = logging.getLogger("dravenpdf.server")


def create_app(settings: Settings | None = None, renderer: AsyncRenderer | None = None) -> FastAPI:
    """Build the app. ``renderer`` lets tests supply their own; by default one is
    created from ``settings`` and started/stopped with the app."""
    settings = settings or Settings()
    if not logging.getLogger().handlers:
        logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("dravenpdf").setLevel(settings.log_level.upper())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active = renderer or AsyncRenderer(
            max_concurrency=settings.max_concurrency,
            max_queue=settings.max_queue,
            recycle_after=settings.browser_recycle_after,
            allowed_hosts=settings.allowed_host_list,
        )
        await active.start()
        app.state.renderer = active
        logger.info("dravenpdf %s ready", __version__)
        try:
            yield
        finally:
            await active.close()

    app = FastAPI(
        title="dravenpdf",
        version=__version__,
        summary="HTML to PDF and PDF tools.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.renderer = None

    def current_pool() -> BrowserPool | None:
        active: AsyncRenderer | None = app.state.renderer
        return active.pool if active is not None else None

    app.state.metrics = Metrics(current_pool)
    errors.install(app)
    for module in (render, documents, convert, health):
        app.include_router(module.router)
    # Last added runs first: request ids wrap everything, including 413 responses.
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(RequestIdMiddleware)
    return app
