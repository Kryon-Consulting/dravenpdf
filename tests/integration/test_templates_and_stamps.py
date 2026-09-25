"""Templates and HTML stamps with a real Chromium."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import SVG, pdf_text
from dravenpdf import (
    AsyncRenderer,
    BlockedRequestError,
    PdfDocument,
    Renderer,
    RenderOptions,
    TemplateError,
)

pytestmark = pytest.mark.browser


@pytest.fixture
def templates(tmp_path: Path) -> Path:
    folder = tmp_path / "templates"
    folder.mkdir()
    (folder / "logo.svg").write_bytes(SVG)
    (folder / "style.css").write_text("h1 { color: rgb(0, 0, 200); }")
    (folder / "base.html").write_text(
        '<html><head><link rel="stylesheet" href="style.css"></head><body>'
        "{% block body %}{% endblock %}</body></html>"
    )
    (folder / "invoice.html").write_text(
        '{% extends "base.html" %}{% block body %}'
        "<h1>Invoice {{ number }}</h1>"
        "{% for item in items %}<p>{{ item.name }}: {{ item.price }}</p>{% endfor %}"
        '<img src="logo.svg" onload="document.body.append(\'IMG-OK\')"'
        " onerror=\"document.body.append('IMG-FAIL')\">"
        "{% endblock %}"
    )
    return folder


async def test_template_string(renderer: AsyncRenderer) -> None:
    doc = await renderer.from_template(
        "<h1>Hello {{ name }}</h1>", {"name": "<b>Ada</b>"}, RenderOptions(paper="Letter")
    )

    text = pdf_text(doc)[0]
    assert "Hello <b>Ada</b>" in text  # escaped, so shown literally


async def test_template_folder_with_assets(renderer: AsyncRenderer, templates: Path) -> None:
    items = [{"name": "Widget", "price": "10.00"}, {"name": "Gadget", "price": "5.50"}]

    doc = await renderer.from_template(
        "invoice.html", {"number": 42, "items": items}, template_dir=templates
    )

    text = pdf_text(doc)[0]
    assert "Invoice 42" in text
    assert "Gadget: 5.50" in text
    assert "IMG-OK" in text


async def test_template_folder_cannot_load_outside(
    renderer: AsyncRenderer, templates: Path
) -> None:
    (templates.parent / "secret.svg").write_bytes(SVG)
    (templates / "leak.html").write_text('<img src="../secret.svg">')

    with pytest.raises(BlockedRequestError, match="outside"):
        await renderer.from_template("leak.html", {}, template_dir=templates)


async def test_template_error(renderer: AsyncRenderer) -> None:
    with pytest.raises(TemplateError):
        await renderer.from_template("{% for %}", {})


async def test_html_stamp_on_mixed_page_sizes(renderer: AsyncRenderer) -> None:
    a4 = await renderer.from_html("<p>body on A4</p>")
    letter = await renderer.from_html(
        "<p>body on Letter</p>", RenderOptions(paper="Letter", landscape=True)
    )
    doc = PdfDocument.merge([a4, letter])

    stamped = await doc.stamp_html(
        renderer,
        '<div style="position:absolute;top:10px;right:10px;font-size:20px">HTML STAMP</div>',
        opacity=0.5,
    )

    texts = pdf_text(stamped)
    assert "body on A4" in texts[0]
    assert "HTML STAMP" in texts[0]
    assert "body on Letter" in texts[1]
    assert "HTML STAMP" in texts[1]
    assert stamped.page_size(1) == pytest.approx(doc.page_size(1))


async def test_html_stamp_is_transparent(renderer: AsyncRenderer) -> None:
    # The stamp only draws a small box, so the page's own black text must stay visible.
    import io

    from PIL import Image

    doc = await renderer.from_html('<div style="background:#000;width:100%;height:200px"></div>')
    stamped = await doc.stamp_html(renderer, '<p style="margin:0">small</p>')

    image = Image.open(io.BytesIO(stamped.to_images(dpi=36)[0])).convert("L")
    assert image.getpixel((image.width // 2, 40)) < 50  # still black under the stamp


def test_sync_template(templates: Path) -> None:
    with Renderer() as r:
        doc = r.from_template("invoice.html", {"number": 7, "items": []}, template_dir=templates)

    assert "Invoice 7" in pdf_text(doc)[0]
