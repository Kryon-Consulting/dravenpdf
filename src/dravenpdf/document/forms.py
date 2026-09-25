"""Filling and flattening existing PDF forms (AcroForms), with pikepdf.

Values are checked before anything is changed: unknown fields, read-only fields,
options that don't exist, text longer than the field allows and line breaks in
single-line fields are all errors, so a fill either fully applies or not at all.

Appearances (what a filled field looks like) are generated with QPDF's generator,
which draws Western European (cp1252) text with the form's own font. For other
text the value is stored and viewers are asked to draw it (``NeedAppearances``);
Acrobat and browser viewers do. Such values can't be flattened, because there is no
drawing to burn into the page, so ``flatten=True`` refuses them.

XFA forms: hybrid forms (XFA plus AcroForm fields) are filled through their
AcroForm fields and the XFA part is removed, so viewers show what was filled.
XFA-only forms are refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import pikepdf
from pikepdf import form as pf

from dravenpdf.errors import PdfOperationError

FieldKind = Literal["text", "checkbox", "radio", "choice", "signature", "button"]
FieldValue = str | bool | None


@dataclass(frozen=True)
class FormField:
    """One form field, as :meth:`PdfDocument.form_fields` reports it."""

    name: str
    kind: FieldKind
    value: FieldValue
    """Text for text/choice fields, True/False for checkboxes, the selected option
    for radio groups (None if none)."""
    options: tuple[str, ...] = ()
    """Radio button states or choice-field options."""
    read_only: bool = False
    required: bool = False
    multiline: bool = False
    max_length: int | None = None


def _plain_name(value: object) -> str:
    return str(value).removeprefix("/")


def _encodable(text: str) -> bool:
    try:
        text.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def _acroform(pdf: pikepdf.Pdf) -> pikepdf.Dictionary | None:
    acroform = pdf.Root.get("/AcroForm")
    return acroform if isinstance(acroform, pikepdf.Dictionary) else None


def _check_xfa(pdf: pikepdf.Pdf) -> None:
    acroform = _acroform(pdf)
    if acroform is None or "/XFA" not in acroform:
        return
    if not len(acroform.get("/Fields", [])):
        raise PdfOperationError("XFA-only forms are not supported (the PDF has no AcroForm fields)")


def _form(pdf: pikepdf.Pdf, *, appearances: bool) -> pf.Form:
    generator = pf.DefaultAppearanceStreamGenerator if appearances else None
    try:
        return pf.Form(pdf, generator)
    except Exception as exc:  # pikepdf raises plain errors for malformed forms
        raise PdfOperationError(f"can't read this PDF's form: {exc}") from exc


def _describe(name: str, field: object) -> FormField:
    ro = bool(getattr(field, "is_read_only", False))
    req = bool(getattr(field, "is_required", False))
    if isinstance(field, pf.TextField):
        return FormField(
            name, "text", str(field.value), read_only=ro, required=req,
            multiline=field.is_multiline, max_length=field.max_length or None,
        )  # fmt: skip
    if isinstance(field, pf.CheckboxField):
        return FormField(name, "checkbox", bool(field.checked), read_only=ro, required=req)
    if isinstance(field, pf.RadioButtonGroup):
        states = tuple(_plain_name(s) for s in field.states)
        current = _plain_name(field.value) if field.value is not None else "Off"
        value = None if current == "Off" else current
        return FormField(name, "radio", value, options=states, read_only=ro, required=req)
    if isinstance(field, pf.ChoiceField):
        options = tuple(str(o.display_value) for o in field.options)
        selected = str(field.value) if field.value else None
        return FormField(name, "choice", selected, options=options, read_only=ro, required=req)
    kind: FieldKind = "signature" if type(field).__name__ == "SignatureField" else "button"
    return FormField(name, kind, None, read_only=ro, required=req)


def list_fields(pdf: pikepdf.Pdf) -> list[FormField]:
    if _acroform(pdf) is None:
        return []
    _check_xfa(pdf)
    return [_describe(name, field) for name, field in _form(pdf, appearances=False).items()]


def fill(pdf: pikepdf.Pdf, values: Mapping[str, FieldValue], *, flatten: bool = False) -> None:
    """Fill ``pdf`` in place (callers pass a copy). See the module docstring."""
    if not values and not flatten:
        return
    if _acroform(pdf) is None:
        raise PdfOperationError("this PDF has no form")
    _check_xfa(pdf)
    fields = dict(_form(pdf, appearances=False).items())
    described = {name: _describe(name, field) for name, field in fields.items()}

    unknown = sorted(set(values) - set(fields))
    if unknown:
        raise PdfOperationError(f"no such form field(s): {', '.join(unknown)}")
    viewer_drawn: list[str] = []
    for name, value in values.items():
        _validate(described[name], value)
        if isinstance(value, str) and not _encodable(value):
            viewer_drawn.append(name)
    if flatten and viewer_drawn:
        raise PdfOperationError(
            "can't flatten non-Western-European text (fields: "
            f"{', '.join(viewer_drawn)}); fill without flattening, and viewers will draw it"
        )

    acroform = _acroform(pdf)
    assert acroform is not None
    if "/XFA" in acroform:
        del acroform["/XFA"]  # hybrid form: make viewers use the AcroForm we fill
    drawn = dict(_form(pdf, appearances=True).items())
    for name, value in values.items():
        _apply(drawn[name] if name not in viewer_drawn else fields[name], value)
    if viewer_drawn:
        acroform.NeedAppearances = True
    if flatten:
        flatten_form(pdf)


def _validate(field: FormField, value: FieldValue) -> None:
    where = f"field {field.name!r}"
    if field.read_only:
        raise PdfOperationError(f"{where} is read-only")
    if field.kind in ("signature", "button"):
        raise PdfOperationError(f"{where} is a {field.kind} and can't be filled")
    if field.kind == "checkbox":
        if not isinstance(value, bool):
            raise PdfOperationError(f"{where} is a checkbox: use true or false")
        return
    if value is not None and not isinstance(value, str):
        raise PdfOperationError(f"{where} takes text")
    if field.kind == "text":
        text = value or ""
        if field.max_length is not None and len(text) > field.max_length:
            raise PdfOperationError(f"{where} allows at most {field.max_length} characters")
        if not field.multiline and ("\n" in text or "\r" in text):
            raise PdfOperationError(f"{where} is single-line; remove the line breaks")
    elif field.kind == "radio" and value not in field.options:
        options = ", ".join(field.options)
        raise PdfOperationError(f"{where} needs one of its options ({options}), not {value!r}")
    elif field.kind == "choice" and value is not None and value not in field.options:
        options = ", ".join(field.options)
        raise PdfOperationError(f"{where} has no option {value!r} (options: {options})")


def _apply(field: object, value: FieldValue) -> None:
    text = value if isinstance(value, str) else ""
    if isinstance(field, pf.TextField):
        field.value = text
    elif isinstance(field, pf.CheckboxField):
        field.checked = bool(value)
    elif isinstance(field, pf.RadioButtonGroup):
        # Select the option object: assigning .value would store a string, not a
        # name, and leave every button drawn as off.
        field.selected = next(o for o in field.options if _plain_name(o.on_value) == text)
    elif isinstance(field, pf.ChoiceField):
        field.value = text


def flatten_form(pdf: pikepdf.Pdf) -> None:
    """Draw every field's current appearance into its page and remove the form.

    Other annotations that have an appearance (comments, highlights) are drawn into
    the page too.
    """
    acroform = _acroform(pdf)
    if acroform is None:
        return
    _check_xfa(pdf)
    if acroform.get("/NeedAppearances"):
        raise PdfOperationError(
            "this form relies on the viewer to draw some values, so it can't be flattened"
        )
    pdf.generate_appearance_streams()
    pdf.flatten_annotations(mode="all")
    # QPDF skips widgets with nothing to draw (empty fields, unselected radio buttons);
    # with the form gone they'd be dead, so remove them.
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        if isinstance(annots, pikepdf.Array):
            kept = [a for a in annots if a.get("/Subtype") != pikepdf.Name.Widget]
            if kept:
                page.obj.Annots = pikepdf.Array(kept)
            else:
                del page.obj["/Annots"]
    if "/AcroForm" in pdf.Root:
        del pdf.Root["/AcroForm"]
