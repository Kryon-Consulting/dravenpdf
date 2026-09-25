from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image

from dravenpdf import PdfDocument, PdfOperationError
from dravenpdf.document.images import PAPER_SIZES_PT


def image_bytes(fmt: str, size: tuple[int, int] = (80, 40), mode: str = "RGB") -> bytes:
    buffer = io.BytesIO()
    color = (255, 0, 0, 128) if mode == "RGBA" else (255, 0, 0)
    Image.new(mode, size, color).save(buffer, fmt)
    return buffer.getvalue()


# ---------------------------------------------------------------- images -> PDF


def test_from_images_uses_image_size() -> None:
    doc = PdfDocument.from_images(
        [image_bytes("PNG", mode="RGBA"), image_bytes("JPEG", size=(96, 192))]
    )

    assert doc.page_count == 2
    assert doc.page_size(0) == pytest.approx((60, 30), abs=0.5)
    assert doc.page_size(1) == pytest.approx((72, 144), abs=0.5)


@pytest.mark.parametrize(("landscape", "swap"), [(False, False), (True, True)])
def test_from_images_on_paper(landscape: bool, swap: bool) -> None:
    doc = PdfDocument.from_images([image_bytes("PNG")], paper="A4", landscape=landscape, margin=36)

    w, h = PAPER_SIZES_PT["A4"]
    assert doc.page_size(0) == pytest.approx((h, w) if swap else (w, h), abs=0.5)


@pytest.mark.parametrize(
    ("images", "message"), [([], "no images"), ([b"garbage"], "could not convert")]
)
def test_from_images_errors(images: list[bytes], message: str) -> None:
    with pytest.raises(PdfOperationError, match=message):
        PdfDocument.from_images(images)


# ---------------------------------------------------------------- PDF -> images


@pytest.fixture
def two_pages() -> PdfDocument:
    return PdfDocument.from_images([image_bytes("PNG", size=(96, 96))] * 2)  # 72 x 72 pt


def test_to_images_png(two_pages: PdfDocument) -> None:
    images = two_pages.to_images(dpi=144)

    assert len(images) == 2
    assert images[0].startswith(b"\x89PNG")
    assert Image.open(io.BytesIO(images[0])).size == (144, 144)


def test_to_images_jpeg_subset(two_pages: PdfDocument) -> None:
    images = two_pages.to_images(fmt="jpeg", pages=[-1])

    assert len(images) == 1
    assert images[0].startswith(b"\xff\xd8")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"dpi": 5}, "dpi"),
        ({"dpi": 1000}, "dpi"),
        ({"fmt": "gif"}, "fmt"),
        ({"pages": [2]}, "out of range"),
        ({"jpeg_quality": 0}, "jpeg_quality"),
    ],
)
def test_to_images_errors(two_pages: PdfDocument, kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(PdfOperationError, match=message):
        two_pages.to_images(**kwargs)  # type: ignore[arg-type]


def test_pdfium_calls_from_many_threads(two_pages: PdfDocument) -> None:
    data = two_pages.to_bytes()

    def work(_: int) -> int:
        doc = PdfDocument.from_bytes(data)
        return len(doc.to_images(dpi=36)) + len(doc.extract_text())

    with ThreadPoolExecutor(8) as pool:
        assert list(pool.map(work, range(32))) == [4] * 32


# ---------------------------------------------------------------- text


def test_extract_text_per_page() -> None:
    doc = PdfDocument.merge(
        [
            PdfDocument.from_images([image_bytes("PNG", size=(400, 200))]).stamp_text(t, angle=0)
            for t in ("first page", "second page")
        ]
    )

    texts = doc.extract_text()

    assert [t.strip() for t in texts] == ["first page", "second page"]


def test_image_only_page_has_no_text(two_pages: PdfDocument) -> None:
    assert two_pages.extract_text() == ["", ""]
