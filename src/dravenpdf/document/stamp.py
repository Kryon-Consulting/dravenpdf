"""Stamps and watermarks: text, images, or pages of another PDF drawn onto pages.

All stamps are placed the same way. A stamp is a Form XObject drawn in the page's
*displayed* coordinates (width x height as a viewer shows it, origin bottom-left),
then mapped onto the page with a matrix that undoes the page's ``/Rotate`` and
``/MediaBox`` offset. So a stamp sits upright on screen whatever the page rotation.
"""

from __future__ import annotations

import io
import math
import zlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal, get_args

import pikepdf
from PIL import Image, UnidentifiedImageError

from dravenpdf.errors import PdfOperationError

Position = Literal[
    "center", "top-left", "top", "top-right", "left", "right",
    "bottom-left", "bottom", "bottom-right",
]  # fmt: skip

POSITIONS: tuple[str, ...] = get_args(Position)

# Helvetica advance widths (1/1000 em) for printable ASCII, from the standard AFM.
# Other characters fall back to an average width.
_HELVETICA_WIDTHS = dict(
    zip(
        (chr(c) for c in range(32, 127)),
        (
            278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
            556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
            1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
            667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
            333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
            556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
        ),
        strict=True,
    )
)  # fmt: skip
_DEFAULT_WIDTH = 556


@dataclass(frozen=True)
class _Geometry:
    """A page as displayed, and how to map displayed coordinates onto it."""

    width: float
    height: float
    matrix: tuple[float, float, float, float, float, float]


def _geometry(page: pikepdf.Page) -> _Geometry:
    x0, y0, x1, y1 = (float(v) for v in page.mediabox)
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    w, h = x1 - x0, y1 - y0
    rotation = int(page.rotation) % 360
    # Displayed point (x', y') -> page user space (x, y), as a PDF cm matrix.
    if rotation == 90:
        return _Geometry(h, w, (0, 1, -1, 0, x0 + w, y0))
    if rotation == 180:
        return _Geometry(w, h, (-1, 0, 0, -1, x0 + w, y0 + h))
    if rotation == 270:
        return _Geometry(h, w, (0, -1, 1, 0, x0, y0 + h))
    return _Geometry(w, h, (1, 0, 0, 1, x0, y0))


