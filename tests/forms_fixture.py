"""Build a small AcroForm PDF by hand: text, multiline text, checkbox, radio group, dropdown."""

from __future__ import annotations

import io

import pikepdf
from pikepdf import Array, Dictionary, Name, String


def _stream(pdf: pikepdf.Pdf, content: bytes, width: float, height: float) -> pikepdf.Object:
    return pdf.make_stream(
        content, Type=Name.XObject, Subtype=Name.Form, BBox=Array([0, 0, width, height])
    )


def build_form(*, xfa: bool = False, xfa_only: bool = False) -> bytes:
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(400, 400))
    page = pdf.pages[0]
    helv = pdf.make_indirect(
        Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica,
                   Encoding=Name.WinAnsiEncoding)
    )  # fmt: skip
    zadb = pdf.make_indirect(
        Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.ZapfDingbats)
    )
    fields = []

    def widget(rect: list[float], **extra: object) -> pikepdf.Object:
        annot = pdf.make_indirect(
            Dictionary(Type=Name.Annot, Subtype=Name.Widget, Rect=Array(rect), P=page.obj,
                       F=4, **extra)
        )  # fmt: skip
        if "/Annots" not in page.obj:
            page.obj.Annots = Array()
        page.obj.Annots.append(annot)
        return annot

    def text(name: str, rect: list[float], flags: int = 0, maxlen: int | None = None) -> None:
        extra = {"MaxLen": maxlen} if maxlen else {}
        fields.append(widget(rect, FT=Name.Tx, T=String(name), Ff=flags,
                             DA=String("/Helv 10 Tf 0 g"), **extra))  # fmt: skip

    def check_box(name: str, rect: list[float], on: str) -> None:
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        ap = Dictionary(N=Dictionary(**{on: _stream(pdf, b"0 g 2 2 10 10 re f", w, h),
                                        "Off": _stream(pdf, b"", w, h)}))  # fmt: skip
        fields.append(widget(rect, FT=Name.Btn, T=String(name), V=Name.Off, AS=Name.Off,
                             AP=ap, DA=String("/ZaDb 10 Tf 0 g")))  # fmt: skip

    text("name", [20, 350, 220, 370])
    text("notes", [20, 280, 220, 340], flags=1 << 12)  # multiline
    text("code", [20, 250, 120, 270], maxlen=4)
    text("id", [240, 250, 380, 270], flags=1)  # read-only
    check_box("agree", [20, 220, 34, 234], "Yes")

    radio = pdf.make_indirect(
        Dictionary(FT=Name.Btn, T=String("size"), Ff=(1 << 15) | (1 << 14), V=Name.Off,
                   Kids=Array())
    )  # fmt: skip
    for i, option in enumerate(("S", "M", "L")):
        rect = [20 + i * 30, 190, 34 + i * 30, 204]
        ap = Dictionary(N=Dictionary(**{option: _stream(pdf, b"0 g 3 3 8 8 re f", 14, 14),
                                        "Off": _stream(pdf, b"", 14, 14)}))  # fmt: skip
        kid = widget(rect, Parent=radio, AS=Name.Off, AP=ap)
        radio.Kids.append(kid)
    fields.append(radio)

    fields.append(
        widget([20, 150, 220, 170], FT=Name.Ch, T=String("country"), Ff=1 << 17,
               Opt=Array([String("Germany"), String("France"), String("Japan")]),
               DA=String("/Helv 10 Tf 0 g"))
    )  # fmt: skip
    pdf.Root.AcroForm = Dictionary(
        Fields=Array(fields), DA=String("/Helv 10 Tf 0 g"),
        DR=Dictionary(Font=Dictionary(Helv=helv, ZaDb=zadb)),
    )  # fmt: skip
    if xfa or xfa_only:
        pdf.Root.AcroForm.XFA = pdf.make_stream(b"<xdp:xdp xmlns:xdp='http://ns.adobe.com/xdp/'/>")
    if xfa_only:
        pdf.Root.AcroForm.Fields = Array()
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()
