"""dravenpdf: HTML to PDF conversion and PDF tools, powered by headless Chromium."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from dravenpdf.document import PdfDocument
from dravenpdf.errors import (
    BlockedRequestError,
    DravenPdfError,
    InvalidPdfError,
    LimitExceededError,
    PdfOperationError,
    PoolExhaustedError,
    RenderError,
    RenderTimeoutError,
    TemplateError,
)
from dravenpdf.options import HeaderFooter, Margins, RenderOptions
from dravenpdf.render import AsyncRenderer, BrowserPool, Renderer, RequestGuard

try:
    __version__ = version("dravenpdf")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0"

__all__ = [
    "AsyncRenderer",
    "BlockedRequestError",
    "BrowserPool",
    "DravenPdfError",
    "HeaderFooter",
    "InvalidPdfError",
    "LimitExceededError",
    "Margins",
    "PdfDocument",
    "PdfOperationError",
    "PoolExhaustedError",
    "RenderError",
    "RenderOptions",
    "RenderTimeoutError",
    "Renderer",
    "RequestGuard",
    "TemplateError",
    "__version__",
]
