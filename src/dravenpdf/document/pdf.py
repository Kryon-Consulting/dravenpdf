"""PdfDocument: an in-memory PDF with a chainable, non-mutating API."""

from __future__ import annotations

import io
from collections.abc import Iterable, Sequence
from os import PathLike
from pathlib import Path
from typing import Protocol

import pikepdf

from dravenpdf.document import images as image_ops
from dravenpdf.document import pages as ops
from dravenpdf.document import stamp as stamp_ops
from dravenpdf.document import text as text_ops
from dravenpdf.document.images import ImageFormat
from dravenpdf.document.stamp import Position
from dravenpdf.errors import InvalidPdfError, PdfOperationError
from dravenpdf.options import Margins, PaperSize, RenderOptions


class HtmlRenderer(Protocol):
    """What stamp_html needs from a renderer (AsyncRenderer fits)."""

    async def from_html(
        self, html: str, options: RenderOptions | None = None, *, base_url: str | None = None
    ) -> PdfDocument: ...


# PdfDocument.metadata keys and the docinfo entries they map to.
METADATA_KEYS: dict[str, str] = {
    "title": "/Title",
    "author": "/Author",
    "subject": "/Subject",
    "keywords": "/Keywords",
    "creator": "/Creator",
    "producer": "/Producer",
    "creation_date": "/CreationDate",
    "mod_date": "/ModDate",
}
_EDITABLE = ("title", "author", "subject", "keywords", "creator", "producer")


def _as_pdf(document: PdfDocument | bytes) -> pikepdf.Pdf:
    if isinstance(document, PdfDocument):
        return document._pdf
    return PdfDocument.from_bytes(document)._pdf


