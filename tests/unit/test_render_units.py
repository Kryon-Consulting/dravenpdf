"""Rendering pieces that don't need a browser."""

from __future__ import annotations

import pytest

from dravenpdf import BrowserPool, InvalidPdfError, PdfDocument, Renderer
from dravenpdf.render.renderer import inject_base_url


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<html><head><title>x</title></head></html>", '<head><base href="https://a.example/">'),
        ('<HEAD lang="en"><title>x</title>', '<HEAD lang="en"><base href="https://a.example/">'),
        ("<p>no head</p>", '<base href="https://a.example/"><p>no head</p>'),
    ],
)
def test_inject_base_url(html: str, expected: str) -> None:
    assert expected in inject_base_url(html, "https://a.example/")


def test_inject_base_url_escapes() -> None:
    out = inject_base_url("<p>x</p>", 'https://a.example/"><script>')
    assert "<script>" not in out
    assert "&quot;" in out


def test_head_like_tags_are_not_mistaken_for_head() -> None:
    out = inject_base_url("<header>x</header>", "https://a.example/")
    assert out.startswith('<base href="https://a.example/"><header>')


@pytest.mark.parametrize(
    "kwargs",
    [{"max_concurrency": 0}, {"max_queue": -1}, {"recycle_after": 0}],
)
def test_pool_rejects_bad_settings(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="must"):
        BrowserPool(**kwargs)


def test_sync_renderer_must_be_started() -> None:
    with pytest.raises(RuntimeError, match="not started"):
        Renderer().from_html("<p>x</p>")


def test_pdf_document_rejects_garbage() -> None:
    with pytest.raises(InvalidPdfError):
        PdfDocument.from_bytes(b"definitely not a pdf")
