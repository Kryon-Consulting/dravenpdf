from __future__ import annotations

import io
import socket
import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pikepdf
import pypdfium2 as pdfium
import pytest

from dravenpdf import AsyncRenderer, PdfDocument

SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
    b'<rect width="10" height="10" fill="red"/></svg>'
)

# The page appends IMG-OK to its body once the image loads, so tests can see it in the PDF.
PAGE_WITH_IMAGE = b"""<!doctype html><html><head><title>t</title></head><body>
<h1>Served page</h1>
<img src="/img.svg" onload="document.body.append('IMG-OK')"
     onerror="document.body.append('IMG-FAIL')">
</body></html>"""


# ---------------------------------------------------------------- PDF helpers


def pdf_text(doc: PdfDocument) -> list[str]:
    """Text of each page."""
    pdf = pdfium.PdfDocument(doc.to_bytes())
    try:
        return [page.get_textpage().get_text_range() for page in pdf]
    finally:
        pdf.close()


def page_size(doc: PdfDocument, index: int = 0) -> tuple[float, float]:
    """(width, height) of a page in points."""
    with pikepdf.open(io.BytesIO(doc.to_bytes())) as pdf:
        box = [float(v) for v in pdf.pages[index].mediabox]
    return box[2] - box[0], box[3] - box[1]


# ---------------------------------------------------------------- local HTTP server


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(self, status: int, body: bytes = b"", content_type: str = "text/html") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if parts.path == "/page.html":
            self._send(200, PAGE_WITH_IMAGE)
        elif parts.path == "/img.svg":
            self._send(200, SVG, "image/svg+xml")
        elif parts.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", query["to"][0])
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif parts.path == "/secret":
            self._send(200, b"<p>SECRET</p>")
        else:
            self._send(404, b"not found")


class _DualStackServer(ThreadingHTTPServer):
    # Serve both 127.0.0.1 and ::1, because "localhost" may resolve to either.
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self) -> None:
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


@dataclass(frozen=True)
class Server:
    port: int

    def url(self, path: str, host: str = "localhost") -> str:
        return f"http://{host}:{self.port}{path}"


@pytest.fixture(scope="session")
def server() -> Iterator[Server]:
    httpd: ThreadingHTTPServer
    try:
        httpd = _DualStackServer(("::", 0), _Handler)
    except OSError:  # no IPv6 on this machine, so "localhost" can only mean 127.0.0.1
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield Server(port=httpd.server_address[1])
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------- renderers


@pytest.fixture
async def renderer() -> AsyncIterator[AsyncRenderer]:
    """Default settings: private network blocked, failing on blocked requests."""
    async with AsyncRenderer() as r:
        yield r


@pytest.fixture
async def local_renderer() -> AsyncIterator[AsyncRenderer]:
    """May load from the local test server through the "localhost" name only."""
    async with AsyncRenderer(allowed_hosts=["localhost"]) as r:
        yield r
