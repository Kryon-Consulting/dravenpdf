"""The report builder sends only local, referenced assets to the PDF service."""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.build_platform_report import collect_assets, render_report


def test_collect_assets_uses_referenced_images_and_rejects_missing_files(tmp_path: Path) -> None:
    (tmp_path / "images").mkdir()
    (tmp_path / "images" / "dashboard.png").write_bytes(b"PNG")
    (tmp_path / "images" / "unused.png").write_bytes(b"unused")
    html = '<img src="images/dashboard.png"><img src="data:image/png;base64,AAAA">'

    assert collect_assets(html, tmp_path) == {"images/dashboard.png": b"PNG"}
    with pytest.raises(ValueError, match="missing asset"):
        collect_assets('<img src="images/absent.png">', tmp_path)
    with pytest.raises(ValueError, match="outside"):
        collect_assets('<img src="../secret.png">', tmp_path)


def test_render_report_posts_html_assets_and_strict_options() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, headers={"Content-Type": "application/pdf"}, content=b"%PDF-1.4\n"
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = render_report(
            client,
            "http://127.0.0.1:8000",
            "test-key",
            '<img src="images/dashboard.png">',
            {"images/dashboard.png": b"PNG"},
        )

    assert result.startswith(b"%PDF-")
    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/v1/render/bundle"
    assert request.headers["X-API-Key"] == "test-key"
    body = request.read()
    assert b"index.html" in body
    assert b"images/dashboard.png" in body
    assert b"fail_on_resource_errors" in body


def test_render_report_rejects_reported_resource_failures() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "application/pdf", "X-DravenPdf-Resource-Errors": "1"},
            content=b"%PDF-1.4\n",
        )

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ValueError, match="resource error"),
    ):
        render_report(client, "http://127.0.0.1:8000", "key", "<h1>Test</h1>", {})
