"""POST /v1/pdf/*: operations on uploaded PDFs (multipart/form-data).

Page fields are 1-based strings like "1,3-5,8-".
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
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
    settings_of,
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
    request: Request,
    file: PdfFile,
    every: Annotated[int | None, Form(description="Pages per part.")] = None,
    ranges: Annotated[
        list[str] | None, Form(description="One part per range; repeat the field.")
    ] = None,
) -> Response:
    doc = await read_pdf(file)
    # iter_split checks the arguments now; each part is then built, serialized and
    # dropped in turn inside zip_response's worker thread, so only one exists at a time.
    files = _serialized_parts(doc.iter_split(every=every, ranges=ranges))
    return await zip_response(files, "parts.zip", max_bytes=settings_of(request).max_output_bytes)


def _serialized_parts(parts: Iterator[PdfDocument]) -> Iterator[tuple[str, bytes]]:
    # A plain counter, not enumerate(): enumerate keeps its last (index, item) tuple
    # for reuse, which would keep the previous part alive while the next is built.
    number = 0
    for part in parts:
        number += 1  # noqa: SIM113 - see above
        data = part.to_bytes()
        del part  # drop this part before the next one is built
        yield f"part-{number}.pdf", data


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


@router.post("/encrypt", response_class=Response, responses=PDF_RESPONSE)
async def encrypt(
    file: PdfFile,
    user_password: Annotated[
        str, Form(description="Needed to open the file; empty means anyone can open it.")
    ] = "",
    owner_password: Annotated[
        str | None, Form(description="Unlocks full access; random if omitted.")
    ] = None,
    password: Annotated[
        str | None, Form(description="The input's password, if it is already protected.")
    ] = None,
    allow_print: Annotated[bool, Form()] = True,
    allow_copy: Annotated[bool, Form()] = True,
    allow_modify: Annotated[bool, Form()] = True,
    allow_annotate: Annotated[bool, Form()] = True,
    allow_forms: Annotated[bool, Form()] = True,
) -> Response:
    """Encrypt with AES-256. The allow_* flags are advisory (honoured by viewers)."""
    if (
        not user_password
        and not owner_password
        and all((allow_print, allow_copy, allow_modify, allow_annotate, allow_forms))
    ):
        raise ApiError("invalid_request", "set a password or restrict a permission")
    doc = await read_pdf(file, password)
    result = await asyncio.to_thread(
        doc.encrypt,
        user_password=user_password, owner_password=owner_password,
        allow_print=allow_print, allow_copy=allow_copy, allow_modify=allow_modify,
        allow_annotate=allow_annotate, allow_forms=allow_forms,
    )  # fmt: skip
    return await pdf_response(result, "encrypted.pdf")


@router.post("/decrypt", response_class=Response, responses=PDF_RESPONSE)
async def decrypt(
    file: PdfFile,
    password: Annotated[str, Form(description="The user or owner password.")],
) -> Response:
    """Remove password protection (needs the password)."""
    doc = await read_pdf(file, password)
    return await pdf_response(await asyncio.to_thread(doc.decrypt), "decrypted.pdf")
