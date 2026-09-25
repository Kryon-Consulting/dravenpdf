"""Exception hierarchy for dravenpdf.

Every error raised on purpose by the library is a subclass of :class:`DravenPdfError`.
Each class has a stable ``code`` string; the HTTP service uses it in error responses
and maps it to a status code in one place (``dravenpdf.server.errors``).
"""

from __future__ import annotations


class DravenPdfError(Exception):
    """Base class for all dravenpdf errors."""

    code: str = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class RenderError(DravenPdfError):
    """Rendering HTML to PDF failed."""

    code = "render_failed"


class RenderTimeoutError(RenderError):
    """A render took longer than its timeout."""

    code = "render_timeout"

    def __init__(self, message: str, *, timeout_ms: int) -> None:
        super().__init__(message)
        self.timeout_ms = timeout_ms


class BlockedRequestError(RenderError):
    """The request guard refused a URL (SSRF protection)."""

    code = "blocked_request"

    def __init__(self, message: str, *, url: str) -> None:
        super().__init__(message)
        self.url = url


class InvalidPdfError(DravenPdfError):
    """Input bytes could not be read as a PDF."""

    code = "invalid_pdf"


class PdfOperationError(DravenPdfError):
    """A PDF operation got arguments it can't apply, e.g. a page out of range."""

    code = "invalid_request"


class PoolExhaustedError(DravenPdfError):
    """Too many renders are already waiting for a browser slot."""

    code = "busy"


__all__ = [
    "BlockedRequestError",
    "DravenPdfError",
    "InvalidPdfError",
    "PdfOperationError",
    "PoolExhaustedError",
    "RenderError",
    "RenderTimeoutError",
]
