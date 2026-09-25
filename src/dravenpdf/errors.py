"""Exception hierarchy for dravenpdf.

Every error raised on purpose by the library is a subclass of :class:`DravenPdfError`.
Each class has a stable ``code`` string; the HTTP service uses it in error responses
and maps it to a status code in one place (``dravenpdf.server.errors``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dravenpdf.render.report import RenderReport


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


class IncompleteRenderError(RenderError):
    """A strict render found missing resources or script errors (see ``.report``)."""

    code = "render_incomplete"

    def __init__(self, message: str, *, report: RenderReport) -> None:
        super().__init__(message)
        self.report = report


class TemplateError(DravenPdfError):
    """A Jinja2 template could not be found, parsed or rendered."""

    code = "invalid_template"


class AssetError(DravenPdfError):
    """An asset bundle is invalid, e.g. a path with ``..`` or too many files."""

    code = "invalid_request"


class InvalidPdfError(DravenPdfError):
    """Input bytes could not be read as a PDF."""

    code = "invalid_pdf"


class PdfPasswordError(InvalidPdfError):
    """The PDF is password-protected and no (or the wrong) password was given."""

    code = "pdf_password"


class PdfOperationError(DravenPdfError):
    """A PDF operation got arguments it can't apply, e.g. a page out of range."""

    code = "invalid_request"


class LimitExceededError(PdfOperationError):
    """An operation's output would pass a size limit, e.g. too many pixels."""

    code = "limit_exceeded"


class SigningError(DravenPdfError):
    """Signing or reading signatures failed (bad key file, unreadable signature...)."""

    code = "signing_failed"


class SignatureInvalidatedWarning(UserWarning):
    """An operation on a signed document writes a new file, which would break its
    signatures, so the result has them removed."""


class PoolExhaustedError(DravenPdfError):
    """Too many renders are already waiting for a browser slot."""

    code = "busy"


__all__ = [
    "AssetError",
    "BlockedRequestError",
    "DravenPdfError",
    "IncompleteRenderError",
    "InvalidPdfError",
    "LimitExceededError",
    "PdfOperationError",
    "PdfPasswordError",
    "PoolExhaustedError",
    "RenderError",
    "RenderTimeoutError",
    "SignatureInvalidatedWarning",
    "SigningError",
    "TemplateError",
]
