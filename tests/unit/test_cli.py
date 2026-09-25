"""CLI commands that don't need a browser."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pikepdf
import pytest
from PIL import Image
from typer.testing import CliRunner

from dravenpdf import PdfDocument, PdfOperationError
from dravenpdf.cli import app

runner = CliRunner()


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    doc = pikepdf.new()
    for i in range(4):
        doc.add_blank_page(page_size=(100 + i, 200))
    path = tmp_path / "in.pdf"
    doc.save(path)
    return path


def widths(path: Path) -> list[int]:
    doc = PdfDocument.open(path)
    return [int(doc.page_size(i)[0]) for i in range(doc.page_count)]


def run(*args: str | Path) -> str:
    result = runner.invoke(app, [str(a) for a in args])
    assert result.exit_code == 0, result.output
    return result.output


def test_version() -> None:
    assert run("--version").startswith("dravenpdf ")


def test_merge_extract_delete_rotate(pdf: Path, tmp_path: Path) -> None:
    run("merge", pdf, pdf, "-o", tmp_path / "m.pdf")
    run("extract", tmp_path / "m.pdf", "2,8", "-o", tmp_path / "e.pdf")
    run("delete", pdf, "1-2", "-o", tmp_path / "d.pdf")
    run("rotate", pdf, "--pages", "1", "--degrees", "90", "-o", tmp_path / "r.pdf")

    assert len(widths(tmp_path / "m.pdf")) == 8
    assert widths(tmp_path / "e.pdf") == [101, 103]
    assert widths(tmp_path / "d.pdf") == [102, 103]
    assert widths(tmp_path / "r.pdf") == [200, 101, 102, 103]


def test_split(pdf: Path, tmp_path: Path) -> None:
    run("split", pdf, "--range", "1", "--range", "2-", "-o", tmp_path / "parts")

    assert sorted(p.name for p in (tmp_path / "parts").iterdir()) == ["part-1.pdf", "part-2.pdf"]
    assert widths(tmp_path / "parts" / "part-2.pdf") == [101, 102, 103]


def test_stamp_metadata_text(pdf: Path, tmp_path: Path) -> None:
    run("stamp", pdf, "--text", "DRAFT", "--pages", "2", "-o", tmp_path / "s.pdf")
    run("metadata", tmp_path / "s.pdf", "--title", "Hello", "-o", tmp_path / "t.pdf")

    info = json.loads(run("metadata", tmp_path / "t.pdf"))
    pages = run("text", tmp_path / "t.pdf").split("\f")
    assert info["pages"] == 4
    assert info["title"] == "Hello"
    assert "DRAFT" in pages[1]
    assert "DRAFT" not in pages[0]


def test_images_round_trip(pdf: Path, tmp_path: Path) -> None:
    run("images", pdf, "--dpi", "36", "--format", "jpeg", "--pages", "3-", "-o", tmp_path / "img")
    names = sorted(p.name for p in (tmp_path / "img").iterdir())
    run("from-images", *(tmp_path / "img" / n for n in names), "-o", tmp_path / "back.pdf")

    assert names == ["page-3.jpg", "page-4.jpg"]
    assert PdfDocument.open(tmp_path / "back.pdf").page_count == 2


def test_compress_to_stdout(pdf: Path) -> None:
    result = runner.invoke(app, ["compress", str(pdf), "-o", "-"])

    assert result.exit_code == 0
    assert result.stdout_bytes.startswith(b"%PDF-")


def test_stamp_needs_exactly_one_source(pdf: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["stamp", str(pdf), "-o", str(tmp_path / "x.pdf")])

    assert result.exit_code == 1
    assert "exactly one" in result.output


def test_library_errors_propagate_to_main(pdf: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["extract", str(pdf), "9", "-o", str(tmp_path / "x.pdf")])

    assert isinstance(result.exception, PdfOperationError)


def test_main_prints_one_line_errors(pdf: Path, tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "dravenpdf", "extract", str(pdf), "9", "-o", str(tmp_path / "x")],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stderr.strip() == "error: page range '9' is past the last page (4)"


def test_from_images_on_paper(tmp_path: Path) -> None:
    image = tmp_path / "a.png"
    Image.new("RGB", (50, 50), "blue").save(image)

    run("from-images", image, "--paper", "Letter", "--margin", "36", "-o", tmp_path / "o.pdf")

    assert PdfDocument.open(tmp_path / "o.pdf").page_size(0) == pytest.approx((612, 792))


def test_bad_json_data(tmp_path: Path) -> None:
    (tmp_path / "t.html").write_text("x")
    (tmp_path / "d.json").write_text("[1, 2]")

    result = runner.invoke(
        app, ["template", str(tmp_path / "t.html"), "--data", str(tmp_path / "d.json"), "-o", "x"]
    )

    assert result.exit_code == 1
    assert "JSON object" in result.output


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("s3cret not json", "not a JSON file"),
        ('{"headers": {"https://a.example/path": {"X": "s3cret"}}}', "origin"),
        ('{"cookies": [{"name": "sid", "value": "s3cret"}]}', "url or domain"),
        ('{"origins": [{"origin": "https://a.example", "indexedDB": "s3cret"}]}', "indexedDB"),
    ],
)
def test_render_rejects_bad_auth_file_without_echoing_it(
    tmp_path: Path, content: str, message: str
) -> None:
    (tmp_path / "auth.json").write_text(content)
    (tmp_path / "in.html").write_text("<p>x</p>")

    result = CliRunner().invoke(
        app,
        ["render", str(tmp_path / "in.html"), "-o", str(tmp_path / "out.pdf"),
         "--auth", str(tmp_path / "auth.json")],
    )  # fmt: skip

    assert result.exit_code == 1
    assert message in result.output
    assert "s3cret" not in result.output
    assert not (tmp_path / "out.pdf").exists()