class PdfDocument:
    """A PDF held in memory, backed by :class:`pikepdf.Pdf`.

    Every operation returns a **new** PdfDocument and leaves this one unchanged,
    so calls chain (``doc.rotate(90).delete([0]).save(...)``) and a document can be
    shared safely. A single PdfDocument is not safe to use from several threads
    at once.

    Page numbers: integer arguments are 0-based (negative counts from the end);
    range strings like ``"1-3,5,8-"`` are 1-based.
    """

    def __init__(self, pdf: pikepdf.Pdf) -> None:
        self._pdf = pdf

    # ------------------------------------------------------------------ loading

    @classmethod
    def from_bytes(cls, data: bytes) -> PdfDocument:
        try:
            return cls(pikepdf.open(io.BytesIO(data)))
        except pikepdf.PasswordError as exc:
            raise InvalidPdfError("password-protected PDFs are not supported") from exc
        except pikepdf.PdfError as exc:
            raise InvalidPdfError(f"not a readable PDF: {exc}") from exc

    @classmethod
    def open(cls, path: str | PathLike[str]) -> PdfDocument:
        return cls.from_bytes(Path(path).read_bytes())

    @classmethod
    def from_images(
        cls,
        images: Sequence[bytes],
        *,
        paper: PaperSize | None = None,
        landscape: bool = False,
        margin: float = 0,
    ) -> PdfDocument:
        """One page per image. Without ``paper``, pages are the images' own size;
        with it, images are fitted inside ``margin`` (points), keeping aspect ratio."""
        return cls.from_bytes(
            image_ops.images_to_pdf(images, paper=paper, landscape=landscape, margin=margin)
        )

    @classmethod
    def merge(cls, documents: Iterable[PdfDocument | bytes]) -> PdfDocument:
        """All pages of each document, in order. Metadata comes from the first."""
        pdfs = [_as_pdf(d) for d in documents]
        return cls(ops.merge(pdfs))

    # ------------------------------------------------------------------ reading

    @property
    def page_count(self) -> int:
        return len(self._pdf.pages)

    def __len__(self) -> int:
        return self.page_count

    @property
    def metadata(self) -> dict[str, str]:
        """Document info (title, author, ...), only the fields that are set."""
        docinfo = self._pdf.docinfo
        result = {}
        for name, key in METADATA_KEYS.items():
            if key in docinfo:
                result[name] = str(docinfo[key])
        return result

    def page_size(self, index: int = 0) -> tuple[float, float]:
        """(width, height) of a page in points, as displayed (rotation applied)."""
        (i,) = ops.normalize_indices([index], self.page_count)
        page = self._pdf.pages[i]
        box = [float(v) for v in page.mediabox]
        width, height = abs(box[2] - box[0]), abs(box[3] - box[1])
        rotation = int(page.rotation) % 360
        return (height, width) if rotation in (90, 270) else (width, height)

    # ------------------------------------------------------------------ page operations

    def extract(self, ranges: str) -> PdfDocument:
        """A new document with the pages in ``ranges`` (1-based, e.g. ``"2-5"``)."""
        return PdfDocument(ops.extract(self._pdf, ranges))

    def split(
        self, *, every: int | None = None, ranges: Sequence[str] | None = None
    ) -> list[PdfDocument]:
        """Split into chunks of ``every`` pages, or one document per range string."""
        if (every is None) == (ranges is None):
            raise PdfOperationError("pass exactly one of every= or ranges=")
        if every is not None:
            parts = ops.split_every(self._pdf, every)
        else:
            assert ranges is not None
            parts = ops.split_ranges(self._pdf, ranges)
        return [PdfDocument(p) for p in parts]

    def rotate(self, degrees: int, pages: Iterable[int] | None = None) -> PdfDocument:
        """Rotate clockwise by a multiple of 90 degrees; all pages unless ``pages`` given."""
        return PdfDocument(ops.rotate(self._pdf, degrees, pages))

    def delete(self, pages: Iterable[int]) -> PdfDocument:
        """Remove the given pages (0-based)."""
        return PdfDocument(ops.delete(self._pdf, pages))

    def reorder(self, order: Sequence[int]) -> PdfDocument:
        """Put pages in a new order, e.g. ``[2, 0, 1]``; must name every page once."""
        return PdfDocument(ops.reorder(self._pdf, order))

    def insert(self, other: PdfDocument | bytes, at: int) -> PdfDocument:
        """Insert all pages of ``other`` before page ``at``; ``at=page_count`` appends."""
        return PdfDocument(ops.insert(self._pdf, _as_pdf(other), at))

    def set_metadata(
        self,
        *,
        title: str | None = None,
        author: str | None = None,
        subject: str | None = None,
        keywords: str | None = None,
        creator: str | None = None,
        producer: str | None = None,
    ) -> PdfDocument:
        """Set document info fields. ``None`` leaves a field alone; ``""`` removes it."""
        values = dict(
            title=title, author=author, subject=subject,
            keywords=keywords, creator=creator, producer=producer,
        )  # fmt: skip
        result = ops.clone(self._pdf)
        for name in _EDITABLE:
            value = values[name]
            if value is None:
                continue
            key = METADATA_KEYS[name]
            if value == "":
                if key in result.docinfo:
                    del result.docinfo[key]
            else:
                result.docinfo[key] = value
        if "/Metadata" in result.Root:  # keep XMP in step so viewers agree
            with result.open_metadata(set_pikepdf_as_editor=False) as xmp:
                xmp.load_from_docinfo(result.docinfo, delete_missing=True)
        return PdfDocument(result)

    # ------------------------------------------------------------------ stamps

    def stamp_text(
        self,
        text: str,
        *,
        font_size: float = 48,
        color: str = "#FF0000",
        opacity: float = 0.3,
        angle: float = 45,
        position: Position = "center",
        margin: float = 36,
        pages: Iterable[int] | None = None,
        under: bool = False,
    ) -> PdfDocument:
        """Text watermark in Helvetica, ``angle`` degrees counter-clockwise.

        Western European (cp1252) characters only; use :meth:`stamp_html` for other
        scripts or richer styling. ``margin`` (points) applies to non-center positions.
        """
        result = ops.clone(self._pdf)
        stamp_ops.stamp_text(
            result, text, font_size=font_size, color=color, opacity=opacity, angle=angle,
            position=position, margin=margin, pages=pages, under=under,
        )  # fmt: skip
        return PdfDocument(result)

    def stamp_image(
        self,
        image: bytes,
        *,
        width: float | None = None,
        position: Position = "center",
        margin: float = 36,
        opacity: float = 1.0,
        pages: Iterable[int] | None = None,
        under: bool = False,
    ) -> PdfDocument:
        """Image stamp (PNG, JPEG, ...; transparency kept). ``width`` in points,
        default the image's size at 96 dpi; shrunk to fit inside the margins."""
        result = ops.clone(self._pdf)
        stamp_ops.stamp_image(
            result, image, width=width, position=position, margin=margin,
            opacity=opacity, pages=pages, under=under,
        )  # fmt: skip
        return PdfDocument(result)

    def overlay(
        self,
        stamp: PdfDocument | bytes,
        *,
        stamp_page: int = 0,
        opacity: float = 1.0,
        pages: Iterable[int] | None = None,
        under: bool = False,
    ) -> PdfDocument:
        """Draw a page of another PDF (letterhead, form background, ...) on each page,
        scaled to fit and centered. ``under=True`` puts it behind the content."""
        result = ops.clone(self._pdf)
        stamp_ops.overlay_page(
            result, _as_pdf(stamp), stamp_page=stamp_page, opacity=opacity,
            pages=pages, under=under,
        )  # fmt: skip
        return PdfDocument(ops._detach(result))

    async def stamp_html(
        self,
        renderer: HtmlRenderer,
        html: str,
        *,
        opacity: float = 1.0,
        pages: Iterable[int] | None = None,
        under: bool = False,
        base_url: str | None = None,
    ) -> PdfDocument:
        """Render ``html`` at each target page's size and draw it on the page.

        The HTML page is transparent except for what it draws, so it works for
        watermarks, headers, "PAID" badges and letterheads in any language.
        """
        targets = stamp_ops.target_pages(self._pdf, pages)
        by_size: dict[tuple[float, float], list[int]] = {}
        for index in targets:
            size = self.page_size(index)
            by_size.setdefault((round(size[0], 2), round(size[1], 2)), []).append(index)
        result = self
        for (width, height), indices in by_size.items():
            stamp = await renderer.from_html(
                html,
                RenderOptions(
                    width=f"{width / 72:.4f}in",
                    height=f"{height / 72:.4f}in",
                    margins=Margins(top="0", right="0", bottom="0", left="0"),
                ),
                base_url=base_url,
            )
            result = result.overlay(stamp, opacity=opacity, pages=indices, under=under)
        return result

    # ------------------------------------------------------------------ images and text

    def to_images(
        self,
        *,
        dpi: int = 150,
        fmt: ImageFormat = "png",
        pages: Iterable[int] | None = None,
        jpeg_quality: int = 85,
    ) -> list[bytes]:
        """Render pages (0-based ``pages``, default all) to PNG or JPEG bytes."""
        return image_ops.pdf_to_images(
            self.to_bytes(), dpi=dpi, fmt=fmt, pages=pages, jpeg_quality=jpeg_quality
        )

    def extract_text(self) -> list[str]:
        """Text of each page. Scanned pages have none (no OCR)."""
        return text_ops.extract_text(self.to_bytes())

    def copy(self) -> PdfDocument:
        return PdfDocument(ops.clone(self._pdf))

    # ------------------------------------------------------------------ output

    def to_bytes(self, *, compress: bool = False) -> bytes:
        """Serialize to PDF bytes.

        Uncompressed streams are always compressed. ``compress=True`` also drops
        unused page resources, recompresses existing streams at the highest level
        and packs objects into object streams, which pays off on larger documents.
        """
        buffer = io.BytesIO()
        if compress:
            pdf = ops.clone(self._pdf)
            pdf.remove_unreferenced_resources()
            pdf.save(
                buffer,
                compress_streams=True,
                recompress_flate=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
            )
        else:
            self._pdf.save(buffer)
        return buffer.getvalue()

    def save(self, path: str | PathLike[str], *, compress: bool = False) -> None:
        Path(path).write_bytes(self.to_bytes(compress=compress))

    def __repr__(self) -> str:
        return f"<PdfDocument pages={self.page_count}>"
