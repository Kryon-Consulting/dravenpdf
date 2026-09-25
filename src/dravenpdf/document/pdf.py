"""PdfDocument: an in-memory PDF.

M2 provides only what rendering needs (load, save, page count). Page operations,
stamps, images, text and compression arrive in M3/M4 (see docs/roadmap.md).
"""

from __future__ import annotations

import io
from os import PathLike
from pathlib import Path

import pikepdf

from dravenpdf.errors import InvalidPdfError


class PdfDocument:
    """A PDF held in memory, backed by :class:`pikepdf.Pdf`."""

    def __init__(self, pdf: pikepdf.Pdf) -> None:
        self._pdf = pdf

    @classmethod
    def from_bytes(cls, data: bytes) -> PdfDocument:
        try:
            return cls(pikepdf.open(io.BytesIO(data)))
        except pikepdf.PdfError as exc:
            raise InvalidPdfError(f"not a readable PDF: {exc}") from exc

    @classmethod
    def open(cls, path: str | PathLike[str]) -> PdfDocument:
        return cls.from_bytes(Path(path).read_bytes())

    @property
    def page_count(self) -> int:
        return len(self._pdf.pages)

    def to_bytes(self) -> bytes:
        buffer = io.BytesIO()
        self._pdf.save(buffer)
        return buffer.getvalue()

    def save(self, path: str | PathLike[str]) -> None:
        Path(path).write_bytes(self.to_bytes())

    def __repr__(self) -> str:
        return f"<PdfDocument pages={self.page_count}>"
