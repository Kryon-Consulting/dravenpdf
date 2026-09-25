"""PdfDocument: an in-memory PDF with a chainable, non-mutating API."""

from __future__ import annotations

import io
from collections.abc import Iterable, Sequence
from os import PathLike
from pathlib import Path

import pikepdf

from dravenpdf.document import pages as ops
from dravenpdf.errors import InvalidPdfError, PdfOperationError

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
