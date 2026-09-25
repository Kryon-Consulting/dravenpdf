"""PdfDocument operations on real Chromium output."""

from __future__ import annotations

import pytest

from conftest import pdf_text
from dravenpdf import AsyncRenderer, PdfDocument

pytestmark = pytest.mark.browser

THREE_PAGES = "".join(
    f'<div style="break-after: page">Page {name}</div>' for name in ("ONE", "TWO", "THREE")
)


async def test_page_operations_keep_content(renderer: AsyncRenderer) -> None:
    doc = await renderer.from_html(THREE_PAGES)
    assert doc.page_count == 3

    reordered = doc.reorder([2, 0, 1]).extract("1-2").set_metadata(title="Ops")

    text = pdf_text(reordered)
    assert "THREE" in text[0]
    assert "ONE" in text[1]
    assert reordered.metadata["title"] == "Ops"


async def test_merge_rendered_documents(renderer: AsyncRenderer) -> None:
    first = await renderer.from_html("<p>first document</p>")
    second = await renderer.from_html("<p>second document</p>")

    merged = PdfDocument.merge([first, second])

    assert [t.strip() for t in pdf_text(merged)] == ["first document", "second document"]


async def test_compress_rendered_pdf(renderer: AsyncRenderer) -> None:
    rows = "".join(f"<tr><td>row {i}</td><td>{i * 7}</td></tr>" for i in range(400))
    doc = await renderer.from_html(f"<table>{rows}</table>")

    packed = doc.to_bytes(compress=True)

    assert len(packed) < len(doc.to_bytes())
    assert "row 399" in "".join(pdf_text(PdfDocument.from_bytes(packed)))
