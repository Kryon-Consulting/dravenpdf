"""POST /v1/render/*: HTML, URLs and templates to PDF (JSON bodies)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from dravenpdf.server.deps import (
    PDF_RESPONSE,
    clamp_timeout,
    pdf_response,
    renderer_of,
    require_api_key,
    settings_of,
    timed,
)
from dravenpdf.server.schemas import RenderHtmlRequest, RenderTemplateRequest, RenderUrlRequest

router = APIRouter(prefix="/v1/render", tags=["render"], dependencies=[Depends(require_api_key)])


@router.post("/html", response_class=Response, responses=PDF_RESPONSE)
async def render_html(body: RenderHtmlRequest, request: Request) -> Response:
    """Render an HTML string. `base_url` resolves relative links and assets."""
    options = clamp_timeout(body.options, settings_of(request))
    doc = await timed(
        request, "html", renderer_of(request).from_html(body.html, options, base_url=body.base_url)
    )
    return await pdf_response(doc, body.filename, body.post)


@router.post("/url", response_class=Response, responses=PDF_RESPONSE)
async def render_url(body: RenderUrlRequest, request: Request) -> Response:
    """Render a public web page."""
    options = clamp_timeout(body.options, settings_of(request))
    doc = await timed(request, "url", renderer_of(request).from_url(body.url, options))
    return await pdf_response(doc, body.filename, body.post)


@router.post("/template", response_class=Response, responses=PDF_RESPONSE)
async def render_template(body: RenderTemplateRequest, request: Request) -> Response:
    """Render a Jinja2 template source with `data` (sandboxed, autoescaped)."""
    options = clamp_timeout(body.options, settings_of(request))
    renderer = renderer_of(request)
    work = renderer.from_template(body.template, body.data, options, base_url=body.base_url)
    doc = await timed(request, "template", work)
    return await pdf_response(doc, body.filename, body.post)
