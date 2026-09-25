"""HTTP API with a real Chromium."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from conftest import Server, pdf_text
from dravenpdf import PdfDocument
from dravenpdf.server.config import Settings

from .conftest import API_KEY, AUTH, make_client, pdf_bytes

pytestmark = pytest.mark.browser


@pytest.fixture
def live() -> Iterator[TestClient]:
    with make_client(Settings(api_key=API_KEY)) as c:
        yield c


def test_render_html(live: TestClient) -> None:
    footer = '<div style="font-size:10px;margin:auto">p<span class="pageNumber"></span></div>'
    response = live.post(
        "/v1/render/html",
        json={
            "html": "<h1>From the API</h1>",
            "options": {"paper": "Letter", "footer": {"html": footer}},
        },
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    doc = PdfDocument.from_bytes(response.content)
    assert "From the API" in pdf_text(doc)[0]
    assert "p1" in pdf_text(doc)[0]
    assert doc.page_size(0) == pytest.approx((612, 792), abs=1)


def test_private_url_is_blocked(live: TestClient, server: Server) -> None:
    response = live.post(
        "/v1/render/url", json={"url": server.url("/page.html", host="127.0.0.1")}, headers=AUTH
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "blocked_request"


def test_allowed_hosts_setting(server: Server) -> None:
    with make_client(Settings(api_key=API_KEY, allowed_hosts="localhost")) as c:
        response = c.post("/v1/render/url", json={"url": server.url("/page.html")}, headers=AUTH)

    assert response.status_code == 200, response.text
    assert "IMG-OK" in pdf_text(PdfDocument.from_bytes(response.content))[0]


def test_render_template(live: TestClient) -> None:
    response = live.post(
        "/v1/render/template",
        json={"template": "<p>Total: {{ total }}</p>", "data": {"total": "9.99"}},
        headers=AUTH,
    )

    assert "Total: 9.99" in pdf_text(PdfDocument.from_bytes(response.content))[0]


def test_timeout_is_504(live: TestClient) -> None:
    response = live.post(
        "/v1/render/html",
        json={"html": "<p>x</p>", "options": {"wait_for_selector": "#never", "timeout_ms": 1000}},
        headers=AUTH,
    )

    assert response.status_code == 504


def test_stamp_html(live: TestClient) -> None:
    response = live.post(
        "/v1/pdf/stamp",
        files={"file": ("in.pdf", pdf_bytes(2), "application/pdf")},
        data={"html": "<p style='font-size:30px'>HTML MARK</p>", "pages": "2"},
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    texts = pdf_text(PdfDocument.from_bytes(response.content))
    assert "HTML MARK" not in texts[0]
    assert "HTML MARK" in texts[1]


def test_readyz_with_real_browser(live: TestClient) -> None:
    assert live.get("/readyz").json()["status"] == "ready"


def test_render_bundle(live: TestClient) -> None:
    from conftest import SVG

    response = live.post(
        "/v1/render/bundle",
        files=[
            ("files", ("index.html", b'<link rel="stylesheet" href="css/site.css"><h1>Bundled</h1>'
                       b'<img src="img/logo.svg" onload="document.body.append(\'IMG-OK\')">'
                       b'<img src="img/missing.png">', "text/html")),
            ("files", ("css/site.css", b'h1::after { content: " CSS-OK"; }', "text/css")),
            ("files", ("img/logo.svg", SVG, "image/svg+xml")),
        ],
        headers=AUTH,
    )  # fmt: skip

    assert response.status_code == 200, response.text
    text = pdf_text(PdfDocument.from_bytes(response.content))[0]
    assert "Bundled CSS-OK" in text
    assert "IMG-OK" in text
    assert response.headers["x-dravenpdf-resource-errors"] == "1"  # img/missing.png


def test_render_url_behind_a_login(server: Server, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    origin = server.url("", host="localhost")
    body = {
        "url": server.url("/auth/cookie-page"),
        "auth": {
            "cookies": [{"name": "session", "value": "s3cret-alice", "url": origin}],
            "headers": {origin: {"X-Tenant": "acme"}},
        },
    }

    with make_client(Settings(api_key=API_KEY, allowed_hosts="localhost")) as c:
        logged_in = c.post("/v1/render/url", json=body, headers=AUTH)
        anonymous = c.post("/v1/render/url", json={"url": body["url"]}, headers=AUTH)

    assert logged_in.status_code == 200, logged_in.text
    assert "WELCOME alice" in pdf_text(PdfDocument.from_bytes(logged_in.content))[0]
    assert "LOGIN REQUIRED" in pdf_text(PdfDocument.from_bytes(anonymous.content))[0]
    assert "s3cret-alice" not in str(logged_in.headers)
    assert "s3cret-alice" not in caplog.text
