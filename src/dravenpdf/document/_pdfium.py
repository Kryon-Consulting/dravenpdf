"""Shared access to pdfium (pypdfium2).

pdfium is not thread-safe: two threads must never call into it at the same time,
even on different documents. Every pdfium call in dravenpdf goes through ``LOCK``.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pypdfium2 as pdfium

from dravenpdf.errors import InvalidPdfError

LOCK = threading.RLock()


@contextmanager
def open_pdf(data: bytes, *, hold_lock: bool = True) -> Iterator[pdfium.PdfDocument]:
    """Open ``data`` with pdfium and close it afterwards.

    By default ``LOCK`` is held throughout. With ``hold_lock=False`` only opening and
    closing take it: the caller must hold ``LOCK`` around every other pdfium call,
    including closing pages and bitmaps. Use that to do slow work that isn't pdfium
    (image encoding) without blocking other threads.
    """
    if hold_lock:
        with LOCK:
            try:
                pdf = pdfium.PdfDocument(data)
            except pdfium.PdfiumError as exc:
                raise InvalidPdfError(f"pdfium could not open the PDF: {exc}") from exc
            try:
                yield pdf
            finally:
                pdf.close()
        return
    with LOCK:
        try:
            pdf = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as exc:
            raise InvalidPdfError(f"pdfium could not open the PDF: {exc}") from exc
    try:
        yield pdf
    finally:
        with LOCK:
            pdf.close()
