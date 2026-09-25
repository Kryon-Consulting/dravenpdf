from __future__ import annotations

import pytest

from dravenpdf import errors


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (errors.RenderError("x"), "render_failed"),
        (errors.RenderTimeoutError("x", timeout_ms=5), "render_timeout"),
        (errors.BlockedRequestError("x", url="file:///etc/passwd"), "blocked_request"),
        (errors.InvalidPdfError("x"), "invalid_pdf"),
        (errors.PdfOperationError("x"), "invalid_request"),
        (errors.PoolExhaustedError("x"), "busy"),
    ],
)
def test_every_error_is_a_dravenpdf_error_with_a_code(
    exc: errors.DravenPdfError, code: str
) -> None:
    assert isinstance(exc, errors.DravenPdfError)
    assert exc.code == code
    assert exc.message == "x"


def test_render_subclasses() -> None:
    assert issubclass(errors.RenderTimeoutError, errors.RenderError)
    assert issubclass(errors.BlockedRequestError, errors.RenderError)


def test_extra_context_is_kept() -> None:
    assert errors.RenderTimeoutError("slow", timeout_ms=1000).timeout_ms == 1000
    assert errors.BlockedRequestError("no", url="http://10.0.0.1").url == "http://10.0.0.1"
