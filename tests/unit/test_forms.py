from __future__ import annotations

import io
from pathlib import Path

import pikepdf
import pytest
from PIL import Image

from conftest import pdf_text
from dravenpdf import FormField, PdfDocument, PdfOperationError
from forms_fixture import build_form

FILL = {
    "name": "Ada Lovelace",
    "notes": "line one\nline two",
    "code": "1234",
    "agree": True,
    "size": "M",
    "country": "Japan",
}


@pytest.fixture
def form() -> PdfDocument:
    return PdfDocument.from_bytes(build_form())


def fields_by_name(doc: PdfDocument) -> dict[str, FormField]:
    return {f.name: f for f in doc.form_fields()}


def ink(doc: PdfDocument, box: tuple[int, int, int, int]) -> int:
    """Darkest pixel in a box given in PDF points (page is 400 x 400), 0 = black."""
    image = Image.open(io.BytesIO(doc.to_images(dpi=72)[0])).convert("L")
    x0, y0, x1, y1 = box
    return min(image.getpixel((x, 400 - y)) for x in range(x0, x1) for y in range(y0, y1))


def test_list_fields(form: PdfDocument) -> None:
    fields = fields_by_name(form)

    assert set(fields) == {"name", "notes", "code", "id", "agree", "size", "country"}
    assert fields["notes"].multiline
    assert fields["code"].max_length == 4
    assert fields["id"].read_only
    assert fields["agree"] == FormField("agree", "checkbox", False)
    assert fields["size"].kind == "radio"
    assert set(fields["size"].options) == {"S", "M", "L"}
    assert fields["size"].value is None
    assert fields["country"].options == ("Germany", "France", "Japan")


def test_no_form() -> None:
    doc = PdfDocument.from_bytes(PdfDocument.from_images([_png()]).to_bytes())

    assert doc.form_fields() == []
    with pytest.raises(PdfOperationError, match="no form"):
        doc.fill_form({"x": "y"})


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(buffer, "PNG")
    return buffer.getvalue()


def test_fill_and_read_back(form: PdfDocument) -> None:
    filled = form.fill_form(FILL)

    fields = fields_by_name(PdfDocument.from_bytes(filled.to_bytes()))
    assert fields["name"].value == "Ada Lovelace"
    assert fields["notes"].value == "line one\nline two"
    assert fields["agree"].value is True
    assert fields["size"].value == "M"
    assert fields["country"].value == "Japan"
    assert fields_by_name(form)["name"].value == ""  # the original is unchanged


def test_flatten_burns_values_into_the_page(form: PdfDocument) -> None:
    flat = form.fill_form(FILL, flatten=True)

    text = pdf_text(flat)[0]
    for value in ("Ada Lovelace", "line one", "line two", "1234", "Japan"):
        assert value in text
    assert flat.form_fields() == []  # no form left to edit
    with pikepdf.open(io.BytesIO(flat.to_bytes())) as pdf:
        assert "/Annots" not in pdf.pages[0].obj  # no dead widgets left behind
    assert ink(flat, (22, 222, 33, 233)) < 50  # checkbox drawn checked
    assert ink(flat, (52, 192, 63, 203)) < 50  # radio "M" drawn selected
    assert ink(flat, (22, 192, 33, 203)) > 200  # radio "S" not


def test_flatten_form_on_its_own(form: PdfDocument) -> None:
    flat = form.fill_form({"name": "Grace"}).flatten_form()

    assert flat.form_fields() == []
    assert "Grace" in pdf_text(flat)[0]


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"nope": "x"}, "no such form field"),
        ({"id": "x"}, "read-only"),
        ({"code": "12345"}, "at most 4 characters"),
        ({"name": "a\nb"}, "single-line"),
        ({"agree": "yes"}, "checkbox"),
        ({"size": "XL"}, "needs one of its options"),
        ({"size": None}, "needs one of its options"),
        ({"country": "Spain"}, "no option 'Spain'"),
        ({"name": 42}, "takes text"),
    ],
)
def test_fill_errors(form: PdfDocument, values: dict[str, object], message: str) -> None:
    with pytest.raises(PdfOperationError, match=message):
        form.fill_form(values)  # type: ignore[arg-type]


