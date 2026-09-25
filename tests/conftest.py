from __future__ import annotations

import io
import socket
import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from html import escape as html_escape
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

# Every request the test server receives: (Host header, path with query, lowercased
# headers). Auth tests tag their URLs with ?tag=... and look them up here.
REQUEST_LOG: list[tuple[str, str, dict[str, str]]] = []


def requests_tagged(tag: str) -> list[tuple[str, str, dict[str, str]]]:
    return [entry for entry in REQUEST_LOG if f"tag={tag}" in entry[1]]


_USERS = {"s3cret-alice": "alice", "s3cret-bob": "bob"}

LS_PAGE = b"""<p id="out">waiting</p><script>
const out = document.getElementById('out');
const done = t => { out.textContent = t; window.__DRAVENPDF_READY__ = true; };
const token = localStorage.getItem('token');
if (!token) done('NO TOKEN');
else fetch('/auth/api', {headers: {Authorization: 'Bearer ' + token}})
  .then(r => r.text()).then(done);
</script>"""

HEADER_PAGE = b"""<p>TENANT PAGE</p>
<img src="/auth/img.svg" onload="document.body.append(' IMG-OK')"
     onerror="document.body.append(' IMG-FAIL')">
<p id="api"></p><script>
fetch('/auth/api2').then(r => r.text()).then(t => {
  document.getElementById('api').textContent = t; window.__DRAVENPDF_READY__ = true; });
</script>"""


# IndexedDB login: /auth/idb-write?token=... stores a token, /auth/idb-page shows it.
IDB_SCRIPT = """<p id="out">waiting</p><script>
const out = document.getElementById('out');
const done = t => { out.textContent = t; window.__DRAVENPDF_READY__ = true; };
const open = indexedDB.open('app', 1);
open.onupgradeneeded = () => open.result.createObjectStore('auth');
open.onerror = () => done('IDB ERROR');
open.onsuccess = () => {
  const db = open.result;
  const token = new URLSearchParams(location.search).get('token');
  if (token) {
    const tx = db.transaction('auth', 'readwrite');
    tx.objectStore('auth').put({token}, 'session');
    tx.oncomplete = () => done('STORED');
  } else {
    const get = db.transaction('auth').objectStore('auth').get('session');
    get.onsuccess = () => done(get.result ? 'TOKEN ' + get.result.token : 'NO TOKEN');
  }
};
</script>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(
        self,
        status: int,
        body: bytes = b"",
        content_type: str = "text/html",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        headers = {k.lower(): v for k, v in self.headers.items()}
        REQUEST_LOG.append((headers.get("host", ""), self.path, headers))
        if parts.path.startswith("/auth/"):
            self._auth(parts.path, query, headers)
        elif parts.path == "/page.html":
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

    def _auth(self, path: str, query: dict[str, list[str]], headers: dict[str, str]) -> None:
        cookies = dict(
            c.strip().split("=", 1) for c in headers.get("cookie", "").split(";") if "=" in c
        )
        tenant_ok = headers.get("x-tenant") == "acme"
        if path == "/auth/cookie-page":
            user = _USERS.get(cookies.get("session", ""))
            self._send(200, f"<p>WELCOME {user}</p>".encode() if user else b"<p>LOGIN REQUIRED</p>")
        elif path == "/auth/ls-page":
            self._send(200, LS_PAGE)
        elif path == "/auth/api":
            ok = headers.get("authorization") == "Bearer ls-t0ken"
            self._send(200 if ok else 401, b"API-OK" if ok else b"API-DENIED", "text/plain")
        elif path == "/auth/header-page":
            self._send(200, HEADER_PAGE if tenant_ok else b"<p>LOGIN REQUIRED</p>")
        elif path == "/auth/img.svg":
            self._send(200 if tenant_ok else 401, SVG, "image/svg+xml")
        elif path == "/auth/api2":
            self._send(200 if tenant_ok else 401, b"API2-OK" if tenant_ok else b"NO", "text/plain")
        elif path == "/auth/cors-api":  # readable by pages on any origin
            body = b"CORS-API-OK" if tenant_ok else b"API-DENIED"
            cors = {"Access-Control-Allow-Origin": "*"}
            self._send(200 if tenant_ok else 401, body, "text/plain", cors)
        elif path in ("/auth/idb-write", "/auth/idb-page"):
            self._send(200, IDB_SCRIPT.encode())
        elif path == "/auth/echo":
            self._send(200, SVG, "image/svg+xml")
        elif path == "/auth/page-with":
            images = "".join(f'<img src="{html_escape(u)}">' for u in query.get("img", []))
            self._send(200, f"<p>PAGE WITH IMAGES</p>{images}".encode())
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
