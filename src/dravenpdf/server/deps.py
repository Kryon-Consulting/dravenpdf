"""Shared request helpers: auth, the renderer, uploads and responses."""

from __future__ import annotations

import asyncio
import secrets
import zipfile
from collections.abc import Awaitable, Iterable, Iterator
from tempfile import SpooledTemporaryFile
from typing import Annotated, Any, TypeVar

from fastapi import Header, Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from dravenpdf.document.pages import parse_page_ranges
from dravenpdf.document.pdf import PdfDocument
from dravenpdf.errors import LimitExceededError
from dravenpdf.options import RenderOptions
from dravenpdf.render.renderer import AsyncRenderer
from dravenpdf.server.config import Settings
from dravenpdf.server.errors import ApiError
from dravenpdf.server.metrics import Metrics
from dravenpdf.server.schemas import PostProcess

T = TypeVar("T")

# ZIPs up to this size stay in memory; larger ones go to a temporary file.
_ZIP_SPOOL_MEMORY_BYTES = 8 * 1024 * 1024
_ZIP_CHUNK_BYTES = 64 * 1024

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
    headers = {"Content-Disposition": f'inline; filename="{filename}"'}
    report = doc.render_report  # read before post-processing makes a new document
    if report is not None:
        # Counts only: details can be long, and they are in the server log.
        headers["X-DravenPdf-Resource-Errors"] = str(report.resource_problems)
        headers["X-DravenPdf-Page-Errors"] = str(len(report.page_errors))
        headers["X-DravenPdf-Blocked"] = str(len(report.blocked))
    data = await asyncio.to_thread(_apply_post, doc, post)
    return Response(content=data, media_type="application/pdf", headers=headers)


async def zip_response(
    files: Iterable[tuple[str, bytes]], filename: str, *, max_bytes: int, compress: bool = True
) -> StreamingResponse:
    """A ZIP of ``files``, built in a worker thread and streamed from a spooled file.

    ``files`` is consumed in the worker thread, so it may be a generator that does
    the work lazily. Past ``max_bytes`` of (uncompressed) content it raises
    :class:`LimitExceededError`. Pass ``compress=False`` for already-compressed data.
    """
    spool = await asyncio.to_thread(_write_zip, files, max_bytes, compress)
    size = spool.tell()
    spool.seek(0)
    return StreamingResponse(
        _read_chunks(spool),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(size),
        },
    )


def _write_zip(
    files: Iterable[tuple[str, bytes]], max_bytes: int, compress: bool
) -> SpooledTemporaryFile[bytes]:
    # Not a with block: the response closes it after streaming (_read_chunks).
    spool: SpooledTemporaryFile[bytes] = SpooledTemporaryFile(  # noqa: SIM115
        max_size=_ZIP_SPOOL_MEMORY_BYTES
    )
    try:
        method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
        total = 0
        with zipfile.ZipFile(spool, "w", method) as archive:
            for name, data in files:
                total += len(data)
                if total > max_bytes:
                    raise LimitExceededError(
                        f"the result is larger than the {max_bytes:,}-byte output limit"
                    )
                archive.writestr(name, data)
                del data  # written; don't hold it while the next file is produced
    except BaseException:
        spool.close()
        raise
    return spool


def _read_chunks(spool: SpooledTemporaryFile[bytes]) -> Iterator[bytes]:
    # A sync iterator: Starlette reads it in a worker thread.
    try:
        while chunk := spool.read(_ZIP_CHUNK_BYTES):
            yield chunk
    finally:
        spool.close()