def _num(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _matrix(m: Iterable[float]) -> str:
    return " ".join(_num(v) for v in m)


def target_pages(pdf: pikepdf.Pdf, pages: Iterable[int] | None) -> list[int]:
    count = len(pdf.pages)
    if pages is None:
        return list(range(count))
    result = []
    for index in pages:
        if not -count <= index < count:
            raise PdfOperationError(f"page index {index} is out of range for {count} pages")
        result.append(index % count)
    return sorted(set(result))


# ---------------------------------------------------------------- placement


def _place(
    pdf: pikepdf.Pdf,
    index: int,
    form: pikepdf.Object,
    *,
    opacity: float,
    under: bool,
) -> None:
    """Draw ``form`` (already in ``pdf``, in displayed coordinates) on page ``index``."""
    page = pdf.pages[index]
    geometry = _geometry(page)
    name = page.add_resource(form, pikepdf.Name.XObject, prefix="DpStamp")
    ops = f"q {_matrix(geometry.matrix)} cm "
    if opacity < 1:
        gs = pdf.make_indirect(
            pikepdf.Dictionary(Type=pikepdf.Name.ExtGState, ca=opacity, CA=opacity)
        )
        gs_name = page.add_resource(gs, pikepdf.Name.ExtGState, prefix="DpGs")
        ops += f"{gs_name} gs "
    ops += f"{name} Do Q\n"
    if under:
        page.contents_add(pdf.make_stream(ops.encode()), prepend=True)
    else:
        # Isolate the page's own content so its leftover graphics state can't leak in.
        page.contents_add(pdf.make_stream(b"q\n"), prepend=True)
        page.contents_add(pdf.make_stream(b"Q\n" + ops.encode()), prepend=False)


def _check_opacity(opacity: float) -> None:
    if not 0 < opacity <= 1:
        raise PdfOperationError(f"opacity must be in (0, 1], not {opacity}")


def _stamp_each(
    pdf: pikepdf.Pdf,
    pages: Iterable[int] | None,
    make_form: Callable[[float, float], pikepdf.Object],
    *,
    opacity: float,
    under: bool,
) -> None:
    """Stamp every target page; ``make_form(width, height)`` is called once per page size."""
    _check_opacity(opacity)
    forms: dict[tuple[float, float], pikepdf.Object] = {}
    for index in target_pages(pdf, pages):
        geometry = _geometry(pdf.pages[index])
        size = (round(geometry.width, 2), round(geometry.height, 2))
        if size not in forms:
            forms[size] = make_form(geometry.width, geometry.height)
        _place(pdf, index, forms[size], opacity=opacity, under=under)


def _form(
    pdf: pikepdf.Pdf,
    width: float,
    height: float,
    content: str,
    resources: pikepdf.Dictionary,
    *,
    group: bool,
) -> pikepdf.Object:
    form = pdf.make_stream(content.encode("latin-1"))
    form.Type = pikepdf.Name.XObject
    form.Subtype = pikepdf.Name.Form
    form.BBox = [0, 0, width, height]
    form.Resources = resources
    if group:
        # Make /ca apply to the stamp as a whole rather than to each object in it.
        form.Group = pikepdf.Dictionary(S=pikepdf.Name.Transparency)
    return form


def _anchor(
    position: str, page_w: float, page_h: float, box_w: float, box_h: float, margin: float
) -> tuple[float, float]:
    """Center point of a ``box_w`` x ``box_h`` box placed at ``position``."""
    if position not in POSITIONS:
        raise PdfOperationError(f"position must be one of {', '.join(POSITIONS)}")
    if "left" in position:
        cx = margin + box_w / 2
    elif "right" in position:
        cx = page_w - margin - box_w / 2
    else:
        cx = page_w / 2
    if position.startswith("top"):
        cy = page_h - margin - box_h / 2
    elif position.startswith("bottom"):
        cy = margin + box_h / 2
    else:
        cy = page_h / 2
    return cx, cy


# ---------------------------------------------------------------- text


def parse_color(color: str) -> tuple[float, float, float]:
    """``"#RRGGBB"`` or ``"#RGB"`` to PDF RGB components."""
    value = color.strip().removeprefix("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    try:
        if len(value) != 6:
            raise ValueError
        r, g, b = (int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        raise PdfOperationError(f"invalid color {color!r}; use '#RRGGBB'") from None
    return r, g, b


def _encode_text(text: str) -> bytes:
    try:
        raw = text.encode("cp1252")
    except UnicodeEncodeError:
        raise PdfOperationError(
            "stamp_text supports Western European (cp1252) characters only; "
            "use stamp_html for other scripts"
        ) from None
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def stamp_text(
    pdf: pikepdf.Pdf,
    text: str,
    *,
    font_size: float,
    color: str,
    opacity: float,
    angle: float,
    position: str,
    margin: float,
    pages: Iterable[int] | None,
    under: bool,
) -> None:
    """Draw ``text`` in Helvetica, rotated ``angle`` degrees counter-clockwise."""
    if not text:
        raise PdfOperationError("stamp text is empty")
    if font_size <= 0:
        raise PdfOperationError("font_size must be positive")
    encoded = _encode_text(text)
    r, g, b = parse_color(color)
    text_w = sum(_HELVETICA_WIDTHS.get(c, _DEFAULT_WIDTH) for c in text) * font_size / 1000
    text_h = font_size * 0.72  # cap height, which is what looks centered
    radians = math.radians(angle)
    cos, sin = math.cos(radians), math.sin(radians)
    box_w = abs(text_w * cos) + abs(text_h * sin)
    box_h = abs(text_w * sin) + abs(text_h * cos)
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font,
            Subtype=pikepdf.Name.Type1,
            BaseFont=pikepdf.Name.Helvetica,
            Encoding=pikepdf.Name.WinAnsiEncoding,
        )
    )

    def make_form(width: float, height: float) -> pikepdf.Object:
        cx, cy = _anchor(position, width, height, box_w, box_h, margin)
        content = (
            f"{_num(r)} {_num(g)} {_num(b)} rg BT /F1 {_num(font_size)} Tf "
            f"{_matrix((cos, sin, -sin, cos, cx, cy))} Tm "
            f"{_num(-text_w / 2)} {_num(-text_h / 2)} Td ("
            + encoded.decode("latin-1")
            + ") Tj ET\n"
        )
        resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
        return _form(pdf, width, height, content, resources, group=opacity < 1)

    _stamp_each(pdf, pages, make_form, opacity=opacity, under=under)


# ---------------------------------------------------------------- images


def image_xobject(pdf: pikepdf.Pdf, data: bytes) -> tuple[pikepdf.Object, int, int]:
    """An Image XObject for PNG/JPEG/GIF/WebP/... bytes, keeping transparency.

    Returns the image and its size in pixels.
    """
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise PdfOperationError(f"not a supported image: {exc}") from exc
    width, height = image.size
    if image.format == "JPEG" and image.mode in ("L", "RGB"):
        stream = pdf.make_stream(data)
        stream.Filter = pikepdf.Name.DCTDecode
        colorspace = "/DeviceGray" if image.mode == "L" else "/DeviceRGB"
    else:
        has_alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
        rgba = image.convert("RGBA")
        stream = pdf.make_stream(zlib.compress(rgba.convert("RGB").tobytes()))
        stream.Filter = pikepdf.Name.FlateDecode
        colorspace = "/DeviceRGB"
        if has_alpha:
            alpha = pdf.make_stream(zlib.compress(rgba.getchannel("A").tobytes()))
            alpha.Type = pikepdf.Name.XObject
            alpha.Subtype = pikepdf.Name.Image
            alpha.Width, alpha.Height = width, height
            alpha.ColorSpace = pikepdf.Name.DeviceGray
            alpha.BitsPerComponent = 8
            alpha.Filter = pikepdf.Name.FlateDecode
            stream.SMask = alpha
    stream.Type = pikepdf.Name.XObject
    stream.Subtype = pikepdf.Name.Image
    stream.Width, stream.Height = width, height
    stream.ColorSpace = pikepdf.Name(colorspace)
    stream.BitsPerComponent = 8
    return stream, width, height


def stamp_image(
    pdf: pikepdf.Pdf,
    image: bytes,
    *,
    width: float | None,
    position: str,
    margin: float,
    opacity: float,
    pages: Iterable[int] | None,
    under: bool,
) -> None:
    """Draw an image. Size: ``width`` points, else its pixel size at 96 dpi; always
    shrunk to fit inside the margins."""
    if width is not None and width <= 0:
        raise PdfOperationError("width must be positive")
    xobject, px_w, px_h = image_xobject(pdf, image)
    aspect = px_h / px_w

    def make_form(page_w: float, page_h: float) -> pikepdf.Object:
        img_w = width if width is not None else px_w * 0.75
        img_h = img_w * aspect
        scale = min(1.0, (page_w - 2 * margin) / img_w, (page_h - 2 * margin) / img_h)
        if scale <= 0:
            raise PdfOperationError("margin leaves no room for the image")
        img_w, img_h = img_w * scale, img_h * scale
        cx, cy = _anchor(position, page_w, page_h, img_w, img_h, margin)
        content = f"{_matrix((img_w, 0, 0, img_h, cx - img_w / 2, cy - img_h / 2))} cm /Im0 Do\n"
        resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=xobject))
        return _form(pdf, page_w, page_h, content, resources, group=opacity < 1)

    _stamp_each(pdf, pages, make_form, opacity=opacity, under=under)


