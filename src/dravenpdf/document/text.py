"""Text extraction (pdfium)."""

from __future__ import annotations

from dravenpdf.document._pdfium import open_pdf


def extract_text(pdf: bytes) -> list[str]:
    """The text of each page, in reading order as pdfium sees it.

    Scanned pages (images of text) have no text; OCR is out of scope.
    """
    results = []
    with open_pdf(pdf) as doc:
        for page in doc:
            try:
                textpage = page.get_textpage()
                try:
                    text = textpage.get_text_range()
                finally:
                    textpage.close()
            finally:
                page.close()
            results.append(text.replace("\r\n", "\n").replace("\r", "\n"))
    return results
