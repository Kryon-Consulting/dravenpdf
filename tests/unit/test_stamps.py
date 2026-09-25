from __future__ import annotations

import io

import pikepdf
import pytest
from PIL import Image

from dravenpdf import PdfDocument, PdfOperationError
from dravenpdf.document.stamp import parse_color


def blank(*sizes: tuple[float, float]) -> PdfDocument:
    pdf = pikepdf.new()
    for size in sizes:
        pdf.add_blank_page(page_size=size)
    buffer = io.BytesIO()
    pdf.save(buffer)
    return PdfDocument.from_bytes(buffer.getvalue())


def png(color: tuple[int, int, int, int], size: tuple[int, int] = (40, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def ink_box(doc: PdfDocument, page: int = 0) -> tuple[float, float, float, float]:
    """Bounding box of non-white pixels, as fractions of the rendered page (x0, y0, x1, y1),
    with y measured from the top, as a viewer shows it."""
    image = Image.open(io.BytesIO(doc.to_images(dpi=72, pages=[page])[0])).convert("L")
    mask = image.point(lambda v: 255 if v < 200 else 0)
    box = mask.getbbox()
    assert box is not None, "nothing was drawn"
    w, h = image.size
    return box[0] / w, box[1] / h, box[2] / w, box[3] / h


def center(box: tuple[float, float, float, float]) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


# ---------------------------------------------------------------- text


def test_text_stamp_is_centered_and_extractable() -> None:
    doc = blank((400, 300)).stamp_text("CONFIDENTIAL", opacity=1, angle=0, color="#000")

    cx, cy = center(ink_box(doc))
    assert cx == pytest.approx(0.5, abs=0.03)
    assert cy == pytest.approx(0.5, abs=0.03)
    assert "CONFIDENTIAL" in doc.extract_text()[0]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize(
    ("position", "quadrant"),
    [
        ("top-left", (0, 0)),
        ("top-right", (1, 0)),
        ("bottom-left", (0, 1)),
        ("bottom-right", (1, 1)),
    ],
)
def test_text_position_is_as_displayed_on_rotated_pages(
    rotation: int, position: str, quadrant: tuple[int, int]
) -> None:
    doc = (
        blank((300, 500))
        .rotate(rotation)
        .stamp_text(
            "X",
            angle=0,
            opacity=1,
            color="#000",
            position=position,  # type: ignore[arg-type]
        )
    )

    cx, cy = center(ink_box(doc))
    assert (int(cx > 0.5), int(cy > 0.5)) == quadrant


def test_text_stamp_angle() -> None:
    flat = ink_box(blank((500, 500)).stamp_text("WATERMARK", angle=0, opacity=1))
    tilted = ink_box(blank((500, 500)).stamp_text("WATERMARK", angle=45, opacity=1))

    assert flat[3] - flat[1] < 0.15  # a flat line of text is short...
    assert tilted[3] - tilted[1] > 0.3  # ...a 45-degree one is tall


def test_only_selected_pages_are_stamped() -> None:
    doc = blank((200, 200), (200, 200), (200, 200)).stamp_text("MARK", pages=[1, -1])

    assert ["MARK" in t for t in doc.extract_text()] == [False, True, True]


def test_stamp_leaves_original_alone() -> None:
    doc = blank((200, 200))
    doc.stamp_text("MARK")

    assert doc.extract_text() == [""]


def test_opacity_uses_transparency_group() -> None:
    raw = blank((200, 200)).stamp_text("MARK", opacity=0.25).to_bytes()
    pdf = pikepdf.open(io.BytesIO(raw))
    resources = pdf.pages[0].Resources

    (gs,) = resources.ExtGState.values()
    (form,) = resources.XObject.values()
    assert float(gs.ca) == 0.25
    assert pikepdf.Name.Transparency == form.Group.S


def test_under_puts_stamp_first() -> None:
    raw = blank((200, 200)).stamp_text("MARK", under=True).to_bytes()
    pdf = pikepdf.open(io.BytesIO(raw))
    contents = pdf.pages[0].obj.Contents

    first = contents[0] if isinstance(contents, pikepdf.Array) else contents
    assert b"Do" in first.read_bytes()


def test_special_characters_are_escaped() -> None:
    doc = blank((400, 200)).stamp_text(r"(a) \ Café", angle=0)

    assert r"(a) \ Café" in doc.extract_text()[0]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"text": "機密"}, "cp1252"),
        ({"text": ""}, "empty"),
        ({"text": "x", "color": "red"}, "invalid color"),
        ({"text": "x", "position": "middle"}, "position must be"),
        ({"text": "x", "opacity": 0}, "opacity"),
        ({"text": "x", "opacity": 1.5}, "opacity"),
        ({"text": "x", "font_size": 0}, "font_size"),
        ({"text": "x", "pages": [5]}, "out of range"),
    ],
)
def test_text_stamp_errors(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(PdfOperationError, match=message):
        blank((200, 200)).stamp_text(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("color", "rgb"),
    [("#FF0000", (1.0, 0.0, 0.0)), ("#0f0", (0.0, 1.0, 0.0)), ("000080", (0.0, 0.0, 128 / 255))],
)
def test_parse_color(color: str, rgb: tuple[float, float, float]) -> None:
    assert parse_color(color) == pytest.approx(rgb)


# ---------------------------------------------------------------- images


def test_image_stamp_position_and_size() -> None:
    doc = blank((400, 400)).stamp_image(
        png((255, 0, 0, 255)), width=100, position="bottom-right", margin=20
    )

    x0, _y0, x1, y1 = ink_box(doc)
    assert (x1 - x0) * 400 == pytest.approx(100, abs=2)
    assert x1 * 400 == pytest.approx(380, abs=2)
    assert y1 * 400 == pytest.approx(380, abs=2)


def test_image_default_size_is_96_dpi() -> None:
    doc = blank((400, 400)).stamp_image(png((0, 0, 0, 255), size=(80, 40)))

    x0, y0, x1, y1 = ink_box(doc)
    assert ((x1 - x0) * 400, (y1 - y0) * 400) == pytest.approx((60, 30), abs=2)


def test_big_image_shrinks_to_fit_margins() -> None:
    doc = blank((200, 200)).stamp_image(png((0, 0, 0, 255)), width=1000, margin=10)

    x0, _, x1, _ = ink_box(doc)
    assert (x1 - x0) * 200 == pytest.approx(180, abs=2)


def test_transparent_pixels_stay_transparent() -> None:
    # Fully transparent image: nothing should show.
    doc = blank((200, 200)).stamp_image(png((255, 0, 0, 0)), width=100)

    image = Image.open(io.BytesIO(doc.to_images(dpi=72)[0])).convert("L")
    assert image.getextrema() == (255, 255)


def test_jpeg_is_embedded_as_is() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (30, 30), (0, 0, 255)).save(buffer, "JPEG")

    raw = blank((200, 200)).stamp_image(buffer.getvalue()).to_bytes()
    pdf = pikepdf.open(io.BytesIO(raw))

    (form,) = pdf.pages[0].Resources.XObject.values()
    assert form.Resources.XObject.Im0.Filter == pikepdf.Name.DCTDecode


