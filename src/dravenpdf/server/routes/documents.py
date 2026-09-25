"""POST /v1/pdf/*: operations on uploaded PDFs (multipart/form-data).

Page fields are 1-based strings like "1,3-5,8-".
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response

from dravenpdf.document.pages import parse_page_ranges
from dravenpdf.document.pdf import PdfDocument
from dravenpdf.document.stamp import Position
from dravenpdf.server.deps import (
    PDF_RESPONSE,
    ZIP_RESPONSE,
    pages_arg,
    pdf_response,
    read_pdf,
    renderer_of,
    require_api_key,
    zip_response,
)
from dravenpdf.server.errors import ApiError

router = APIRouter(prefix="/v1/pdf", tags=["pdf"], dependencies=[Depends(require_api_key)])


PdfFile = Annotated[UploadFile, File(description="A PDF.")]
PagesField = Annotated[str | None, Form(description="1-based pages, e.g. 1,3-5,8- (default: all).")]


@router.post("/merge", response_class=Response, responses=PDF_RESPONSE)
async def merge(
    files: Annotated[list[UploadFile], File(description="Two or more PDFs, in order.")],
) -> Response:
    if len(files) < 2:
        raise ApiError("invalid_request", "send at least two files to merge")
    docs = [await read_pdf(f) for f in files]
    return await pdf_response(await asyncio.to_thread(PdfDocument.merge, docs), "merged.pdf")


@router.post("/split", response_class=Response, responses=ZIP_RESPONSE)
async def split(
    file: PdfFile,
    every: Annotated[int | None, Form(description="Pages per part.")] = None,
    ranges: Annotated[
        list[str] | None, Form(description="One part per range; repeat the field.")
    ] = None,
) -> Response:
    doc = await read_pdf(file)
    parts = await asyncio.to_thread(doc.split, every=every, ranges=ranges)
    files = [(f"part-{i}.pdf", await asyncio.to_thread(p.to_bytes)) for i, p in enumerate(parts, 1)]
    return zip_response(files, "parts.zip")


@router.post("/extract", response_class=Response, responses=PDF_RESPONSE)
async def extract(
    file: PdfFile, ranges: Annotated[str, Form(description="1-based pages, e.g. 2-5,8.")]
) -> Response:
    doc = await read_pdf(file)
    return await pdf_response(await asyncio.to_thread(doc.extract, ranges), "extract.pdf")


@router.post("/rotate", response_class=Response, responses=PDF_RESPONSE)
async def rotate(
    file: PdfFile,
    degrees: Annotated[int, Form(description="Clockwise, a multiple of 90.")] = 90,
    pages: PagesField = None,
) -> Response:
    doc = await read_pdf(file)
    result = await asyncio.to_thread(doc.rotate, degrees, pages_arg(pages, doc))
    return await pdf_response(result, "rotated.pdf")


@router.post("/delete", response_class=Response, responses=PDF_RESPONSE)
async def delete(
    file: PdfFile, pages: Annotated[str, Form(description="1-based pages to remove.")]
) -> Response:
    doc = await read_pdf(file)
    indices = parse_page_ranges(pages, doc.page_count)
    return await pdf_response(await asyncio.to_thread(doc.delete, indices), "document.pdf")


@router.post("/reorder", response_class=Response, responses=PDF_RESPONSE)
async def reorder(
    file: PdfFile,
    order: Annotated[str, Form(description="Every page once, 1-based, e.g. 3,1,2.")],
) -> Response:
    doc = await read_pdf(file)
    indices = parse_page_ranges(order, doc.page_count)
    return await pdf_response(await asyncio.to_thread(doc.reorder, indices), "document.pdf")


@router.post("/stamp", response_class=Response, responses=PDF_RESPONSE)
async def stamp(
    request: Request,
    file: PdfFile,
    text: Annotated[str | None, Form(description="Text watermark (cp1252).")] = None,
    image: Annotated[UploadFile | None, File(description="Image to stamp.")] = None,
    html: Annotated[str | None, Form(description="HTML to render and stamp.")] = None,
    opacity: Annotated[float | None, Form(gt=0, le=1)] = None,
    angle: Annotated[float, Form(description="Text angle, degrees.")] = 45,
    font_size: Annotated[float, Form(gt=0)] = 48,
    color: Annotated[str, Form(description="Text color, #RRGGBB.")] = "#FF0000",
    width: Annotated[float | None, Form(gt=0, description="Image width, points.")] = None,
    position: Annotated[Position, Form()] = "center",
    margin: Annotated[float, Form(ge=0)] = 36,
    under: Annotated[bool, Form(description="Put it behind the page content.")] = False,
    pages: PagesField = None,
) -> Response:
    """Stamp exactly one of `text`, `image` or `html` onto pages."""
    if sum(x is not None for x in (text, image, html)) != 1:
        raise ApiError("invalid_request", "send exactly one of text, image or html")
    doc = await read_pdf(file)
    targets = pages_arg(pages, doc)
    if text is not None:
        result = await asyncio.to_thread(
            doc.stamp_text, text, font_size=font_size, color=color,
            opacity=0.3 if opacity is None else opacity, angle=angle,
            position=position, margin=margin, pages=targets, under=under,
        )  # fmt: skip
    elif image is not None:
        result = await asyncio.to_thread(
            doc.stamp_image, await image.read(), width=width, position=position, margin=margin,
            opacity=1.0 if opacity is None else opacity, pages=targets, under=under,
        )  # fmt: skip
    else:
        assert html is not None
        result = await doc.stamp_html(
            renderer_of(request), html, opacity=1.0 if opacity is None else opacity,
            pages=targets, under=under,
        )  # fmt: skip
    return await pdf_response(result, "stamped.pdf")


@router.post("/metadata", response_class=Response, responses=PDF_RESPONSE)
async def metadata(
    file: PdfFile,
    title: Annotated[str | None, Form(description='"" removes the field.')] = None,
    author: Annotated[str | None, Form()] = None,
    subject: Annotated[str | None, Form()] = None,
    keywords: Annotated[str | None, Form()] = None,
) -> Response:
    doc = await read_pdf(file)
    result = await asyncio.to_thread(
        doc.set_metadata, title=title, author=author, subject=subject, keywords=keywords
    )
    return await pdf_response(result, "document.pdf")


@router.post("/compress", response_class=Response, responses=PDF_RESPONSE)
async def compress(file: PdfFile) -> Response:
    doc = await read_pdf(file)
    data = await asyncio.to_thread(doc.to_bytes, compress=True)
    return Response(
        data,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="compressed.pdf"'},
    )
