"""Render options shared by the Python API, the CLI and the HTTP service.

The same models validate an HTTP request body and a Python call, so both accept
exactly the same options.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PaperSize = Literal["A3", "A4", "A5", "Letter", "Legal", "Tabloid"]
MediaType = Literal["print", "screen"]
WaitUntil = Literal["load", "domcontentloaded", "networkidle"]

# Units Chromium's print API accepts. A bare number means pixels.
_CSS_LENGTH = re.compile(r"^\d+(\.\d+)?(px|in|cm|mm)?$")
# "1-3, 5, 8-10" (1-based, as people write them).
_PAGE_RANGES = re.compile(r"^\s*\d+(\s*-\s*\d+)?(\s*,\s*\d+(\s*-\s*\d+)?)*\s*$")

MAX_TIMEOUT_MS = 300_000


def _check_css_length(value: str) -> str:
    value = value.strip()
    if not _CSS_LENGTH.match(value):
        raise ValueError(f"invalid CSS length {value!r}; use e.g. '20mm', '1in', '96px' or '96'")
    return value


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Margins(_Model):
    """Page margins as CSS lengths."""

    top: str = "20mm"
    right: str = "15mm"
    bottom: str = "20mm"
    left: str = "15mm"

    @field_validator("top", "right", "bottom", "left")
    @classmethod
    def _valid_length(cls, value: str) -> str:
        return _check_css_length(value)


class HeaderFooter(_Model):
    """HTML for the page header or footer.

    Chromium fills these span classes in: ``pageNumber``, ``totalPages``, ``date``,
    ``title`` and ``url``. The template does not see the page's stylesheets, so
    use inline styles, and set a font size (the default is very small).
    Leave enough top/bottom margin for it to show.
    """

    html: str = Field(min_length=1)


class RenderOptions(_Model):
    """How to lay out and when to capture a rendered page."""

    paper: PaperSize = "A4"
    width: str | None = None
    height: str | None = None
    landscape: bool = False
    margins: Margins = Field(default_factory=Margins)
    scale: float = Field(default=1.0, ge=0.1, le=2.0)
    print_background: bool = True
    media: MediaType = "print"
    header: HeaderFooter | None = None
    footer: HeaderFooter | None = None
    page_ranges: str | None = None
    wait_until: WaitUntil = "networkidle"
    wait_for_selector: str | None = None
    wait_for_ready_flag: bool = False
    timeout_ms: int = Field(default=30_000, gt=0, le=MAX_TIMEOUT_MS)

    @field_validator("width", "height")
    @classmethod
    def _valid_size(cls, value: str | None) -> str | None:
        return None if value is None else _check_css_length(value)

    @field_validator("page_ranges")
    @classmethod
    def _valid_ranges(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _PAGE_RANGES.match(value):
            raise ValueError(f"invalid page ranges {value!r}; use e.g. '1-3, 5'")
        for part in value.split(","):
            bounds = [int(n) for n in part.split("-")]
            if bounds[0] < 1 or bounds[-1] < bounds[0]:
                raise ValueError(f"invalid page range {part.strip()!r}; pages start at 1")
        return value.replace(" ", "")

    @model_validator(mode="after")
    def _width_and_height_together(self) -> RenderOptions:
        if (self.width is None) != (self.height is None):
            raise ValueError("set both width and height, or neither")
        return self

    def to_pdf_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for Playwright's ``page.pdf()``."""
        kwargs: dict[str, Any] = {
            "landscape": self.landscape,
            "margin": self.margins.model_dump(),
            "scale": self.scale,
            "print_background": self.print_background,
            "prefer_css_page_size": False,
        }
        if self.width is not None:
            kwargs["width"] = self.width
            kwargs["height"] = self.height
        else:
            kwargs["format"] = self.paper
        if self.header is not None or self.footer is not None:
            # Chromium draws its own default header/footer when a template is empty,
            # so a blank element stands in for whichever one wasn't given.
            kwargs["display_header_footer"] = True
            kwargs["header_template"] = self.header.html if self.header else "<span></span>"
            kwargs["footer_template"] = self.footer.html if self.footer else "<span></span>"
        if self.page_ranges is not None:
            kwargs["page_ranges"] = self.page_ranges
        return kwargs


__all__ = [
    "HeaderFooter",
    "Margins",
    "MediaType",
    "PaperSize",
    "RenderOptions",
    "WaitUntil",
]
