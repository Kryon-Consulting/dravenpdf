from __future__ import annotations

import gc
import io
from pathlib import Path

import pikepdf
import pytest

from dravenpdf import InvalidPdfError, PdfDocument, PdfOperationError
from dravenpdf.document.pages import parse_page_ranges


def make_pdf(pages: int, *, title: str | None = "Sample") -> PdfDocument:
    """Page i is (100 + i) points wide, so tests can tell pages apart by width."""
    pdf = pikepdf.new()
    for i in range(pages):
        pdf.add_blank_page(page_size=(100 + i, 200))
    if title is not None:
        pdf.docinfo["/Title"] = title
        pdf.docinfo["/Author"] = "Kryon"
    buffer = io.BytesIO()
    pdf.save(buffer)
    return PdfDocument.from_bytes(buffer.getvalue())


def widths(doc: PdfDocument) -> list[int]:
    return [int(doc.page_size(i)[0]) for i in range(doc.page_count)]


# ---------------------------------------------------------------- loading and saving


def test_round_trip_through_file(tmp_path: Path) -> None:
    path = tmp_path / "a.pdf"
    make_pdf(3).save(path)

    doc = PdfDocument.open(path)

    assert len(doc) == 3
    assert widths(doc) == [100, 101, 102]


def test_password_protected_pdf_is_rejected() -> None:
    pdf = pikepdf.new()
    pdf.add_blank_page()
    buffer = io.BytesIO()
    pdf.save(buffer, encryption=pikepdf.Encryption(owner="o", user="u"))

    with pytest.raises(InvalidPdfError, match="password"):
        PdfDocument.from_bytes(buffer.getvalue())


def test_metadata() -> None:
    assert make_pdf(1).metadata == {"title": "Sample", "author": "Kryon"}
    assert make_pdf(1, title=None).metadata == {}


# ---------------------------------------------------------------- page ranges


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("1", [0]),
        ("2-4", [1, 2, 3]),
        ("1-2, 5", [0, 1, 4]),
        ("4-", [3, 4]),
        ("5,1", [4, 0]),
        (" 1 - 2 ", [0, 1]),
        ("1-1", [0]),
    ],
)
def test_parse_page_ranges(spec: str, expected: list[int]) -> None:
    assert parse_page_ranges(spec, 5) == expected


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("", "empty"),
        ("0", "pages start at 1"),
        ("3-2", "pages start at 1"),
        ("6", "past the last page"),
        ("2-9", "past the last page"),
        ("a", "invalid page range"),
        ("1,,2", "invalid page range"),
        ("-3", "invalid page range"),
    ],
)
def test_parse_page_ranges_errors(spec: str, message: str) -> None:
    with pytest.raises(PdfOperationError, match=message):
        parse_page_ranges(spec, 5)


# ---------------------------------------------------------------- operations


def test_extract_keeps_metadata_and_leaves_original_alone() -> None:
    doc = make_pdf(5)

    part = doc.extract("2-3,5")

    assert widths(part) == [101, 102, 104]
    assert part.metadata["title"] == "Sample"
    assert widths(doc) == [100, 101, 102, 103, 104]


def test_split_every() -> None:
    parts = make_pdf(5).split(every=2)

    assert [widths(p) for p in parts] == [[100, 101], [102, 103], [104]]


