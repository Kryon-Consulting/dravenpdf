"""HTTP API without a browser (renders use a fake renderer)."""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from conftest import pdf_text
from dravenpdf import (
    BlockedRequestError,
    HttpError,
    IncompleteRenderError,
    PdfDocument,
    PoolExhaustedError,
    RenderError,
    RenderReport,
    RenderTimeoutError,
    TemplateError,
)
from dravenpdf.server import deps
from dravenpdf.server.config import Settings

from .conftest import API_KEY, AUTH, FakeRenderer, make_client, pdf_bytes


def upload(data: bytes, name: str = "in.pdf") -> tuple[str, bytes, str]:
    return (name, data, "application/pdf")


def as_doc(content: bytes) -> PdfDocument:
    return PdfDocument.from_bytes(content)


def png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (40, 20), (255, 0, 0, 255)).save(buffer, "PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------- settings and auth


def test_settings_require_an_api_key() -> None:
    with pytest.raises(ValidationError, match="DRAVENPDF_API_KEY"):
        Settings()
    assert Settings(auth_disabled=True).api_key is None


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRAVENPDF_API_KEY", "abc")
    monkeypatch.setenv("DRAVENPDF_ALLOWED_HOSTS", " cdn.example.com, *.example.org ,")
    monkeypatch.setenv("DRAVENPDF_MAX_CONCURRENCY", "2")

    settings = Settings()

    assert settings.api_key is not None
    assert settings.api_key.get_secret_value() == "abc"
    assert settings.allowed_host_list == ["cdn.example.com", "*.example.org"]
    assert settings.max_concurrency == 2


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"X-API-Key": ""}])
def test_auth_required(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post("/v1/render/html", json={"html": "<p>x</p>"}, headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_auth_disabled(fake: FakeRenderer) -> None:
    with make_client(Settings(auth_disabled=True), fake) as c:
        assert c.post("/v1/render/html", json={"html": "<p>x</p>"}).status_code == 200


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/metrics"])
def test_operational_endpoints_need_no_key(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 200


def test_readyz_reports_starting(fake: FakeRenderer) -> None:
    fake.is_running = False
    with make_client(Settings(api_key=API_KEY), fake) as c:
        assert c.get("/readyz").status_code == 503


def test_metrics(client: TestClient) -> None:
    client.post("/v1/render/html", json={"html": "<p>x</p>"}, headers=AUTH)

    body = client.get("/metrics").text

    assert "dravenpdf_renders_active 0.0" in body
    assert 'dravenpdf_render_seconds_count{source="html"} 1.0' in body


def test_request_id(client: TestClient) -> None:
    generated = client.get("/healthz").headers["x-request-id"]
    kept = client.get("/healthz", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"]

    assert len(generated) == 32
    assert kept == "abc-123"


# ---------------------------------------------------------------- limits and errors


def test_body_limit_with_content_length(fake: FakeRenderer) -> None:
    with make_client(Settings(api_key=API_KEY, max_body_mb=0.01), fake) as c:
        response = c.post("/v1/render/html", json={"html": "x" * 20_000}, headers=AUTH)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_body_limit_when_streamed(fake: FakeRenderer) -> None:
    def chunks():  # no Content-Length: the size is only known while reading
        for _ in range(20):
            yield b"x" * 1000

    with make_client(Settings(api_key=API_KEY, max_body_mb=0.01), fake) as c:
        response = c.post(
            "/v1/pdf/compress",
            content=chunks(),
            headers={**AUTH, "Content-Type": "multipart/form-data; boundary=zzz"},
        )

    assert response.status_code == 413


def test_validation_error_is_400(client: TestClient) -> None:
    response = client.post(
        "/v1/render/html", json={"html": "<p>x</p>", "options": {"paper": "A9"}}, headers=AUTH
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert "options.paper" in error["message"]


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (PoolExhaustedError("full"), 503, "busy"),
        (RenderTimeoutError("slow", timeout_ms=5), 504, "render_timeout"),
        (BlockedRequestError("no", url="http://10.0.0.1"), 422, "blocked_request"),
        (RenderError("HTTP 404"), 422, "render_failed"),
        (TemplateError("bad"), 400, "invalid_template"),
        (IncompleteRenderError("missing", report=RenderReport()), 422, "render_incomplete"),
        (RuntimeError("boom"), 500, "internal_error"),
    ],
)
def test_error_mapping(
    client: TestClient, fake: FakeRenderer, error: Exception, status: int, code: str
) -> None:
    fake.error = error

    response = client.post("/v1/render/html", json={"html": "<p>x</p>"}, headers=AUTH)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    if code == "busy":
        assert response.headers["retry-after"] == "5"
    if code == "internal_error":
        assert response.headers["x-request-id"] in response.json()["error"]["message"]
        assert "boom" not in response.json()["error"]["message"]


# ---------------------------------------------------------------- render endpoints


def test_render_clamps_timeout_and_names_file(client: TestClient, fake: FakeRenderer) -> None:
    response = client.post(
        "/v1/render/url",
        json={
            "url": "https://example.com",
            "options": {"timeout_ms": 60_000, "landscape": True},
            "filename": "../My Report",
        },
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'inline; filename="My_Report.pdf"'
    kind, source, options = fake.calls[0]
    assert (kind, source) == ("url", "https://example.com")
    assert options is not None
    assert options.timeout_ms == 10_000
    assert options.landscape


def test_render_post_processing(client: TestClient) -> None:
    response = client.post(
        "/v1/render/template",
        json={
            "template": "<p>{{ x }}</p>",
            "data": {"x": 1},
            "post": {
                "stamp_text": {"text": "PAID", "angle": 0},
                "metadata": {"title": "Invoice"},
                "compress": True,
            },
        },
        headers=AUTH,
    )

    doc = as_doc(response.content)
    assert "PAID" in pdf_text(doc)[0]
    assert doc.metadata["title"] == "Invoice"


def test_render_rejects_unknown_fields(client: TestClient) -> None:
    response = client.post("/v1/render/html", json={"html": "x", "htlm": "y"}, headers=AUTH)

    assert response.status_code == 400


# ---------------------------------------------------------------- PDF endpoints


def test_merge(client: TestClient) -> None:
    files = [("files", upload(pdf_bytes(2))), ("files", upload(pdf_bytes(1)))]

    response = client.post("/v1/pdf/merge", files=files, headers=AUTH)

    assert response.status_code == 200
    assert as_doc(response.content).page_count == 3


def test_merge_needs_two(client: TestClient) -> None:
    response = client.post("/v1/pdf/merge", files=[("files", upload(pdf_bytes()))], headers=AUTH)

    assert response.status_code == 400


def test_non_pdf_upload_is_415(client: TestClient) -> None:
    response = client.post(
        "/v1/pdf/compress", files={"file": ("a.txt", b"hello", "text/plain")}, headers=AUTH
    )

    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_media_type"


def test_broken_pdf_is_422(client: TestClient) -> None:
    response = client.post(
        "/v1/pdf/compress", files={"file": upload(b"%PDF-1.7\ngarbage")}, headers=AUTH
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_pdf"


def zip_names(content: bytes) -> list[str]:
    return zipfile.ZipFile(io.BytesIO(content)).namelist()


def test_split_every_and_ranges(client: TestClient) -> None:
    every = client.post(
        "/v1/pdf/split", files={"file": upload(pdf_bytes(5))}, data={"every": 2}, headers=AUTH
    )
    ranges = client.post(
        "/v1/pdf/split",
        files={"file": upload(pdf_bytes(5))},
        data={"ranges": ["1-2", "3-"]},
        headers=AUTH,
    )

    assert zip_names(every.content) == ["part-1.pdf", "part-2.pdf", "part-3.pdf"]
    assert zip_names(ranges.content) == ["part-1.pdf", "part-2.pdf"]


@pytest.mark.parametrize(
    ("path", "data", "pages", "first_width"),
    [
        ("/v1/pdf/extract", {"ranges": "2-3"}, 2, 201),
        ("/v1/pdf/delete", {"pages": "1"}, 2, 201),
        ("/v1/pdf/reorder", {"order": "3,1,2"}, 3, 202),
        ("/v1/pdf/rotate", {"degrees": "90", "pages": "1"}, 3, 300),
    ],
)
def test_page_operations(
    client: TestClient, path: str, data: dict[str, str], pages: int, first_width: int
) -> None:
    response = client.post(path, files={"file": upload(pdf_bytes(3))}, data=data, headers=AUTH)

    assert response.status_code == 200, response.text
    doc = as_doc(response.content)
    assert doc.page_count == pages
    assert int(doc.page_size(0)[0]) == first_width


def test_bad_page_range_is_400(client: TestClient) -> None:
    response = client.post(
        "/v1/pdf/extract", files={"file": upload(pdf_bytes(2))}, data={"ranges": "5"}, headers=AUTH
    )

    assert response.status_code == 400
    assert "past the last page" in response.json()["error"]["message"]


def test_metadata_and_compress(client: TestClient) -> None:
    meta = client.post(
        "/v1/pdf/metadata",
        files={"file": upload(pdf_bytes())},
        data={"title": "T", "author": "A"},
        headers=AUTH,
    )
    packed = client.post("/v1/pdf/compress", files={"file": ("in.pdf", meta.content)}, headers=AUTH)

    assert as_doc(packed.content).metadata == {"title": "T", "author": "A"}


def test_stamp_text_and_image(client: TestClient) -> None:
    text = client.post(
        "/v1/pdf/stamp",
        files={"file": upload(pdf_bytes(2))},
        data={"text": "DRAFT", "pages": "2", "angle": "0"},
        headers=AUTH,
    )
    image = client.post(
        "/v1/pdf/stamp",
        files={"file": upload(pdf_bytes()), "image": ("logo.png", png(), "image/png")},
        data={"position": "top-right", "width": "50"},
        headers=AUTH,
    )

    assert [("DRAFT" in t) for t in pdf_text(as_doc(text.content))] == [False, True]
    assert image.status_code == 200


@pytest.mark.parametrize(
    "data",
    [{}, {"text": "A", "html": "<p>B</p>"}, {"text": "機密"}, {"text": "A", "position": "x"}],
)
def test_stamp_bad_requests(client: TestClient, data: dict[str, str]) -> None:
    response = client.post(
        "/v1/pdf/stamp", files={"file": upload(pdf_bytes())}, data=data, headers=AUTH
    )

    assert response.status_code == 400


# ---------------------------------------------------------------- convert endpoints


def test_images_to_pdf_and_back(client: TestClient) -> None:
    made = client.post(
        "/v1/convert/images-to-pdf",
        files=[("files", ("a.png", png(), "image/png"))] * 2,
        data={"paper": "A4", "margin": "36"},
        headers=AUTH,
    )
    assert as_doc(made.content).page_count == 2

    images = client.post(
        "/v1/convert/pdf-to-images",
        files={"file": ("in.pdf", made.content)},
        data={"dpi": "30", "format": "jpeg", "pages": "2"},
        headers=AUTH,
    )
    assert zip_names(images.content) == ["page-2.jpg"]


def test_pdf_to_images_limits(fake: FakeRenderer) -> None:
    # pdf_bytes pages are 200 x 300 pt: 600 x 900 = 540,000 pixels at 216 dpi.
    settings = Settings(api_key=API_KEY, max_image_megapixels=0.5, max_output_mb=0.001)
    with make_client(settings, fake) as c:
        too_many_pixels = c.post(
            "/v1/convert/pdf-to-images",
            files={"file": upload(pdf_bytes(1))},
            data={"dpi": "216"},
            headers=AUTH,
        )
        too_many_bytes = c.post(
            "/v1/convert/pdf-to-images",
            files={"file": upload(pdf_bytes(40))},
            data={"dpi": "10"},
            headers=AUTH,
        )

    assert too_many_pixels.status_code == 422
    assert too_many_pixels.json()["error"]["code"] == "limit_exceeded"
    assert "540,000 pixels" in too_many_pixels.json()["error"]["message"]
    assert too_many_bytes.status_code == 422
    assert too_many_bytes.json()["error"]["code"] == "limit_exceeded"


def test_split_output_limit(fake: FakeRenderer) -> None:
    source = pdf_bytes(1)
    settings = Settings(api_key=API_KEY, max_output_mb=len(source) * 3.5 / 1024 / 1024)
    with make_client(settings, fake) as c:
        ok = c.post(
            "/v1/pdf/split",
            files={"file": upload(source)},
            data={"ranges": ["1"] * 2},
            headers=AUTH,
        )
        too_big = c.post(
            "/v1/pdf/split",
            files={"file": upload(source)},
            data={"ranges": ["1"] * 50},
            headers=AUTH,
        )

    assert zip_names(ok.content) == ["part-1.pdf", "part-2.pdf"]
    assert int(ok.headers["content-length"]) == len(ok.content)
    assert too_big.status_code == 422
    assert too_big.json()["error"]["code"] == "limit_exceeded"


def test_zip_spilled_to_disk_streams_whole(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deps, "_ZIP_SPOOL_MEMORY_BYTES", 1)

    response = client.post(
        "/v1/pdf/split", files={"file": upload(pdf_bytes(5))}, data={"every": 1}, headers=AUTH
    )

    assert zip_names(response.content) == [f"part-{i}.pdf" for i in range(1, 6)]


def test_text(client: TestClient) -> None:
    stamped = as_doc(pdf_bytes(2)).stamp_text("hello", pages=[0]).to_bytes()

    response = client.post("/v1/convert/text", files={"file": upload(stamped)}, headers=AUTH)

    assert response.json()["pages"][0].strip() == "hello"
    assert response.json()["pages"][1] == ""


def test_openapi_lists_every_endpoint(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert len([p for p in paths if p.startswith("/v1/")]) == 21


def test_split_holds_one_part_at_a_time(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from dravenpdf.document import pages

    created: list[object] = []
    still_held: list[int] = []
    original = pages._from_pages

    def tracked(source: object, indices: object) -> object:
        # Before building a part, count earlier parts that something other than this
        # list still references (the list, the loop variable and the call's own
        # argument account for 3 references).
        still_held.append(sum(1 for part in created if sys.getrefcount(part) > 3))
        part = original(source, indices)  # type: ignore[arg-type]
        created.append(part)
        return part

    monkeypatch.setattr(pages, "_from_pages", tracked)
    response = client.post(
        "/v1/pdf/split", files={"file": upload(pdf_bytes(6))}, data={"every": 1}, headers=AUTH
    )

    assert response.status_code == 200
    assert len(zip_names(response.content)) == 6
    assert still_held == [0] * 6


def test_render_report_headers(client: TestClient, fake: FakeRenderer) -> None:
    fake.result.render_report = RenderReport(
        http_errors=[HttpError("https://a.example/x.png", 404, "image")],
        page_errors=["boom", "bang"],
    )

    response = client.post("/v1/render/html", json={"html": "<p>x</p>"}, headers=AUTH)

    assert response.headers["x-dravenpdf-resource-errors"] == "1"
    assert response.headers["x-dravenpdf-page-errors"] == "2"
    assert response.headers["x-dravenpdf-blocked"] == "0"


def test_non_render_responses_have_no_report_headers(client: TestClient) -> None:
    response = client.post("/v1/pdf/compress", files={"file": upload(pdf_bytes())}, headers=AUTH)

    assert "x-dravenpdf-resource-errors" not in response.headers


# ---------------------------------------------------------------- bundles


def asset(path: str, data: bytes = b"x") -> tuple[str, tuple[str, bytes, str]]:
    return ("files", (path, data, "application/octet-stream"))


def test_bundle_with_html_field(client: TestClient, fake: FakeRenderer) -> None:
    response = client.post(
        "/v1/render/bundle",
        data={
            "html": "<p>doc</p>",
            "options": '{"landscape": true, "timeout_ms": 60000}',
            "filename": "My Report",
        },
        files=[asset("css/site.css", b"h1{}"), asset("img/logo.png", b"png")],
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"] == 'inline; filename="My_Report.pdf"'
    kind, source, options = fake.calls[0]
    assert (kind, source) == ("html", "<p>doc</p>")
    assert options is not None
    assert options.landscape
    assert options.timeout_ms == 10_000  # clamped like the JSON endpoints
    assert fake.assets[0] == {"css/site.css": b"h1{}", "img/logo.png": b"png"}


def test_bundle_with_index_file_and_template_data(client: TestClient, fake: FakeRenderer) -> None:
    response = client.post(
        "/v1/render/bundle",
        data={"data": '{"name": "Ada"}'},
        files=[asset("index.html", b"<p>{{ name }}</p>"), asset("a.css")],
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    kind, source, _ = fake.calls[0]
    assert (kind, source) == ("template", ("<p>{{ name }}</p>", {"name": "Ada"}))
    assert fake.assets[0] == {"a.css": b"x"}


@pytest.mark.parametrize(
    ("data", "files", "message"),
    [
        ({}, [asset("a.css")], "index.html"),
        ({"html": "x"}, [asset("a.css"), asset("a.css")], "duplicate"),
        ({"html": "x"}, [asset("../secret.css")], "'..'"),
        ({"html": "x"}, [asset("/etc/passwd")], "relative"),
        ({"html": "x", "data": "[1]"}, [], "JSON object"),
        ({"html": "x", "data": "{nope"}, [], "not valid JSON"),
        ({"html": "x", "options": '{"paper": "A9"}'}, [], "options: paper"),
        ({"html": "x", "post": '{"compress": "maybe"}'}, [], "post: compress"),
    ],
)
def test_bundle_bad_requests(
    client: TestClient,
    fake: FakeRenderer,
    data: dict[str, str],
    files: list[tuple[str, tuple[str, bytes, str]]],
    message: str,
) -> None:
    response = client.post("/v1/render/bundle", data=data, files=files or None, headers=AUTH)

    assert response.status_code == 400, response.text
    assert message in response.json()["error"]["message"]
    assert fake.calls == []  # rejected before any render


# ---------------------------------------------------------------- page credentials


def test_render_url_passes_auth(client: TestClient, fake: FakeRenderer) -> None:
    response = client.post(
        "/v1/render/url",
        json={
            "url": "https://app.example.com/report",
            "auth": {
                "cookies": [{"name": "sid", "value": "s3cret", "url": "https://app.example.com"}],
                "headers": {"https://api.example.com": {"Authorization": "Bearer s3cret"}},
                "storage_state": {"origins": [{"origin": "https://app.example.com",
                                               "localStorage": [{"name": "t", "value": "s3"}]}]},
            },
        },
        headers=AUTH,
    )  # fmt: skip

    assert response.status_code == 200, response.text
    (auth,) = fake.auth
    assert auth.cookies[0].name == "sid"
    assert auth.headers_for("https://api.example.com/x") == {"authorization": "Bearer s3cret"}
    assert "s3cret" not in str(response.headers)


@pytest.mark.parametrize(
    ("auth", "message"),
    [
        ({"headers": {"https://a.example": {"X": "s3cret\r\nX-Evil: 1"}}}, "control"),
        ({"headers": {"https://a.example": {"Cookie": "sid=s3cret"}}}, "use cookies"),
        ({"headers": {"https://a.example/path": {"X": "s3cret"}}}, "origin"),
        ({"storage_state": "/var/state.json"}, "storage_state"),
        ({"cookies": [{"name": "sid", "value": "s3cret"}]}, "url or domain"),
    ],
)
def test_render_url_rejects_bad_auth_without_echoing_it(
    client: TestClient, fake: FakeRenderer, auth: dict[str, object], message: str
) -> None:
    response = client.post(
        "/v1/render/url", json={"url": "https://a.example/", "auth": auth}, headers=AUTH
    )

    assert response.status_code == 400
    assert message in response.json()["error"]["message"]
    assert "s3cret" not in response.text
    assert fake.calls == []


def test_auth_only_on_the_url_endpoint(client: TestClient) -> None:
    response = client.post(
        "/v1/render/html", json={"html": "x", "auth": {"cookies": []}}, headers=AUTH
    )

    assert response.status_code == 400


# ---------------------------------------------------------------- encryption


def test_encrypt_and_decrypt_endpoints(client: TestClient) -> None:
    import pikepdf

    encrypted = client.post(
        "/v1/pdf/encrypt",
        files={"file": upload(pdf_bytes(2))},
        data={"user_password": "pw-123", "allow_copy": "false"},
        headers=AUTH,
    )
    assert encrypted.status_code == 200, encrypted.text
    assert not pikepdf.open(io.BytesIO(encrypted.content), password="pw-123").allow.extract

    locked = client.post(
        "/v1/pdf/rotate", files={"file": ("e.pdf", encrypted.content)}, headers=AUTH
    )
    wrong = client.post(
        "/v1/pdf/decrypt",
        files={"file": ("e.pdf", encrypted.content)},
        data={"password": "pw-999"},
        headers=AUTH,
    )
    decrypted = client.post(
        "/v1/pdf/decrypt",
        files={"file": ("e.pdf", encrypted.content)},
        data={"password": "pw-123"},
        headers=AUTH,
    )

    assert (locked.status_code, locked.json()["error"]["code"]) == (422, "pdf_password")
    assert wrong.status_code == 422
    assert "pw-999" not in wrong.text
    assert as_doc(decrypted.content).page_count == 2


def test_encrypt_needs_a_password_or_restriction(client: TestClient) -> None:
    response = client.post("/v1/pdf/encrypt", files={"file": upload(pdf_bytes())}, headers=AUTH)

    assert response.status_code == 400


# ---------------------------------------------------------------- forms


def test_form_endpoints(client: TestClient) -> None:
    import json as jsonlib

    from forms_fixture import build_form

    form = ("form.pdf", build_form(), "application/pdf")
    listed = client.post("/v1/pdf/form/fields", files={"file": form}, headers=AUTH)
    filled = client.post(
        "/v1/pdf/form/fill",
        files={"file": form},
        data={"values": jsonlib.dumps({"name": "Ada", "agree": True, "size": "L"}),
              "flatten": "true"},
        headers=AUTH,
    )  # fmt: skip
    flattened = client.post("/v1/pdf/form/flatten", files={"file": form}, headers=AUTH)

    names = {f["name"]: f for f in listed.json()["fields"]}
    assert names["size"]["kind"] == "radio"
    assert sorted(names["size"]["options"]) == ["L", "M", "S"]
    assert filled.status_code == 200, filled.text
    assert "Ada" in pdf_text(as_doc(filled.content))[0]
    assert as_doc(filled.content).form_fields() == []
    assert as_doc(flattened.content).form_fields() == []


@pytest.mark.parametrize(
    ("values", "status", "message"),
    [
        ("{nope", 400, "not valid JSON"),
        ('["a"]', 400, "must be an object"),
        ('{"name": {"x": 1}}', 400, "must be an object"),
        ('{"size": "XL"}', 400, "needs one of its options"),
        ('{"missing": "x"}', 400, "no such form field"),
    ],
)
def test_form_fill_errors(client: TestClient, values: str, status: int, message: str) -> None:
    from forms_fixture import build_form

    response = client.post(
        "/v1/pdf/form/fill",
        files={"file": ("f.pdf", build_form(), "application/pdf")},
        data={"values": values},
        headers=AUTH,
    )

    assert response.status_code == status
    assert message in response.json()["error"]["message"]
