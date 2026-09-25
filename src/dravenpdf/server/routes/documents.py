"""POST /v1/pdf/*: operations on uploaded PDFs (multipart/form-data).

Page fields are 1-based strings like "1,3-5,8-".
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response

from dravenpdf.document.pages import parse_page_ranges
from dravenpdf.document.pdf import PdfDocument
from dravenpdf.document.signing import SignatureBox, SigningKey
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
from dravenpdf.server.schemas import (
    FormFieldOut,
    FormFieldsResponse,
    SignatureOut,
    SignaturesResponse,
    SigningKeyOut,
    SigningKeysResponse,
)

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


@router.post("/form/fields")
async def form_fields(
    file: PdfFile, password: Annotated[str | None, Form()] = None
) -> FormFieldsResponse:
    """The PDF's form fields (empty list if it has no form)."""
    doc = await read_pdf(file, password)
    fields = await asyncio.to_thread(doc.form_fields)
    return FormFieldsResponse(
        fields=[FormFieldOut(**{**asdict(f), "options": list(f.options)}) for f in fields]
    )


@router.post("/form/fill", response_class=Response, responses=PDF_RESPONSE)
async def form_fill(
    file: PdfFile,
    values: Annotated[
        str, Form(description='JSON object: {"field name": "text" | true/false | "option"}')
    ],
    flatten: Annotated[bool, Form(description="Burn the values in and remove the form.")] = False,
    password: Annotated[str | None, Form()] = None,
) -> Response:
    """Fill form fields; all values are checked before any is applied."""
    try:
        parsed = json.loads(values)
    except json.JSONDecodeError as exc:
        raise ApiError("invalid_request", f"values: not valid JSON ({exc.msg})") from None
    if not isinstance(parsed, dict) or not all(
        isinstance(v, str | bool) or v is None for v in parsed.values()
    ):
        raise ApiError("invalid_request", "values: must be an object of text, true/false or null")
    doc = await read_pdf(file, password)
    result = await asyncio.to_thread(doc.fill_form, parsed, flatten=flatten)
    return await pdf_response(result, "filled.pdf")


@router.post("/form/flatten", response_class=Response, responses=PDF_RESPONSE)
async def form_flatten(file: PdfFile, password: Annotated[str | None, Form()] = None) -> Response:
    """Burn the current field values into the pages and remove the form."""
    doc = await read_pdf(file, password)
    return await pdf_response(await asyncio.to_thread(doc.flatten_form), "flattened.pdf")


# ---------------------------------------------------------------- signatures


@router.get("/signing-keys")
async def signing_keys(request: Request) -> SigningKeysResponse:
    """The signing keys configured on this server (names and certificates only)."""
    keys: dict[str, SigningKey] = request.app.state.signing_keys
    return SigningKeysResponse(
        keys=[
            SigningKeyOut(name=name, subject=key.subject, not_after=key.not_after)
            for name, key in sorted(keys.items())
        ]
    )


@router.post("/sign", response_class=Response, responses=PDF_RESPONSE)
async def sign(
    request: Request,
    file: PdfFile,
    key: Annotated[str, Form(description="Name of a key configured on the server.")],
    reason: Annotated[str | None, Form(max_length=500)] = None,
    location: Annotated[str | None, Form(max_length=500)] = None,
    contact: Annotated[str | None, Form(max_length=500)] = None,
    field_name: Annotated[str, Form(min_length=1, max_length=100)] = "Signature",
    page: Annotated[int | None, Form(ge=1, description="Visible signature: 1-based page.")] = None,
    x: Annotated[float | None, Form(ge=0, description="Points from the left edge.")] = None,
    y: Annotated[float | None, Form(ge=0, description="Points from the bottom edge.")] = None,
    width: Annotated[float | None, Form(gt=0)] = None,
    height: Annotated[float | None, Form(gt=0)] = None,
    timestamp: Annotated[
        bool, Form(description="Add an RFC 3161 timestamp (server's timestamp_url).")
    ] = False,
    password: Annotated[str | None, Form(description="The input's password, if any.")] = None,
) -> Response:
    """Digitally sign with a server-side key. The response is the signed file; don't
    change it afterwards, or the signature breaks."""
    keys: dict[str, SigningKey] = request.app.state.signing_keys
    if not keys:
        raise ApiError("invalid_request", "signing is not configured on this server")
    if key not in keys:
        raise ApiError("invalid_request", f"unknown signing key {key!r}")
    placement = (page, x, y, width, height)
    if any(v is not None for v in placement) and any(v is None for v in placement):
        raise ApiError("invalid_request", "a visible signature needs page, x, y, width and height")
    timestamp_url = settings_of(request).timestamp_url
    if timestamp and not timestamp_url:
        raise ApiError("invalid_request", "no timestamp server is configured")
    doc = await read_pdf(file, password)
    box = None
    if page is not None and x is not None and y is not None and width and height:
        if page > doc.page_count:
            raise ApiError("invalid_request", f"page {page} is past the last page")
        box = SignatureBox(page=page - 1, x=x, y=y, width=width, height=height)
    signed = await asyncio.to_thread(
        doc.sign, keys[key], field_name=field_name, reason=reason, location=location,
        contact=contact, box=box, timestamp_url=timestamp_url if timestamp else None,
    )  # fmt: skip
    return await pdf_response(signed, "signed.pdf")


@router.post("/verify")
async def verify(
    request: Request,
    file: PdfFile,
    password: Annotated[str | None, Form()] = None,
) -> SignaturesResponse:
    """Check the embedded signatures against the server's trust roots."""
    doc = await read_pdf(file, password)
    roots: list[bytes] = request.app.state.trust_roots
    infos = await asyncio.to_thread(doc.verify_signatures, roots)
    return SignaturesResponse(
        signatures=[SignatureOut(**asdict(info), ok=info.ok) for info in infos]
    )
