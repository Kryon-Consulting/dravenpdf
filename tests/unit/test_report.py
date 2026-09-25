from __future__ import annotations

import pytest

from dravenpdf import FailedRequest, HttpError, IncompleteRenderError, RenderReport
from dravenpdf.render.report import ReportCollector


def test_empty_report_is_ok() -> None:
    report = RenderReport()

    assert report.ok
    assert report.resource_problems == 0
    assert report.summary() == ""


def test_console_errors_alone_are_still_ok() -> None:
    assert RenderReport(console_errors=["noisy analytics script"]).ok


def test_summary_lists_and_truncates() -> None:
    report = RenderReport(
        http_errors=[HttpError(f"https://a.example/{i}.png", 404, "image") for i in range(4)],
        failed_requests=[FailedRequest("https://b.example/f.woff2", "net::ERR_FAILED", "font")],
        page_errors=["boom"],
        blocked=[("http://10.0.0.1/", "non-public")],
    )

    summary = report.summary(limit=3)

    assert summary.startswith("https://a.example/0.png returned HTTP 404; ")
    assert summary.endswith("; and 4 more")
    assert report.resource_problems == 5
    assert not report.ok


@pytest.mark.parametrize(
    ("report", "flags", "raises"),
    [
        (RenderReport(page_errors=["x"]), {"fail_on_resource_errors": True}, False),
        (RenderReport(page_errors=["x"]), {"fail_on_page_errors": True}, True),
        (RenderReport(http_errors=[HttpError("u", 500, "fetch")]), {}, False),
        (
            RenderReport(http_errors=[HttpError("u", 500, "fetch")]),
            {"fail_on_resource_errors": True},
            True,
        ),
        (RenderReport(blocked=[("u", "r")]), {"fail_on_resource_errors": True}, False),
    ],
)
def test_strict_check(report: RenderReport, flags: dict[str, bool], raises: bool) -> None:
    collector = ReportCollector()
    collector.report = report
    kwargs = {"fail_on_resource_errors": False, "fail_on_page_errors": False, **flags}

    if raises:
        with pytest.raises(IncompleteRenderError) as info:
            collector.check(**kwargs)
        assert info.value.code == "render_incomplete"
        assert info.value.report is report
    else:
        collector.check(**kwargs)
