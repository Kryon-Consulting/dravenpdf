"""POST /v1/convert/*: images <-> PDF and text extraction (multipart/form-data)."""

from __future__ import annotations

import asyncio
from collections.abc import Generator, Iterator
from contextlib import closing
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response

from dravenpdf.document.images import ImageFormat
from dravenpdf.document.pdf import PdfDocument
from dravenpdf.options import PaperSize
from dravenpdf.server.deps import (
    PDF_RESPONSE,
    pages_arg,
    pdf_response,
    read_pdf,
    require_api_key,
    settings_of,
    zip_response,
)
from dravenpdf.server.schemas import TextResponse

router = APIRouter(prefix="/v1/convert", tags=["convert"], dependencies=[Depends(require_api_key)])


@router.post("/images-to-pdf", response_class=Response, responses=PDF_RESPONSE)
async def images_to_pdf(
    files: Annotated[list[UploadFile], File(description="Images, one page each, in order.")],
    paper: Annotated[PaperSize | None, Form(description="Fit onto this paper.")] = None,
    landscape: Annotated[bool, Form()] = False,
    margin: Annotated[float, Form(ge=0, description="Points, with paper.")] = 0,
) -> Response:
    images = [await f.read() for f in files]
    doc = await asyncio.to_thread(
        PdfDocument.from_images, images, paper=paper, landscape=landscape, margin=margin
    )
    return await pdf_response(doc, "images.pdf")


@router.post(
    "/pdf-to-images",
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}, "description": "page-N.png/jpg"}},
)
async def pdf_to_images(
    request: Request,
    file: Annotated[UploadFile, File(description="A PDF.")],
    dpi: Annotated[int, Form()] = 150,
    format: Annotated[ImageFormat, Form()] = "png",
    pages: Annotated[str | None, Form(description="1-based pages (default: all).")] = None,
) -> Response:
    doc = await read_pdf(file)
    targets = pages_arg(pages, doc) or list(range(doc.page_count))
    settings = settings_of(request)
    # Nothing is rendered yet: each page is rendered, encoded and written to the ZIP
    # one at a time inside zip_response's worker thread.
    images = doc.iter_images(
        dpi=dpi,
        fmt=format,
        pages=targets,
        max_pixels=settings.max_image_pixels,
        max_total_bytes=settings.max_output_bytes,
    )
    extension = "jpg" if format == "jpeg" else "png"
    # PNG and JPEG are compressed already; deflating them again only costs CPU.
    return await zip_response(
        _named(targets, images, extension), "pages.zip",
        max_bytes=settings.max_output_bytes, compress=False,
    )  # fmt: skip


def _named(
    targets: list[int], images: Generator[bytes, None, None], ext: str
) -> Iterator[tuple[str, bytes]]:
    # Not zip()/enumerate(): they keep their last item, which would hold the previous
    # image while the next one renders. Closing this generator closes ``images``.
    with closing(images):
        for index in targets:
            data = next(images)
            yield f"page-{index + 1}.{ext}", data
            del data


@router.post("/text")
async def text(file: Annotated[UploadFile, File(description="A PDF.")]) -> TextResponse:
    doc = await read_pdf(file)
    return TextResponse(pages=await asyncio.to_thread(doc.extract_text))
