"""Shared request helpers: auth, the renderer, uploads and responses."""

from __future__ import annotations

import asyncio
import io
import secrets
import zipfile
from collections.abc import Awaitable, Sequence
from typing import Annotated, Any, TypeVar

from fastapi import Header, Request, UploadFile
from fastapi.responses import Response

from dravenpdf.document.pages import parse_page_ranges
from dravenpdf.document.pdf import PdfDocument
from dravenpdf.options import RenderOptions
from dravenpdf.render.renderer import AsyncRenderer
from dravenpdf.server.config import Settings
from dravenpdf.server.errors import ApiError
from dravenpdf.server.metrics import Metrics
from dravenpdf.server.schemas import PostProcess

T = TypeVar("T")

# OpenAPI descriptions of binary responses.
PDF_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {"content": {"application/pdf": {}}, "description": "The PDF."}
}
ZIP_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {"content": {"application/zip": {}}, "description": "A ZIP archive."}
}


def settings_of(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def renderer_of(request: Request) -> AsyncRenderer:
    renderer: AsyncRenderer = request.app.state.renderer
    return renderer


def metrics_of(request: Request) -> Metrics:
    metrics: Metrics = request.app.state.metrics
    return metrics


async def require_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Header(description="The service API key.")] = None,
) -> None:
    settings = settings_of(request)
    if settings.auth_disabled:
        return
    expected = settings.api_key.get_secret_value() if settings.api_key else ""
    if not x_api_key or not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise ApiError("unauthorized", "missing or wrong X-API-Key header")


def clamp_timeout(options: RenderOptions, settings: Settings) -> RenderOptions:
    """Requests can lower the render timeout but not raise it past the server's limit."""
    if options.timeout_ms <= settings.render_timeout_ms:
        return options
    return options.model_copy(update={"timeout_ms": settings.render_timeout_ms})


async def timed(request: Request, source: str, work: Awaitable[T]) -> T:
    with metrics_of(request).render_seconds.labels(source).time():
        return await work


async def read_pdf(upload: UploadFile) -> PdfDocument:
    data = await upload.read()
    if b"%PDF-" not in data[:1024]:
        raise ApiError("unsupported_media_type", f"{upload.filename or 'upload'} is not a PDF")
    return await asyncio.to_thread(PdfDocument.from_bytes, data)


def pages_arg(spec: str | None, doc: PdfDocument) -> list[int] | None:
    """1-based page string from a form field, as 0-based indices (None = all)."""
    if spec is None or not spec.strip():
        return None
    return parse_page_ranges(spec, doc.page_count)


def _apply_post(doc: PdfDocument, post: PostProcess | None) -> bytes:
    if post is None:
        return doc.to_bytes()
    if post.stamp_text is not None:
        doc = doc.stamp_text(**post.stamp_text.model_dump())
    if post.metadata is not None:
        doc = doc.set_metadata(**post.metadata.model_dump())
    return doc.to_bytes(compress=post.compress)


async def pdf_response(
    doc: PdfDocument, filename: str = "document.pdf", post: PostProcess | None = None
) -> Response:
    data = await asyncio.to_thread(_apply_post, doc, post)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


def zip_response(files: Sequence[tuple[str, bytes]], filename: str) -> Response:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files:
            archive.writestr(name, data)
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
