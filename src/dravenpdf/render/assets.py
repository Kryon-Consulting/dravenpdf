"""Serving an HTML document and its assets from memory.

``from_html(html, assets={...})`` loads the document from a private origin that only
exists inside the render (``https://bundle.dravenpdf.invalid/``; ``.invalid`` is a
reserved domain, so it can never reach a real host). The request guard answers every
request for that origin from the bundle and never sends it to the network, so
relative links like ``css/site.css`` or ``../img/logo.png`` resolve to bundle files,
and anything not in the bundle is a 404 (which shows up in the render report).
"""

from __future__ import annotations

import mimetypes
import posixpath
from collections.abc import Mapping
from urllib.parse import unquote, urlsplit

from playwright.async_api import Route

from dravenpdf.errors import AssetError

ORIGIN = "https://bundle.dravenpdf.invalid"
HOST = "bundle.dravenpdf.invalid"
MAX_FILES = 1000
MAX_PATH_LENGTH = 255

# mimetypes doesn't know every web type on every platform.
_TYPES = {
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".svg": "image/svg+xml",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".json": "application/json",
    ".webp": "image/webp",
    ".avif": "image/avif",
}


def check_path(path: str) -> str:
    """Validate a bundle path like ``css/site.css`` and return it unchanged.

    Paths are relative, use ``/``, and must not contain ``..``, empty or ``.``
    segments, backslashes or control characters.
    """
    if not path or len(path) > MAX_PATH_LENGTH:
        raise AssetError(f"asset path must be 1-{MAX_PATH_LENGTH} characters: {path!r}")
    if "\\" in path or any(ord(c) < 32 for c in path):
        raise AssetError(f"asset path may not contain backslashes or control characters: {path!r}")
    if path.startswith("/") or ":" in path.split("/", 1)[0]:
        raise AssetError(f"asset path must be relative: {path!r}")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise AssetError(f"asset path may not contain empty, '.' or '..' parts: {path!r}")
    return path


def content_type(path: str) -> str:
    extension = posixpath.splitext(path)[1].lower()
    return _TYPES.get(extension) or mimetypes.guess_type(path)[0] or "application/octet-stream"


class AssetBundle:
    """An HTML document plus the files it refers to, keyed by relative path."""

    def __init__(self, html: str, assets: Mapping[str, bytes]) -> None:
        if len(assets) > MAX_FILES:
            raise AssetError(f"too many assets: {len(assets)} (the limit is {MAX_FILES})")
        self.html = html.encode("utf-8")
        self.files = {check_path(path): bytes(data) for path, data in assets.items()}

    @property
    def document_url(self) -> str:
        return ORIGIN + "/"

    @staticmethod
    def owns(url: str) -> bool:
        parts = urlsplit(url)
        return parts.scheme == "https" and (parts.hostname or "").lower() == HOST

    def lookup(self, url: str) -> tuple[bytes, str] | None:
        """(body, content type) for a URL on the bundle origin, or None if missing."""
        path = unquote(urlsplit(url).path)
        if path in ("", "/"):
            return self.html, "text/html; charset=utf-8"
        # Resolve "a/../b" the way the browser already has; "/.." can't climb out.
        key = posixpath.normpath(path).lstrip("/")
        data = self.files.get(key)
        return None if data is None else (data, content_type(key))

    async def fulfill(self, route: Route) -> None:
        found = self.lookup(route.request.url)
        if found is None:
            await route.fulfill(status=404, body=b"not in the asset bundle")
            return
        body, kind = found
        await route.fulfill(
            status=200, body=body, headers={"Content-Type": kind, "Cache-Control": "no-store"}
        )
