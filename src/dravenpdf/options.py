"""Render options shared by the Python API, the CLI and the HTTP service.

The same models validate an HTTP request body and a Python call, so both accept
exactly the same options.
"""

from __future__ import annotations

import re
import zoneinfo
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PaperSize = Literal["A3", "A4", "A5", "Letter", "Legal", "Tabloid"]
MediaType = Literal["print", "screen"]
WaitUntil = Literal["load", "domcontentloaded", "networkidle"]
ColorScheme = Literal["light", "dark", "no-preference"]
ReducedMotion = Literal["reduce", "no-preference"]

# Units Chromium's print API accepts. A bare number means pixels.
_CSS_LENGTH = re.compile(r"^\d+(\.\d+)?(px|in|cm|mm)?$")
# "1-3, 5, 8-10" (1-based, as people write them).
_PAGE_RANGES = re.compile(r"^\s*\d+(\s*-\s*\d+)?(\s*,\s*\d+(\s*-\s*\d+)?)*\s*$")

MAX_TIMEOUT_MS = 300_000
# BCP 47 language tag, e.g. "en", "de-DE", "zh-Hant-TW".
_LOCALE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


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


class Viewport(_Model):
    """Browser window size in CSS pixels, before printing.

    Printing lays the page out again at the paper width, so CSS-only pages look the
    same at any viewport. It matters for scripts that measure the window while the
    page loads, such as chart libraries and responsive dashboards.
    """

    width: int = Field(ge=100, le=10_000)
    height: int = Field(ge=100, le=10_000)


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
    margins: Margins | None = Field(default_factory=Margins)
    """Page margins. None sends no margins, leaving them to CSS ``@page`` rules (an
    explicit ``@page { margin }`` wins over this setting either way)."""
    prefer_css_page_size: bool = False
    """Use the page size from CSS ``@page { size }`` instead of ``paper``/``width``."""
    tagged: bool = False
    """Write a tagged PDF (structure tree for screen readers). Not a PDF/UA guarantee."""
    outline: bool = False
    """Add bookmarks built from the document's headings. Implies ``tagged``: Chromium
    builds the outline from the tag structure and silently skips it otherwise."""
    scale: float = Field(default=1.0, ge=0.1, le=2.0)
    print_background: bool = True
    media: MediaType = "print"
    header: HeaderFooter | None = None
    footer: HeaderFooter | None = None
    page_ranges: str | None = None
    wait_until: WaitUntil = "networkidle"
    wait_for_selector: str | None = None
    wait_for_ready_flag: bool = False
    wait_for_expression: str | None = Field(default=None, min_length=1, max_length=10_000)
    """JavaScript expression (or function) polled until it returns something truthy."""
    timeout_ms: int = Field(default=30_000, gt=0, le=MAX_TIMEOUT_MS)
    fail_on_resource_errors: bool = False
    """Fail if any image, stylesheet, font, script or fetch fails or returns HTTP 4xx/5xx."""
    fail_on_page_errors: bool = False
    """Fail if the page throws an uncaught JavaScript exception."""

    # Browser environment. None keeps Chromium's default.
    viewport: Viewport | None = None
    """Window size while the page loads (default 1280 x 720)."""
    device_scale_factor: float = Field(default=1.0, ge=1.0, le=4.0)
    """Pixels per CSS pixel; raise it for sharper canvas charts in the PDF."""
    locale: str | None = None
    """BCP 47 tag like "de-DE": navigator.language, Intl formatting, Accept-Language."""
    timezone: str | None = None
    """IANA time zone like "Europe/Berlin" for dates the page formats."""
    color_scheme: ColorScheme | None = None
    """What prefers-color-scheme media queries see."""
    reduced_motion: ReducedMotion | None = None
    """What prefers-reduced-motion sees; "reduce" skips many CSS animations."""

    @field_validator("locale")
    @classmethod
    def _valid_locale(cls, value: str | None) -> str | None:
        if value is not None and not _LOCALE.match(value):
            raise ValueError(f"invalid locale {value!r}; use a tag like 'en-US' or 'de'")
        return value

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            zoneinfo.ZoneInfo(value)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {value!r}; use e.g. 'Europe/Berlin'") from None
        return value

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

    def to_context_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for Playwright's ``browser.new_context()``."""
        kwargs: dict[str, Any] = {}
        if self.viewport is not None:
            kwargs["viewport"] = {"width": self.viewport.width, "height": self.viewport.height}
        if self.device_scale_factor != 1.0:
            kwargs["device_scale_factor"] = self.device_scale_factor
        if self.locale is not None:
            kwargs["locale"] = self.locale
        if self.timezone is not None:
            kwargs["timezone_id"] = self.timezone
        if self.color_scheme is not None:
            kwargs["color_scheme"] = self.color_scheme
        if self.reduced_motion is not None:
            kwargs["reduced_motion"] = self.reduced_motion
        return kwargs

    def to_pdf_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for Playwright's ``page.pdf()``."""
        kwargs: dict[str, Any] = {
            "landscape": self.landscape,
            "scale": self.scale,
            "print_background": self.print_background,
            "prefer_css_page_size": self.prefer_css_page_size,
        }
        if self.margins is not None:
            kwargs["margin"] = self.margins.model_dump()
        # Only sent when on, so older Playwright versions without them still work.
        if self.tagged or self.outline:
            kwargs["tagged"] = True
        if self.outline:
            kwargs["outline"] = True
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
    "ColorScheme",
    "HeaderFooter",
    "Margins",
    "MediaType",
    "PaperSize",
    "ReducedMotion",
    "RenderOptions",
    "Viewport",
    "WaitUntil",
]
