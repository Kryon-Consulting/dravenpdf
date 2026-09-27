"""Images to PDF (img2pdf) and PDF pages to images (pdfium)."""

from __future__ import annotations

import io
import logging
import warnings
from collections.abc import Generator, Iterable, Sequence
from typing import Any, Literal

import img2pdf
import pypdfium2 as pdfium
from PIL import Image, UnidentifiedImageError

from dravenpdf.document._pdfium import LOCK, open_pdf
from dravenpdf.document.pages import normalize_indices
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


# Formats whose frames can each have their own size. Others (GIF, APNG, WebP) report
# one canvas size for every frame, and seeking through them would decode each frame.
_FRAMES_WITH_OWN_SIZE = frozenset({"TIFF", "MPO"})


def _check_pixels(images: Sequence[bytes], max_pixels: int) -> None:
    """Refuse images with a frame (img2pdf makes each frame a page) larger than
    ``max_pixels``, reading only headers."""
    for number, data in enumerate(images, start=1):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as image:
                    sizes = _frame_sizes(image)
        except Image.DecompressionBombError:
            raise LimitExceededError(f"image {number} is too large to open") from None
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
            continue  # img2pdf reports unreadable images with its own message
        for width, height in sizes:
            if width * height > max_pixels:
                raise LimitExceededError(
                    f"image {number} is {width * height:,} pixels; the limit is {max_pixels:,}"
                )


def _frame_sizes(image: Image.Image) -> list[tuple[int, int]]:
    sizes = [image.size]
    if image.format in _FRAMES_WITH_OWN_SIZE:
        for frame in range(1, getattr(image, "n_frames", 1)):
            image.seek(frame)  # reads the frame's header, not its pixels
            sizes.append(image.size)
    return sizes


def images_to_pdf(
    images: Sequence[bytes],
    *,
    paper: PaperSize | None = None,
    landscape: bool = False,
    margin: float = 0,
    max_pixels: int | None = None,
) -> bytes:
    """One page per image (PNG, JPEG, GIF, TIFF, WebP, ...), and per frame of a
    multi-frame image.

    Without ``paper`` each page is the image's own size. With ``paper`` each image is
    scaled to fit inside the page and its ``margin`` (points), keeping its aspect ratio.
    JPEG and many PNGs are embedded without re-encoding. ``max_pixels`` refuses any
    image (or frame) with more pixels (:class:`LimitExceededError`), before decoding.
    """
    if not images:
        raise PdfOperationError("no images given")
    if max_pixels is not None:
        _check_pixels(images, max_pixels)
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
    images = iter_pdf_to_images(
        pdf, dpi=dpi, fmt=fmt, pages=pages, jpeg_quality=jpeg_quality,
        max_pixels=max_pixels, max_total_bytes=max_total_bytes,
    )  # fmt: skip
    return list(images)


def check_image_options(dpi: int, fmt: str, jpeg_quality: int) -> None:
    """Raise :class:`PdfOperationError` for options :func:`pdf_to_images` refuses."""
    if not MIN_DPI <= dpi <= MAX_DPI:
        raise PdfOperationError(f"dpi must be between {MIN_DPI} and {MAX_DPI}")
    if fmt not in ("png", "jpeg"):
        raise PdfOperationError("fmt must be 'png' or 'jpeg'")
    if not 1 <= jpeg_quality <= 100:
        raise PdfOperationError("jpeg_quality must be between 1 and 100")


def iter_pdf_to_images(
    pdf: bytes,
    *,
    dpi: int = 150,
    fmt: ImageFormat = "png",
    pages: Iterable[int] | None = None,
    jpeg_quality: int = 85,
    max_pixels: int | None = None,
    max_total_bytes: int | None = None,
) -> Generator[bytes, None, None]:
    """Like :func:`pdf_to_images`, one image at a time.

    ``dpi``, ``fmt`` and ``jpeg_quality`` are checked now; the PDF is opened and each
    page rendered as the iterator is consumed, so page and limit errors come from it.
    The PDF stays open until the iterator is exhausted or closed: close it (e.g. with
    ``contextlib.closing``) in the thread that consumed it when stopping early.
    """
    check_image_options(dpi, fmt, jpeg_quality)
    targets = None if pages is None else list(pages)
    return _render_pages(pdf, dpi, fmt, targets, jpeg_quality, max_pixels, max_total_bytes)


def _render_pages(
    pdf: bytes,
    dpi: int,
    fmt: str,
    pages: list[int] | None,
    jpeg_quality: int,
    max_pixels: int | None,
    max_total_bytes: int | None,
) -> Generator[bytes, None, None]:
    total = 0
    with open_pdf(pdf, hold_lock=False) as doc:
        with LOCK:
            count = len(doc)
        indices = range(count) if pages is None else normalize_indices(pages, count)
        for index in indices:
            with LOCK:
                image = _render_page(doc, index, dpi, max_pixels)
            data = _encode(image, fmt, jpeg_quality)  # outside the lock: the slow part
            del image
            total += len(data)
            if max_total_bytes is not None and total > max_total_bytes:
                raise LimitExceededError(
                    f"the images are larger than the {max_total_bytes:,}-byte limit "
                    "(lower the dpi, use jpeg, or ask for fewer pages)"
                )
            yield data
            del data  # don't hold this image while the next one renders


def _render_page(
    doc: pdfium.PdfDocument, index: int, dpi: int, max_pixels: int | None
) -> Image.Image:
    """Render one page to a PIL image that owns its pixels. Call with ``LOCK`` held."""
    scale = dpi / 72
    page = doc[index]
    try:
        if max_pixels is not None:
            width, height = page.get_size()
            pixels = round(width * scale) * round(height * scale)
            if pixels > max_pixels:
                raise LimitExceededError(
                    f"page {index + 1} would be {pixels:,} pixels at {dpi} dpi; "
                    f"the limit is {max_pixels:,} (lower the dpi)"
                )
        bitmap = page.render(scale=scale)
        try:
            # to_pil() shares the bitmap's buffer for some formats: copy before closing.
            image: Image.Image = bitmap.to_pil().copy()
            return image
        finally:
            bitmap.close()
    finally:
        page.close()


def _encode(image: Image.Image, fmt: str, quality: int) -> bytes:
    buffer = io.BytesIO()
    if fmt == "png":
        image.save(buffer, format="PNG", optimize=False)
    else:
        image.convert("RGB").save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()
