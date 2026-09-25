"""Page-level operations on :class:`pikepdf.Pdf` objects.

These functions never modify their inputs: each returns a new ``pikepdf.Pdf``.
:class:`dravenpdf.document.pdf.PdfDocument` wraps them in a chainable API.

Page numbers: integer arguments are 0-based (negative values count from the end,
as in Python). Range strings such as ``"1-3,5,8-"`` are 1-based, because that is
how people write page ranges; ``"8-"`` means page 8 to the end.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable, Sequence

import pikepdf

from dravenpdf.errors import PdfOperationError

_RANGE_PART = re.compile(r"^(\d+)(?:-(\d*))?$")


def clone(pdf: pikepdf.Pdf) -> pikepdf.Pdf:
    """A full, independent copy (keeps bookmarks, metadata and everything else)."""
    buffer = io.BytesIO()
    pdf.save(buffer)
    buffer.seek(0)
    return pikepdf.open(buffer)


def parse_page_ranges(spec: str, page_count: int) -> list[int]:
    """Turn ``"1-3,5,8-"`` into 0-based page indices, in the order written.

    Raises :class:`PdfOperationError` for malformed ranges or pages that don't exist.
    """
    if not spec.strip():
        raise PdfOperationError("page range is empty")
    indices: list[int] = []
    for raw in spec.split(","):
        part = raw.replace(" ", "")
        match = _RANGE_PART.match(part)
        if not match:
            raise PdfOperationError(f"invalid page range {raw.strip()!r}; use e.g. '1-3,5,8-'")
        first = int(match.group(1))
        if match.group(2) is None:
            last = first
        elif match.group(2) == "":
            last = page_count
        else:
            last = int(match.group(2))
        if first < 1 or last < first:
            raise PdfOperationError(f"invalid page range {part!r}; pages start at 1")
        if last > page_count:
            raise PdfOperationError(f"page range {part!r} is past the last page ({page_count})")
        indices.extend(range(first - 1, last))
    return indices


def normalize_indices(pages: Iterable[int], page_count: int) -> list[int]:
    """Check 0-based indices (negative ones count from the end) and make them positive."""
    result = []
    for index in pages:
        if not -page_count <= index < page_count:
            raise PdfOperationError(
                f"page index {index} is out of range for a {page_count}-page document"
            )
        result.append(index % page_count)
    return result


def _copy_docinfo(source: pikepdf.Pdf, target: pikepdf.Pdf) -> None:
    # Document info holds text (title, author, dates). Copy the text itself: pikepdf
    # won't take objects that belong to another Pdf.
    for key, value in source.docinfo.items():
        if isinstance(value, pikepdf.String):
            target.docinfo[key] = str(value)


def _from_pages(source: pikepdf.Pdf, indices: Sequence[int]) -> pikepdf.Pdf:
    """A new PDF with the given pages of ``source``, in that order (repeats allowed)."""
    result = pikepdf.new()
    for index in indices:
        result.pages.append(source.pages[index])
    _copy_docinfo(source, result)
    return _detach(result)


def _detach(pdf: pikepdf.Pdf) -> pikepdf.Pdf:
    """Materialize pages copied from other PDFs.

    pikepdf copies page content from a source PDF lazily, and the source has to stay
    open until the copy is saved. Saving and reopening now means the result no
    longer depends on its sources, which callers are free to drop.
    """
    return clone(pdf)


def merge(pdfs: Sequence[pikepdf.Pdf]) -> pikepdf.Pdf:
    """All pages of each PDF, in order. Metadata comes from the first one."""
    if not pdfs:
        raise PdfOperationError("nothing to merge")
    result = pikepdf.new()
    for pdf in pdfs:
        result.pages.extend(pdf.pages)
    _copy_docinfo(pdfs[0], result)
    return _detach(result)


def extract(pdf: pikepdf.Pdf, ranges: str) -> pikepdf.Pdf:
    """The pages named by a 1-based range string, e.g. ``"2-5"``."""
    return _from_pages(pdf, parse_page_ranges(ranges, len(pdf.pages)))


def split_every(pdf: pikepdf.Pdf, every: int) -> list[pikepdf.Pdf]:
    """Chunks of ``every`` pages (the last chunk may be shorter)."""
    if every < 1:
        raise PdfOperationError("every must be at least 1")
    count = len(pdf.pages)
    return [
        _from_pages(pdf, range(start, min(start + every, count)))
        for start in range(0, count, every)
    ]


def split_ranges(pdf: pikepdf.Pdf, ranges: Sequence[str]) -> list[pikepdf.Pdf]:
    """One PDF per range string, e.g. ``["1-3", "4-"]``."""
    if not ranges:
        raise PdfOperationError("no ranges given")
    return [extract(pdf, spec) for spec in ranges]


def rotate(pdf: pikepdf.Pdf, degrees: int, pages: Iterable[int] | None = None) -> pikepdf.Pdf:
    """Rotate pages clockwise by a multiple of 90 degrees (all pages by default)."""
    if degrees % 90 != 0:
        raise PdfOperationError(f"rotation must be a multiple of 90 degrees, not {degrees}")
    result = clone(pdf)
    count = len(result.pages)
    targets = range(count) if pages is None else normalize_indices(pages, count)
    for index in set(targets):
        result.pages[index].rotate(degrees, relative=True)
    return result


def delete(pdf: pikepdf.Pdf, pages: Iterable[int]) -> pikepdf.Pdf:
    """Remove pages. At least one page must remain."""
    count = len(pdf.pages)
    doomed = set(normalize_indices(pages, count))
    if len(doomed) == count:
        raise PdfOperationError("cannot delete every page")
    result = clone(pdf)
    for index in sorted(doomed, reverse=True):
        del result.pages[index]
    return result


def reorder(pdf: pikepdf.Pdf, order: Sequence[int]) -> pikepdf.Pdf:
    """Pages in a new order. ``order`` must name every page exactly once."""
    count = len(pdf.pages)
    indices = normalize_indices(order, count)
    if sorted(indices) != list(range(count)):
        raise PdfOperationError(
            f"order must list each of the {count} pages exactly once, got {list(order)}"
        )
    return _from_pages(pdf, indices)


def insert(pdf: pikepdf.Pdf, other: pikepdf.Pdf, at: int) -> pikepdf.Pdf:
    """All pages of ``other`` inserted before page ``at`` (``at == page_count`` appends)."""
    count = len(pdf.pages)
    if not 0 <= at <= count:
        raise PdfOperationError(f"insert position {at} is out of range 0..{count}")
    result = pikepdf.new()
    result.pages.extend(pdf.pages[:at])
    result.pages.extend(other.pages)
    result.pages.extend(pdf.pages[at:])
    _copy_docinfo(pdf, result)
    return _detach(result)
