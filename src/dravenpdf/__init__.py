"""dravenpdf: HTML to PDF conversion and PDF tools, powered by headless Chromium."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from dravenpdf.document import PdfDocument
from dravenpdf.document.forms import FormField
from dravenpdf.document.signing import (
    SignatureBox,
    SignatureInfo,
    SigningKey,
    signature_problems,
)
from dravenpdf.errors import (
    AssetError,
    BlockedRequestError,
    DravenPdfError,
    IncompleteRenderError,
    InvalidPdfError,
    LimitExceededError,
    PdfOperationError,
    PdfPasswordError,
    PoolExhaustedError,
    RenderError,
    RenderTimeoutError,
    SignatureInvalidatedWarning,
    SigningError,
    TemplateError,
)
from dravenpdf.options import HeaderFooter, Margins, RenderOptions, Viewport
from dravenpdf.render import AsyncRenderer, BrowserPool, Renderer, RequestGuard
from dravenpdf.render.auth import Cookie, RenderAuth, StorageState
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
    "Cookie",
    "DravenPdfError",
    "FailedRequest",
    "FormField",
    "HeaderFooter",
    "HttpError",
    "IncompleteRenderError",
    "InvalidPdfError",
    "LimitExceededError",
    "Margins",
    "PdfDocument",
    "PdfOperationError",
    "PdfPasswordError",
    "PoolExhaustedError",
    "RenderAuth",
    "RenderError",
    "RenderOptions",
    "RenderReport",
    "RenderTimeoutError",
    "Renderer",
    "RequestGuard",
    "SignatureBox",
    "SignatureInfo",
    "SignatureInvalidatedWarning",
    "SigningError",
    "SigningKey",
    "StorageState",
    "TemplateError",
    "Viewport",
    "__version__",
    "signature_problems",
]
