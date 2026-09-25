"""Render reports and strict mode, with a real Chromium."""

from __future__ import annotations

import pytest

from conftest import Server
from dravenpdf import AsyncRenderer, IncompleteRenderError, RenderOptions

pytestmark = pytest.mark.browser

BROKEN_ASSETS = '<p>body</p><img src="missing.png"><link rel="stylesheet" href="missing.css">'
SCRIPT_ERRORS = "<p>body</p><script>console.error('cerr'); throw new Error('boom')</script>"


async def test_clean_render_has_an_empty_report(renderer: AsyncRenderer) -> None:
    doc = await renderer.from_html("<p>fine</p>")

    report = doc.render_report
    assert report is not None
    assert report.ok
    assert report.summary() == ""


async def test_http_errors_are_reported_once(local_renderer: AsyncRenderer, server: Server) -> None:
    doc = await local_renderer.from_html(BROKEN_ASSETS, base_url=server.url("/"))

    report = doc.render_report
    assert report is not None
    assert sorted((e.resource_type, e.status) for e in report.http_errors) == [
        ("image", 404),
        ("stylesheet", 404),
    ]
    assert report.failed_requests == []  # the aborted stylesheet isn't counted twice
    assert report.console_errors == []  # Chromium's own "Failed to load" noise is dropped
    assert not report.ok


async def test_script_errors_are_reported(renderer: AsyncRenderer) -> None:
    doc = await renderer.from_html(SCRIPT_ERRORS)

    report = doc.render_report
    assert report is not None
    assert report.page_errors == ["boom"]
    assert report.console_errors == ["cerr"]


async def test_failed_connections_are_reported() -> None:
    async with AsyncRenderer(allowed_hosts=["localhost"]) as r:
        doc = await r.from_html('<img src="http://localhost:1/x.png">')

    report = doc.render_report
    assert report is not None
    ((failed,),) = [report.failed_requests]
    assert failed.url == "http://localhost:1/x.png"
    assert failed.reason.startswith("net::ERR_")


async def test_blocked_requests_are_in_the_report(server: Server) -> None:
    async with AsyncRenderer(on_blocked="skip") as r:
        doc = await r.from_html(f'<img src="{server.url("/img.svg", host="127.0.0.1")}">')

    report = doc.render_report
    assert report is not None
    assert [url for url, _ in report.blocked] == [server.url("/img.svg", host="127.0.0.1")]
    assert report.failed_requests == []  # listed as blocked, not as failed


async def test_strict_resources(local_renderer: AsyncRenderer, server: Server) -> None:
    strict = RenderOptions(fail_on_resource_errors=True)

    with pytest.raises(IncompleteRenderError, match="2 resource") as info:
        await local_renderer.from_html(BROKEN_ASSETS, strict, base_url=server.url("/"))

    assert "HTTP 404" in info.value.message
    assert len(info.value.report.http_errors) == 2
    # Script errors alone don't trip the resource check.
    assert (await local_renderer.from_html(SCRIPT_ERRORS, strict)).page_count == 1


async def test_strict_page_errors(renderer: AsyncRenderer) -> None:
    with pytest.raises(IncompleteRenderError, match="1 script error"):
        await renderer.from_html(SCRIPT_ERRORS, RenderOptions(fail_on_page_errors=True))


async def test_derived_documents_have_no_report(renderer: AsyncRenderer) -> None:
    doc = await renderer.from_html("<p>x</p>")

    assert doc.rotate(90).render_report is None
