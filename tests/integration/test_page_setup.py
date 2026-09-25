"""CSS page size, tagged PDFs, outlines, expression waits and prepare hooks."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pikepdf
import pytest
from PIL import Image
from playwright.async_api import Page
from typer.testing import CliRunner

from conftest import pdf_text
from dravenpdf import AsyncRenderer, PdfDocument, RenderOptions, RenderTimeoutError
from dravenpdf.cli import app

pytestmark = pytest.mark.browser

A5 = (419.53, 595.28)
FULL_BLEED = """<style>@page { size: A5 } html, body { margin: 0 }
.bleed { background: #000; height: 100vh }</style><div class="bleed"></div>"""
HEADINGS = "<h1>Chapter one</h1><h2>Section</h2><h1>Chapter two</h1>"


def corner_is_ink(doc: PdfDocument) -> bool:
    image = Image.open(io.BytesIO(doc.to_images(dpi=36)[0])).convert("L")
    return image.getpixel((1, 1)) < 50


def outline_of(doc: PdfDocument) -> list[tuple[str, list[str]]]:
    with pikepdf.open(io.BytesIO(doc.to_bytes())) as pdf, pdf.open_outline() as outline:
        return [(item.title, [c.title for c in item.children]) for item in outline.root]


def has_structure(doc: PdfDocument) -> bool:
    with pikepdf.open(io.BytesIO(doc.to_bytes())) as pdf:
        return "/StructTreeRoot" in pdf.Root


async def test_css_page_size_and_margins(renderer: AsyncRenderer) -> None:
    default = await renderer.from_html(FULL_BLEED)
    css = await renderer.from_html(
        FULL_BLEED, RenderOptions(prefer_css_page_size=True, margins=None)
    )

    assert default.page_size(0) == pytest.approx((595.28, 841.89), abs=1)  # A4 from options
    assert not corner_is_ink(default)  # 20 mm / 15 mm margins
    assert css.page_size(0) == pytest.approx(A5, abs=1)
    assert corner_is_ink(css)  # no margins sent: ink reaches the edge


async def test_tagged_and_outline(renderer: AsyncRenderer) -> None:
    plain = await renderer.from_html(HEADINGS)
    tagged = await renderer.from_html(HEADINGS, RenderOptions(tagged=True))
    outlined = await renderer.from_html(HEADINGS, RenderOptions(outline=True))

    assert not has_structure(plain)
    assert has_structure(tagged)
    assert outline_of(tagged) == []
    assert outline_of(outlined) == [("Chapter one", ["Section"]), ("Chapter two", [])]


async def test_wait_for_expression(renderer: AsyncRenderer) -> None:
    html = """<ul id="list"></ul><script>
    let n = 0;
    const timer = setInterval(() => {
      document.getElementById('list').insertAdjacentHTML('beforeend', `<li>item ${++n}</li>`);
      if (n === 3) clearInterval(timer);
    }, 400);</script>"""

    doc = await renderer.from_html(
        html, RenderOptions(wait_for_expression="document.querySelectorAll('li').length >= 3")
    )

    assert "item 3" in pdf_text(doc)[0]


async def test_prepare_hook(renderer: AsyncRenderer) -> None:
    html = """<button onclick="document.getElementById('more').hidden = false">Expand</button>
    <div id="more" hidden>EXPANDED DETAILS</div>"""

    async def expand(page: Page) -> None:
        await page.click("button")

    plain = await renderer.from_html(html)
    prepared = await renderer.from_html(html, prepare=expand)

    assert "EXPANDED DETAILS" not in pdf_text(plain)[0]
    assert "EXPANDED DETAILS" in pdf_text(prepared)[0]


async def test_prepare_hook_is_under_the_deadline(renderer: AsyncRenderer) -> None:
    async def stall(page: Page) -> None:
        await asyncio.sleep(5)

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(RenderTimeoutError):
        await renderer.from_html("<p>x</p>", RenderOptions(timeout_ms=500), prepare=stall)

    assert loop.time() - started < 2


def test_cli_page_setup_flags(tmp_path: Path) -> None:
    (tmp_path / "p.html").write_text(FULL_BLEED + HEADINGS)

    result = CliRunner().invoke(
        app,
        ["render", str(tmp_path / "p.html"), "-o", str(tmp_path / "o.pdf"),
         "--prefer-css-page-size", "--css-margins", "--outline"],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    doc = PdfDocument.open(tmp_path / "o.pdf")
    assert doc.page_size(0) == pytest.approx(A5, abs=1)
    assert outline_of(doc)[0][0] == "Chapter one"