def test_bad_image() -> None:
    with pytest.raises(PdfOperationError, match="not a supported image"):
        blank((200, 200)).stamp_image(b"not an image")


# ---------------------------------------------------------------- overlay


def test_overlay_scales_to_fit_and_centers() -> None:
    letterhead = blank((100, 100)).stamp_image(png((0, 0, 0, 255)), width=100, margin=0)

    doc = blank((400, 200)).overlay(letterhead)

    x0, y0, x1, y1 = ink_box(doc)
    assert (x0, y0, x1, y1) == pytest.approx((0.25, 0.0, 0.75, 1.0), abs=0.02)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_overlay_of_rotated_source_looks_the_same(rotation: int) -> None:
    # Overlaying a rotated page onto a same-size page must reproduce what a viewer
    # shows for the source page.
    stamp = (
        blank((200, 400)).rotate(rotation).stamp_text("X", position="top-left", angle=0, opacity=1)
    )

    doc = blank(stamp.page_size(0)).overlay(stamp)

    assert center(ink_box(doc)) == pytest.approx(center(ink_box(stamp)), abs=0.01)


def test_overlay_outlives_its_source() -> None:
    import gc

    stamp = blank((100, 100)).stamp_text("SRC", opacity=1)
    doc = blank((100, 100)).overlay(stamp.to_bytes())
    del stamp
    gc.collect()

    assert "SRC" in PdfDocument.from_bytes(doc.to_bytes()).extract_text()[0]


def test_overlay_bad_stamp_page() -> None:
    with pytest.raises(PdfOperationError, match="no page index 3"):
        blank((100, 100)).overlay(blank((100, 100)), stamp_page=3)
