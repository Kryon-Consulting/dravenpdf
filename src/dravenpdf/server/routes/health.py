"""Health, readiness and metrics (no API key needed)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from dravenpdf.server.deps import metrics_of

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """The process is up."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """The browser pool is running and not saturated."""
    renderer = getattr(request.app.state, "renderer", None)
    if renderer is None or not renderer.is_running:
        return JSONResponse({"status": "starting"}, status_code=503)
    pool = renderer.pool
    if pool.active >= pool.max_concurrency and pool.waiting >= pool.max_queue:
        return JSONResponse({"status": "busy"}, status_code=503)
    return JSONResponse({"status": "ready", "active": pool.active, "waiting": pool.waiting})


@router.get("/metrics")
async def metrics(request: Request) -> Response:
    """Prometheus metrics."""
    return Response(generate_latest(metrics_of(request).registry), media_type=CONTENT_TYPE_LATEST)
