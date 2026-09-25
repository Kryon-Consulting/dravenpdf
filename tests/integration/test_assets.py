"""HTML plus in-memory assets, with a real Chromium."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import SVG, Server, pdf_text
from dravenpdf import AssetError, AsyncRenderer, BlockedRequestError, Renderer
from dravenpdf.render.assets import ORIGIN

pytestmark = pytest.mark.browser

HTML = """<!doctype html><html><head>
<link rel="stylesheet" href="css/site.css"><script src="js/app.js"></script>
</head><body><h1>Bundle</h1>
<img src="img/logo.svg" onload="document.body.append('IMG-OK')"
     onerror="document.body.append('IMG-FAIL')">
</body></html>"""

ASSETS = {
    # @import must come first; more.css resolves relative to css/site.css.
    "css/site.css": b'@import url("more.css"); h1::after { content: " CSS-OK"; }',
    "css/more.css": b'body::after { content: "NESTED-CSS-OK"; }',
    "js/app.js": b"document.addEventListener('DOMContentLoaded',"
    b" () => document.body.append('JS-OK'));",
    "img/logo.svg": SVG,
}


async def test_assets_load_from_memory(renderer: AsyncRenderer) -> None:
    doc = await renderer.from_html(HTML, assets=ASSETS)

    text = pdf_text(doc)[0]
    for marker in ("CSS-OK", "NESTED-CSS-OK", "JS-OK", "IMG-OK"):
        assert marker in text
    assert doc.render_report is not None
    assert doc.render_report.ok


async def test_missing_asset_is_a_404_in_the_report(renderer: AsyncRenderer) -> None:
    doc = await renderer.from_html(HTML, assets={"css/site.css": b""})

    report = doc.render_report
    assert report is not None
    missing = sorted(e.url.removeprefix(ORIGIN) for e in report.http_errors)
    assert missing == ["/img/logo.svg", "/js/app.js"]
    assert {e.status for e in report.http_errors} == {404}


async def test_bundle_pages_are_still_guarded(renderer: AsyncRenderer, server: Server) -> None:
    html = f'<img src="{server.url("/img.svg", host="127.0.0.1")}">'

    with pytest.raises(BlockedRequestError):
        await renderer.from_html(html, assets={})


async def test_bundle_works_when_private_network_is_allowed() -> None:
    # With allow_private_network the guard normally doesn't intercept at all; a
    # bundle still needs its origin answered from memory.
    async with AsyncRenderer(allow_private_network=True) as r:
        doc = await r.from_html(HTML, assets=ASSETS)

    assert "IMG-OK" in pdf_text(doc)[0]


async def test_template_with_assets(renderer: AsyncRenderer) -> None:
    doc = await renderer.from_template(
        '<h1>{{ title }}</h1><img src="logo.svg" onload="document.body.append(\'IMG-OK\')">',
        {"title": "Templated"},
        assets={"logo.svg": SVG},
    )

    text = pdf_text(doc)[0]
    assert "Templated" in text
    assert "IMG-OK" in text


async def test_conflicting_arguments(renderer: AsyncRenderer, tmp_path: Path) -> None:
    with pytest.raises(AssetError, match="base_url or assets"):
        await renderer.from_html("x", base_url="https://a.example/", assets={})
    with pytest.raises(AssetError, match="template_dir or assets"):
        await renderer.from_template("x", {}, template_dir=tmp_path, assets={})


def test_sync_renderer_assets() -> None:
    with Renderer() as r:
        doc = r.from_html(HTML, assets=ASSETS)

    assert "CSS-OK" in pdf_text(doc)[0]
