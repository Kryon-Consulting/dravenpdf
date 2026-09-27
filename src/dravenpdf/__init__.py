"""dravenpdf: HTML to PDF conversion and PDF tools, powered by headless Chromium."""

from __future__ import annotations

import importlib
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from dravenpdf.document import PdfDocument
    from dravenpdf.document.forms import FormField
    from dravenpdf.document.signing import (
        SignatureBox,
        SignatureInfo,
        SigningKey,
        signature_problems,
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

# Imported on first use: the per-render proxy process imports dravenpdf.errors (and so
# this package) and must not pay for pikepdf, playwright or pydantic
# (see tests/unit/test_proxy_imports.py).
_LAZY = {
    "PdfDocument": "dravenpdf.document",
    "FormField": "dravenpdf.document.forms",
    "SignatureBox": "dravenpdf.document.signing",
    "SignatureInfo": "dravenpdf.document.signing",
    "SigningKey": "dravenpdf.document.signing",
    "signature_problems": "dravenpdf.document.signing",
    "HeaderFooter": "dravenpdf.options",
    "Margins": "dravenpdf.options",
    "RenderOptions": "dravenpdf.options",
    "Viewport": "dravenpdf.options",
    "AsyncRenderer": "dravenpdf.render",
    "BrowserPool": "dravenpdf.render",
    "Renderer": "dravenpdf.render",
    "RequestGuard": "dravenpdf.render",
    "Cookie": "dravenpdf.render.auth",
    "RenderAuth": "dravenpdf.render.auth",
    "StorageState": "dravenpdf.render.auth",
    "FailedRequest": "dravenpdf.render.report",
    "HttpError": "dravenpdf.render.report",
    "RenderReport": "dravenpdf.render.report",
}


def __getattr__(name: str) -> object:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value
