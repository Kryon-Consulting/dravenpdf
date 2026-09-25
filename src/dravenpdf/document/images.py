"""Images to PDF (img2pdf) and PDF pages to images (pdfium)."""

from __future__ import annotations

import io
import logging
from collections.abc import Iterable, Sequence
from typing import Any, Literal

import img2pdf

from dravenpdf.document._pdfium import open_pdf
from dravenpdf.errors import LimitExceededError, PdfOperationError
from dravenpdf.options import PaperSize

ImageFormat = Literal["png", "jpeg"]

_MM = 72 / 25.4
PAPER_SIZES_PT: dict[str, tuple[float, float]] = {
    "A3": (297 * _MM, 420 * _MM),
    "A4": (210 * _MM, 297 * _MM),
    "A5": (148 * _MM, 210 * _MM),
    "Letter": (8.5 * 72, 11 * 72),
    "Legal": (8.5 * 72, 14 * 72),
    "Tabloid": (11 * 72, 17 * 72),
}

MIN_DPI, MAX_DPI = 10, 600

_IMG2PDF_ERRORS = (
    img2pdf.ImageOpenError,
    img2pdf.PdfTooLargeError,
    img2pdf.UnsupportedColorspaceError,
    img2pdf.JpegColorspaceError,
    img2pdf.NegativeDimensionError,
    img2pdf.ExifOrientationError,
    ValueError,
    OSError,
)

# img2pdf logs an INFO/WARNING line for every image with transparency; that's expected.
logging.getLogger("img2pdf").setLevel(logging.ERROR)


def images_to_pdf(
    images: Sequence[bytes],
    *,
    paper: PaperSize | None = None,
    landscape: bool = False,
    margin: float = 0,
) -> bytes:
    """One page per image (PNG, JPEG, GIF, TIFF, WebP, ...).

    Without ``paper`` each page is the image's own size. With ``paper`` each image is
    scaled to fit inside the page and its ``margin`` (points), keeping its aspect ratio.
    JPEG and many PNGs are embedded without re-encoding.
    """
    if not images:
        raise PdfOperationError("no images given")
    options: dict[str, Any] = {"rotation": img2pdf.Rotation.ifvalid}
    if paper is not None:
        if margin < 0:
            raise PdfOperationError("margin must not be negative")
        w, h = PAPER_SIZES_PT[paper]
        if landscape:
            w, h = h, w
        options["layout_fun"] = img2pdf.get_layout_fun(
            pagesize=(w, h), border=(margin, margin), fit=img2pdf.FitMode.into
        )
    try:
        result: bytes = img2pdf.convert(list(images), **options)
    except _IMG2PDF_ERRORS as exc:
        raise PdfOperationError(f"could not convert image: {exc}") from exc
    return result


def pdf_to_images(
    pdf: bytes,
    *,
    dpi: int = 150,
    fmt: ImageFormat = "png",
    pages: Iterable[int] | None = None,
    jpeg_quality: int = 85,
    max_pixels: int | None = None,
    max_total_bytes: int | None = None,
) -> list[bytes]:
    """Render pages to PNG or JPEG. ``pages`` are 0-based indices (default: all).

    ``max_pixels`` caps one page's width x height at ``dpi``, checked before the page
    is rendered. ``max_total_bytes`` caps the encoded images together; rendering stops
    as soon as it is passed. Either raises :class:`LimitExceededError`.
    """
    if not MIN_DPI <= dpi <= MAX_DPI:
        raise PdfOperationError(f"dpi must be between {MIN_DPI} and {MAX_DPI}")
    if fmt not in ("png", "jpeg"):
        raise PdfOperationError("fmt must be 'png' or 'jpeg'")
    if not 1 <= jpeg_quality <= 100:
        raise PdfOperationError("jpeg_quality must be between 1 and 100")
    scale = dpi / 72
    results: list[bytes] = []
    total = 0
    with open_pdf(pdf) as doc:
        count = len(doc)
        indices = range(count) if pages is None else list(pages)
        for index in indices:
            if not -count <= index < count:
                raise PdfOperationError(f"page index {index} is out of range for {count} pages")
            page = doc[index % count]
            try:
                if max_pixels is not None:
                    width, height = page.get_size()
                    pixels = round(width * scale) * round(height * scale)
                    if pixels > max_pixels:
                        raise LimitExceededError(
                            f"page {index % count + 1} would be {pixels:,} pixels at {dpi} dpi; "
                            f"the limit is {max_pixels:,} (lower the dpi)"
                        )
                bitmap = page.render(scale=scale)
                image = bitmap.to_pil()
            finally:
                page.close()
            buffer = io.BytesIO()
            if fmt == "png":
                image.save(buffer, format="PNG", optimize=False)
            else:
                image.convert("RGB").save(buffer, format="JPEG", quality=jpeg_quality)
            results.append(buffer.getvalue())
            total += len(results[-1])
            if max_total_bytes is not None and total > max_total_bytes:
                raise LimitExceededError(
                    f"the images are larger than the {max_total_bytes:,}-byte limit "
                    "(lower the dpi, use jpeg, or ask for fewer pages)"
                )
    return results