# ---------------------------------------------------------------- PDF pages


def overlay_page(
    pdf: pikepdf.Pdf,
    stamp: pikepdf.Pdf,
    *,
    stamp_page: int,
    opacity: float,
    pages: Iterable[int] | None,
    under: bool,
) -> None:
    """Draw a page of another PDF on each target page, scaled to fit and centered."""
    if not 0 <= stamp_page < len(stamp.pages):
        raise PdfOperationError(f"stamp has no page index {stamp_page}")
    source = stamp.pages[stamp_page]
    source_geometry = _geometry(source)
    inner = pdf.copy_foreign(source.as_form_xobject(handle_transformations=False))
    # Without handle_transformations the form is in the page's user space; the
    # matrix below maps it upright (the same mapping used for every other stamp).
    sw, sh = source_geometry.width, source_geometry.height

    def make_form(page_w: float, page_h: float) -> pikepdf.Object:
        scale = min(page_w / sw, page_h / sh)
        dx, dy = (page_w - sw * scale) / 2, (page_h - sh * scale) / 2
        upright = _invert(source_geometry.matrix)
        content = f"q {_matrix((scale, 0, 0, scale, dx, dy))} cm {_matrix(upright)} cm /Fx0 Do Q\n"
        resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Fx0=inner))
        return _form(pdf, page_w, page_h, content, resources, group=opacity < 1)

    _stamp_each(pdf, pages, make_form, opacity=opacity, under=under)


def _invert(
    m: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float, float, float]:
    a, b, c, d, e, f = m
    det = a * d - b * c
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return (ia, ib, ic, id_, -(e * ia + f * ic), -(e * ib + f * id_))
