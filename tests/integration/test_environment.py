"""Viewport and browser-environment options, with a real Chromium."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import pdf_text
from dravenpdf import AsyncRenderer, PdfDocument, RenderOptions, Viewport
from dravenpdf.cli import app

pytestmark = pytest.mark.browser

# The page prints what it sees, so the PDF text shows the environment it rendered in.
PROBE = """<pre id="out"></pre><script>
const q = m => matchMedia(m).matches;
document.getElementById('out').textContent = [
  'width=' + innerWidth,
  'dpr=' + devicePixelRatio,
  'lang=' + navigator.language,
  'number=' + new Intl.NumberFormat().format(1234.5),
  'tz=' + Intl.DateTimeFormat().resolvedOptions().timeZone,
  'dark=' + q('(prefers-color-scheme: dark)'),
  'reduce=' + q('(prefers-reduced-motion: reduce)'),
].join(' ');
</script>"""


def seen(doc: PdfDocument) -> str:
    return " ".join(pdf_text(doc)[0].split())


async def test_defaults(renderer: AsyncRenderer) -> None:
    text = seen(await renderer.from_html(PROBE))

    assert "width=1280" in text
    assert "dpr=1" in text
    assert "dark=false" in text


async def test_environment_options(renderer: AsyncRenderer) -> None:
    options = RenderOptions(
        viewport=Viewport(width=1920, height=1080),
        device_scale_factor=2,
        locale="de-DE",
        timezone="Asia/Tokyo",
        color_scheme="dark",
        reduced_motion="reduce",
    )

    text = seen(await renderer.from_html(PROBE, options))

    for expected in ("width=1920", "dpr=2", "lang=de-DE", "number=1.234,5", "tz=Asia/Tokyo",
                     "dark=true", "reduce=true"):  # fmt: skip
        assert expected in text, text


async def test_contexts_do_not_share_settings(renderer: AsyncRenderer) -> None:
    await renderer.from_html(PROBE, RenderOptions(locale="fr-FR"))

    assert "lang=en-US" in seen(await renderer.from_html(PROBE))


def test_cli_environment_flags(tmp_path: Path) -> None:
    (tmp_path / "p.html").write_text(PROBE)

    result = CliRunner().invoke(
        app,
        ["render", str(tmp_path / "p.html"), "-o", str(tmp_path / "o.pdf"),
         "--viewport", "800x600", "--locale", "ja-JP", "--timezone", "Europe/Berlin",
         "--color-scheme", "dark"],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    text = seen(PdfDocument.open(tmp_path / "o.pdf"))
    assert "width=800" in text
    assert "lang=ja-JP" in text
    assert "tz=Europe/Berlin" in text


def test_cli_bad_viewport(tmp_path: Path) -> None:
    (tmp_path / "p.html").write_text("x")

    result = CliRunner().invoke(
        app, ["render", str(tmp_path / "p.html"), "-o", "x.pdf", "--viewport", "wide"]
    )

    assert result.exit_code == 1
    assert "1920x1080" in result.output
