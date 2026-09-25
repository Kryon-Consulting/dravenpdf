"""dravenpdf: HTML to PDF conversion and PDF tools, powered by headless Chromium."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from dravenpdf.document import PdfDocument
from dravenpdf.errors import (
    AssetError,
    BlockedRequestError,
    DravenPdfError,
    IncompleteRenderError,
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
from dravenpdf.render.report import FailedRequest, HttpError, RenderReport

try:
    __version__ = version("dravenpdf")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0"

__all__ = [
    "AssetError",
    "AsyncRenderer",
    "BlockedRequestError",
    "BrowserPool",
    "DravenPdfError",
    "FailedRequest",
    "HeaderFooter",
    "HttpError",
    "IncompleteRenderError",
    "InvalidPdfError",
    "LimitExceededError",
    "Margins",
    "PdfDocument",
    "PdfOperationError",
    "PoolExhaustedError",
    "RenderError",
    "RenderOptions",
    "RenderReport",
    "RenderTimeoutError",
    "Renderer",
    "RequestGuard",
    "TemplateError",
    "__version__",
]