def test_fill_is_all_or_nothing(form: PdfDocument) -> None:
    with pytest.raises(PdfOperationError):
        form.fill_form({"name": "Ada", "size": "XL"})

    assert fields_by_name(form)["name"].value == ""


def test_non_latin_text_is_left_to_the_viewer(form: PdfDocument) -> None:
    filled = form.fill_form({"name": "山田 太郎"})

    reopened = PdfDocument.from_bytes(filled.to_bytes())
    assert fields_by_name(reopened)["name"].value == "山田 太郎"
    with pikepdf.open(io.BytesIO(filled.to_bytes())) as pdf:
        assert bool(pdf.Root.AcroForm.NeedAppearances)
    with pytest.raises(PdfOperationError, match="can't flatten non-Western"):
        form.fill_form({"name": "山田 太郎"}, flatten=True)
    with pytest.raises(PdfOperationError, match="viewer to draw"):
        filled.flatten_form()


def test_western_european_text_flattens(form: PdfDocument) -> None:
    flat = form.fill_form({"name": "Café Müller \u2013 Straße"}, flatten=True)

    assert "Café Müller \u2013 Straße" in pdf_text(flat)[0]


def test_hybrid_xfa_form_is_filled_through_acroform() -> None:
    doc = PdfDocument.from_bytes(build_form(xfa=True))

    filled = doc.fill_form({"name": "Ada"})

    with pikepdf.open(io.BytesIO(filled.to_bytes())) as pdf:
        assert "/XFA" not in pdf.Root.AcroForm
    assert fields_by_name(filled)["name"].value == "Ada"


@pytest.mark.filterwarnings("ignore:.*not reachable from /AcroForm")  # fixture's own widgets
def test_xfa_only_form_is_refused() -> None:
    doc = PdfDocument.from_bytes(build_form(xfa_only=True))

    with pytest.raises(PdfOperationError, match="XFA-only"):
        doc.form_fields()


def test_filled_form_keeps_pending_encryption(form: PdfDocument) -> None:
    filled = form.encrypt(user_password="pw").fill_form({"name": "Ada"})

    assert filled.is_encrypted


def test_page_operations_keep_form_fields(form: PdfDocument) -> None:
    filled = form.fill_form({"name": "Ada"})

    for doc in (filled.extract("1"), filled.reorder([0]), filled.split(every=1)[0]):
        fields = fields_by_name(doc)
        assert fields["name"].value == "Ada"
        assert "size" in fields
    merged = PdfDocument.merge([filled, form])
    assert len(merged.form_fields()) == 2 * len(form.form_fields())  # second copy renamed


def test_cli_form_commands(tmp_path: Path) -> None:
    import json

    from typer.testing import CliRunner

    from dravenpdf.cli import app

    (tmp_path / "form.pdf").write_bytes(build_form())
    (tmp_path / "values.json").write_text(json.dumps({"name": "Ada", "agree": True}))
    runner = CliRunner()

    listed = runner.invoke(app, ["form-fields", str(tmp_path / "form.pdf")])
    filled = runner.invoke(
        app,
        ["fill-form", str(tmp_path / "form.pdf"), "--data", str(tmp_path / "values.json"),
         "--flatten", "-o", str(tmp_path / "out.pdf")],
    )  # fmt: skip

    assert listed.exit_code == 0
    assert {f["name"] for f in json.loads(listed.stdout)} >= {"name", "agree", "size"}
    assert filled.exit_code == 0, filled.output
    assert "Ada" in pdf_text(PdfDocument.open(tmp_path / "out.pdf"))[0]
