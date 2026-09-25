from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pikepdf
import pytest
from fastapi.testclient import TestClient

from dravenpdf import BrowserPool, PdfDocument, RenderOptions
from dravenpdf.server.app import create_app
from dravenpdf.server.config import Settings

API_KEY = "test-key"
AUTH = {"X-API-Key": API_KEY}


def pdf_bytes(pages: int = 1, width: int = 200) -> bytes:
    pdf = pikepdf.new()
    for i in range(pages):
        pdf.add_blank_page(page_size=(width + i, 300))
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


@dataclass
class FakeRenderer:
    """Stands in for AsyncRenderer: returns ``result`` or raises ``error``."""

    result: PdfDocument = field(default_factory=lambda: PdfDocument.from_bytes(pdf_bytes()))
    error: Exception | None = None
    calls: list[tuple[str, Any, RenderOptions | None]] = field(default_factory=list)
    assets: list[dict[str, bytes] | None] = field(default_factory=list)
    auth: list[Any] = field(default_factory=list)
    pool: BrowserPool = field(default_factory=BrowserPool)
    is_running: bool = True

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def _answer(self, kind: str, source: Any, options: RenderOptions | None) -> PdfDocument:
        self.calls.append((kind, source, options))
        if self.error is not None:
            raise self.error
        return self.result

    async def from_html(
        self,
        html: str,
        options: RenderOptions | None = None,
        *,
        base_url: str | None = None,
        assets: dict[str, bytes] | None = None,
        auth: Any = None,
    ) -> PdfDocument:
        self.assets.append(assets)
        self.auth.append(auth)
        return await self._answer("html", html, options)

    async def from_url(
        self, url: str, options: RenderOptions | None = None, *, auth: Any = None
    ) -> PdfDocument:
        self.auth.append(auth)
        return await self._answer("url", url, options)

    async def from_template(
        self, template: str, data: Any, options: RenderOptions | None = None, **kwargs: Any
    ) -> PdfDocument:
        self.assets.append(kwargs.get("assets"))
        self.auth.append(kwargs.get("auth"))
        return await self._answer("template", (template, data), options)


@pytest.fixture
def fake() -> FakeRenderer:
    return FakeRenderer()


def make_client(settings: Settings, renderer: Any = None) -> TestClient:
    return TestClient(create_app(settings, renderer=renderer), raise_server_exceptions=False)


@pytest.fixture
def client(fake: FakeRenderer) -> Iterator[TestClient]:
    with make_client(Settings(api_key=API_KEY, render_timeout_ms=10_000), fake) as c:
        yield c
