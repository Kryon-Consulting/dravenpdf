"""dravenpdf: HTML to PDF conversion and PDF tools, powered by headless Chromium."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from dravenpdf.errors import (
    BlockedRequestError,
    DravenPdfError,
    InvalidPdfError,
    PdfOperationError,
    PoolExhaustedError,
    RenderError,
    RenderTimeoutError,
)
from dravenpdf.options import HeaderFooter, Margins, RenderOptions

try:
    __version__ = version("dravenpdf")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0"

__all__ = [
    "BlockedRequestError",
    "DravenPdfError",
    "HeaderFooter",
    "InvalidPdfError",
    "Margins",
    "PdfOperationError",
    "PoolExhaustedError",
    "RenderError",
    "RenderOptions",
    "RenderTimeoutError",
    "__version__",
]
