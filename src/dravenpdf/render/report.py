"""What went wrong during a render: failed loads, HTTP errors, script errors.

A render can "succeed" with a missing image, stylesheet, font or API response. The
:class:`RenderReport` attached to every rendered document says so, and the strict
options in :class:`~dravenpdf.options.RenderOptions` turn such problems into errors.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from playwright.async_api import ConsoleMessage, Error, Page, Request, Response

from dravenpdf.errors import IncompleteRenderError

# Chromium logs this to the console for every HTTP error; the response is already
# recorded in http_errors, so the console copy is noise.
_CHROMIUM_LOAD_NOISE = "Failed to load resource"
_BLOCKED_BY_GUARD = "net::ERR_BLOCKED_BY_CLIENT"  # Chromium may add ".Inspector"
_MAX_ENTRIES = 100  # per list; a broken page shouldn't make an unbounded report


@dataclass(frozen=True)
class FailedRequest:
    url: str
    reason: str
    resource_type: str


@dataclass(frozen=True)
class HttpError:
    url: str
    status: int
    resource_type: str


@dataclass
class RenderReport:
    """Problems seen while rendering. Empty lists mean nothing went wrong."""

    failed_requests: list[FailedRequest] = field(default_factory=list)
    """Loads that never got a response (refused, DNS, aborted, ...)."""
    http_errors: list[HttpError] = field(default_factory=list)
    """Sub-resources answered with HTTP 400 or above."""
    page_errors: list[str] = field(default_factory=list)
    """Uncaught JavaScript exceptions."""
    console_errors: list[str] = field(default_factory=list)
    """``console.error`` messages from the page."""
    blocked: list[tuple[str, str]] = field(default_factory=list)
    """(url, reason) for requests the SSRF guard refused."""

    @property
    def resource_problems(self) -> int:
        return len(self.failed_requests) + len(self.http_errors)

    @property
    def ok(self) -> bool:
        """No failed or erroring resources, no script errors, nothing blocked."""
        return not (self.failed_requests or self.http_errors or self.page_errors or self.blocked)

    def summary(self, limit: int = 5) -> str:
        """A short human-readable list of the problems, for error messages and logs."""
        lines = [f"{e.url} returned HTTP {e.status}" for e in self.http_errors]
        lines += [f"{f.url} failed ({f.reason})" for f in self.failed_requests]
        lines += [f"script error: {m}" for m in self.page_errors]
        lines += [f"{url} blocked ({reason})" for url, reason in self.blocked]
        shown = "; ".join(lines[:limit])
        return shown + (f"; and {len(lines) - limit} more" if len(lines) > limit else "")


class ReportCollector:
    """Listens to a page's events and fills a :class:`RenderReport`."""

    def __init__(self) -> None:
        self.report = RenderReport()
        self._http_error_urls: set[str] = set()

    def attach(self, page: Page) -> None:
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_request_failed)
        page.on("pageerror", self._on_page_error)
        page.on("console", self._on_console)

    def _on_response(self, response: Response) -> None:
        if response.status >= 400 and len(self.report.http_errors) < _MAX_ENTRIES:
            self._http_error_urls.add(response.url)
            self.report.http_errors.append(
                HttpError(response.url, response.status, response.request.resource_type)
            )

    def _on_request_failed(self, request: Request) -> None:
        reason = request.failure or "failed"
        # Blocked requests are reported by the guard; an HTTP error response is
        # already recorded (Chromium then also aborts some of them, e.g. stylesheets).
        if reason.startswith(_BLOCKED_BY_GUARD) or request.url in self._http_error_urls:
            return
        if len(self.report.failed_requests) < _MAX_ENTRIES:
            self.report.failed_requests.append(
                FailedRequest(request.url, reason, request.resource_type)
            )

    def _on_page_error(self, error: Error) -> None:
        if len(self.report.page_errors) < _MAX_ENTRIES:
            self.report.page_errors.append(error.message or str(error))

    def _on_console(self, message: ConsoleMessage) -> None:
        if message.type != "error" or message.text.startswith(_CHROMIUM_LOAD_NOISE):
            return
        if len(self.report.console_errors) < _MAX_ENTRIES:
            self.report.console_errors.append(message.text)

    def check(self, *, fail_on_resource_errors: bool, fail_on_page_errors: bool) -> None:
        """Raise :class:`IncompleteRenderError` if a strict option is violated."""
        report = self.report
        problems = []
        if fail_on_resource_errors and report.resource_problems:
            problems.append(f"{report.resource_problems} resource(s) failed to load")
        if fail_on_page_errors and report.page_errors:
            problems.append(f"{len(report.page_errors)} script error(s)")
        if problems:
            raise IncompleteRenderError(
                f"render incomplete: {', '.join(problems)}: {report.summary()}", report=report
            )
