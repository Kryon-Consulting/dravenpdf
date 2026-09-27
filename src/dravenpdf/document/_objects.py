"""Small helpers for PDF objects shared by the form and signature code."""

from __future__ import annotations

from collections.abc import Callable

import pikepdf


def acroform(pdf: pikepdf.Pdf) -> pikepdf.Dictionary | None:
    """The document's interactive form dictionary, if it has a usable one."""
    form = pdf.Root.get("/AcroForm")
    return form if isinstance(form, pikepdf.Dictionary) else None


def remove_annotations(pdf: pikepdf.Pdf, doomed: Callable[[pikepdf.Object], bool]) -> None:
    """Drop every page annotation ``doomed`` returns True for; an emptied /Annots goes."""
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        if not isinstance(annots, pikepdf.Array):
            continue
        kept = [a for a in annots if not doomed(a)]
        if len(kept) == len(annots):
            continue
        if kept:
            page.obj.Annots = pikepdf.Array(kept)
        else:
            del page.obj["/Annots"]