def test_split_ranges() -> None:
    parts = make_pdf(5).split(ranges=["1-2", "3-"])

    assert [widths(p) for p in parts] == [[100, 101], [102, 103, 104]]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "exactly one"),
        ({"every": 1, "ranges": ["1"]}, "exactly one"),
        ({"every": 0}, "at least 1"),
        ({"ranges": []}, "no ranges"),
    ],
)
def test_split_errors(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(PdfOperationError, match=message):
        make_pdf(3).split(**kwargs)  # type: ignore[arg-type]


def test_rotate_selected_pages() -> None:
    doc = make_pdf(2)

    rotated = doc.rotate(90, pages=[0])

    assert rotated.page_size(0) == (200, 100)
    assert rotated.page_size(1) == (101, 200)
    assert doc.page_size(0) == (100, 200)


def test_rotation_accumulates_and_normalizes() -> None:
    doc = make_pdf(1).rotate(90).rotate(-90).rotate(270)

    assert doc.page_size(0) == (200, 100)
    assert doc._pdf.pages[0].rotation == 270


def test_rotate_rejects_odd_angles() -> None:
    with pytest.raises(PdfOperationError, match="multiple of 90"):
        make_pdf(1).rotate(45)


def test_delete_with_negative_index() -> None:
    assert widths(make_pdf(4).delete([0, -1])) == [101, 102]


@pytest.mark.parametrize(
    ("pages", "message"),
    [([0, 1, 2], "every page"), ([3], "out of range"), ([-4], "out of range")],
)
def test_delete_errors(pages: list[int], message: str) -> None:
    with pytest.raises(PdfOperationError, match=message):
        make_pdf(3).delete(pages)


def test_reorder() -> None:
    assert widths(make_pdf(3).reorder([2, 0, 1])) == [102, 100, 101]


@pytest.mark.parametrize("order", [[0, 1], [0, 0, 1], [0, 1, 2, 3]])
def test_reorder_must_name_every_page_once(order: list[int]) -> None:
    with pytest.raises(PdfOperationError):
        make_pdf(3).reorder(order)


def test_insert() -> None:
    base, extra = make_pdf(3), make_pdf(2)

    assert widths(base.insert(extra, at=1)) == [100, 100, 101, 101, 102]
    assert widths(base.insert(extra.to_bytes(), at=3)) == [100, 101, 102, 100, 101]
    with pytest.raises(PdfOperationError, match="out of range"):
        base.insert(extra, at=4)


def test_merge_documents_and_bytes() -> None:
    first = make_pdf(2, title="First")
    second = make_pdf(1, title="Second")

    merged = PdfDocument.merge([first, second.to_bytes(), first])

    assert widths(merged) == [100, 101, 100, 100, 101]
    assert merged.metadata["title"] == "First"


def test_merge_needs_something() -> None:
    with pytest.raises(PdfOperationError, match="nothing to merge"):
        PdfDocument.merge([])


def test_results_outlive_their_sources() -> None:
    sources = [make_pdf(2), make_pdf(3)]
    merged = PdfDocument.merge(sources)
    extracted = sources[1].extract("2-3")
    inserted = sources[0].insert(sources[1], at=1)
    del sources
    gc.collect()

    for doc, expected in ((merged, 5), (extracted, 2), (inserted, 5)):
        reloaded = PdfDocument.from_bytes(doc.to_bytes())
        assert reloaded.page_count == expected


def test_chaining() -> None:
    doc = make_pdf(4).delete([0]).reorder([2, 1, 0]).rotate(180, pages=[0])

    assert widths(doc) == [103, 102, 101]


# ---------------------------------------------------------------- metadata


def test_set_metadata_sets_removes_and_leaves() -> None:
    doc = make_pdf(1).set_metadata(title="Q3 Report", author="", subject="Sales")

    assert doc.metadata == {"title": "Q3 Report", "subject": "Sales"}


def test_set_metadata_keeps_xmp_in_step() -> None:
    pdf = pikepdf.new()
    pdf.add_blank_page()
    with pdf.open_metadata() as xmp:
        xmp["dc:title"] = "Old title"
    buffer = io.BytesIO()
    pdf.save(buffer)

    doc = PdfDocument.from_bytes(buffer.getvalue()).set_metadata(title="New title")

    reopened = pikepdf.open(io.BytesIO(doc.to_bytes()))
    assert reopened.open_metadata()["dc:title"] == "New title"


# ---------------------------------------------------------------- compression


def test_compress_shrinks_uncompressed_content() -> None:
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.pages[0].obj.Contents = pdf.make_stream(b"0 0 m 10 10 l S\n" * 5000)
    buffer = io.BytesIO()
    pdf.save(buffer, compress_streams=False)
    raw = buffer.getvalue()
    doc = PdfDocument.from_bytes(raw)

    packed = doc.to_bytes(compress=True)

    assert len(raw) > 80_000
    assert len(packed) < len(raw) / 10
    assert PdfDocument.from_bytes(packed).page_count == 1
