from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image

from dravenpdf import LimitExceededError, PdfDocument, PdfOperationError
from dravenpdf.document import _pdfium
from dravenpdf.document import images as image_ops
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


def test_to_images_pixel_limit_checked_before_rendering(two_pages: PdfDocument) -> None:
    # 72 x 72 pt at 144 dpi is 144 x 144 = 20,736 pixels.
    assert len(two_pages.to_images(dpi=144, max_pixels=144 * 144)) == 2
    with pytest.raises(LimitExceededError, match="page 1 would be 20,736 pixels"):
        two_pages.to_images(dpi=144, max_pixels=144 * 144 - 1)


def test_to_images_output_limit(two_pages: PdfDocument) -> None:
    one = len(two_pages.to_images(pages=[0])[0])

    assert len(two_pages.to_images(max_total_bytes=2 * one)) == 2
    with pytest.raises(LimitExceededError, match="larger than"):
        two_pages.to_images(max_total_bytes=2 * one - 1)


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


def pdf_pages(count: int) -> bytes:
    pdf = pikepdf.new()
    for _ in range(count):
        pdf.add_blank_page(page_size=(200, 300))
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def test_encoding_runs_outside_the_pdfium_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    free: list[bool] = []
    real = image_ops._encode

    def spy(image: Image.Image, fmt: str, quality: int) -> bytes:
        def probe() -> None:
            got = _pdfium.LOCK.acquire(timeout=1)
            if got:
                _pdfium.LOCK.release()
            free.append(got)

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        return real(image, fmt, quality)

    monkeypatch.setattr(image_ops, "_encode", spy)
    image_ops.pdf_to_images(pdf_pages(2), dpi=20)

    assert free == [True, True]


def test_iter_pdf_to_images_checks_arguments_immediately() -> None:
    with pytest.raises(PdfOperationError, match="dpi"):
        image_ops.iter_pdf_to_images(pdf_pages(1), dpi=5)


def test_iter_images_matches_to_images() -> None:
    doc = PdfDocument.from_bytes(pdf_pages(3))

    assert list(doc.iter_images(dpi=20, pages=[2, 0])) == doc.to_images(dpi=20, pages=[2, 0])


def test_iter_images_does_no_work_until_consumed(monkeypatch: pytest.MonkeyPatch) -> None:
    derived = PdfDocument.from_bytes(pdf_pages(2)).rotate(90)
    writes: list[object] = []
    monkeypatch.setattr(derived, "_readable_bytes", lambda: writes.append(1) or pdf_pages(2))

    with pytest.raises(PdfOperationError, match="dpi"):
        derived.iter_images(dpi=5)
    images = derived.iter_images(dpi=20)
    assert writes == []
    assert len(list(images)) == 2
    assert writes == [1]


def test_closing_the_iterator_closes_the_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []
    real_close = pdfium.PdfDocument.close

    def spy_close(self: pdfium.PdfDocument) -> None:
        closed.append(True)
        real_close(self)

    monkeypatch.setattr(pdfium.PdfDocument, "close", spy_close)
    images = PdfDocument.from_bytes(pdf_pages(3)).iter_images(dpi=20)
    next(images)
    assert closed == []
    images.close()
    assert closed == [True]


def test_from_images_pixel_cap() -> None:
    small, large = image_bytes("PNG", size=(10, 10)), image_bytes("PNG", size=(100, 50))

    assert PdfDocument.from_images([small], max_pixels=100).page_count == 1
    with pytest.raises(LimitExceededError, match=r"image 2 is 5,000 pixels"):
        PdfDocument.from_images([small, large], max_pixels=1_000)


def test_from_images_pixel_cap_checks_every_tiff_frame() -> None:
    buffer = io.BytesIO()
    first, second = Image.new("RGB", (10, 10)), Image.new("RGB", (100, 50))
    first.save(buffer, "TIFF", save_all=True, append_images=[second])

    assert PdfDocument.from_images([buffer.getvalue()]).page_count == 2
    with pytest.raises(LimitExceededError, match=r"image 1 is 5,000 pixels"):
        PdfDocument.from_images([buffer.getvalue()], max_pixels=1_000)


def test_from_images_pixel_cap_leaves_unreadable_images_to_img2pdf() -> None:
    with pytest.raises(PdfOperationError, match="could not convert image"):
        PdfDocument.from_images([b"not an image"], max_pixels=1_000)
