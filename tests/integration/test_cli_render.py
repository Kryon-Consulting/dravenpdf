"""CLI commands that render with Chromium."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import Server, pdf_text
from dravenpdf import BlockedRequestError, IncompleteRenderError, PdfDocument
from dravenpdf.cli import app

pytestmark = pytest.mark.browser

runner = CliRunner()


def test_render_file_with_footer(tmp_path: Path) -> None:
    (tmp_path / "in.html").write_text('<h1>CLI</h1><div style="break-before:page">two</div>')
    (tmp_path / "f.html").write_text(
        '<div style="font-size:9px;margin:auto"><span class="pageNumber"></span>'
        '/<span class="totalPages"></span></div>'
    )

    result = runner.invoke(
        app,
        ["render", str(tmp_path / "in.html"), "-o", str(tmp_path / "o.pdf"), "--paper", "A5",
         "--landscape", "--footer", str(tmp_path / "f.html")],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    doc = PdfDocument.open(tmp_path / "o.pdf")
    assert "2/2" in pdf_text(doc)[1]
    assert doc.page_size(0)[0] > doc.page_size(0)[1]


def test_render_private_url_needs_flag(tmp_path: Path, server: Server) -> None:
    url = server.url("/page.html", host="127.0.0.1")

    blocked = runner.invoke(app, ["render", url, "-o", str(tmp_path / "a.pdf")])
    allowed = runner.invoke(app, ["render", url, "-o", str(tmp_path / "b.pdf"), "--allow-private"])

    assert isinstance(blocked.exception, BlockedRequestError)
    assert allowed.exit_code == 0, allowed.output
    assert "IMG-OK" in pdf_text(PdfDocument.open(tmp_path / "b.pdf"))[0]


def test_template_with_data(tmp_path: Path) -> None:
    (tmp_path / "t.html").write_text("<p>{% for n in names %}{{ n }};{% endfor %}</p>")
    (tmp_path / "d.json").write_text(json.dumps({"names": ["Ada", "Grace"]}))

    result = runner.invoke(
        app,
        ["template", str(tmp_path / "t.html"), "--data", str(tmp_path / "d.json"),
         "-o", str(tmp_path / "o.pdf")],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert "Ada;Grace;" in pdf_text(PdfDocument.open(tmp_path / "o.pdf"))[0]


def test_stamp_html(tmp_path: Path) -> None:
    (tmp_path / "in.html").write_text("<p>body</p>")
    (tmp_path / "s.html").write_text("<p style='text-align:right'>HTML STAMP</p>")
    runner.invoke(app, ["render", str(tmp_path / "in.html"), "-o", str(tmp_path / "a.pdf")])

    result = runner.invoke(
        app,
        ["stamp", str(tmp_path / "a.pdf"), "--html", str(tmp_path / "s.html"),
         "-o", str(tmp_path / "b.pdf")],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert "HTML STAMP" in pdf_text(PdfDocument.open(tmp_path / "b.pdf"))[0]


def test_render_warns_and_can_be_strict(tmp_path: Path) -> None:
    (tmp_path / "in.html").write_text('<p>body</p><img src="missing.png">')
    args = ["render", str(tmp_path / "in.html"), "-o", str(tmp_path / "o.pdf")]

    lenient = runner.invoke(app, args)
    strict = runner.invoke(app, [*args, "--fail-on-resource-errors"])

    assert lenient.exit_code == 0
    assert "warning:" in lenient.output
    assert "missing.png" in lenient.output
    assert isinstance(strict.exception, IncompleteRenderError)
