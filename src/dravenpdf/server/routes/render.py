"""POST /v1/render/*: HTML, URLs and templates to PDF (JSON bodies)."""

from __future__ import annotations

import json
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError

from dravenpdf.options import RenderOptions
from dravenpdf.render.assets import check_path
from dravenpdf.render.auth import RenderAuth
from dravenpdf.server.deps import (
    PDF_RESPONSE,
    clamp_timeout,
    pdf_response,
    renderer_of,
    require_api_key,
    settings_of,
    timed,
)
from dravenpdf.server.errors import ApiError, describe_validation_errors
from dravenpdf.server.schemas import (
    PostProcess,
    RenderHtmlRequest,
    RenderTemplateRequest,
    RenderUrlRequest,
    safe_filename,
)

M = TypeVar("M", bound=BaseModel)

router = APIRouter(prefix="/v1/render", tags=["render"], dependencies=[Depends(require_api_key)])


@router.post("/html", response_class=Response, responses=PDF_RESPONSE)
async def render_html(body: RenderHtmlRequest, request: Request) -> Response:
    """Render an HTML string. `base_url` resolves relative links and assets; `auth`
    supplies credentials for the remote resources the page loads."""
    options = clamp_timeout(body.options, settings_of(request))
    work = renderer_of(request).from_html(
        body.html, options, base_url=body.base_url, auth=body.auth
    )
    doc = await timed(request, "html", work)
    return await pdf_response(doc, body.filename, body.post)


@router.post("/url", response_class=Response, responses=PDF_RESPONSE)
async def render_url(body: RenderUrlRequest, request: Request) -> Response:
    """Render a web page. `auth` supplies cookies, storage state and per-origin
    headers for pages behind a login; they apply to this render only."""
    options = clamp_timeout(body.options, settings_of(request))
    work = renderer_of(request).from_url(body.url, options, auth=body.auth)
    doc = await timed(request, "url", work)
    return await pdf_response(doc, body.filename, body.post)


@router.post("/template", response_class=Response, responses=PDF_RESPONSE)
async def render_template(body: RenderTemplateRequest, request: Request) -> Response:
    """Render a Jinja2 template source with `data` (sandboxed, autoescaped). `auth`
    supplies credentials for the remote resources the page loads."""
    options = clamp_timeout(body.options, settings_of(request))
    renderer = renderer_of(request)
    work = renderer.from_template(
        body.template, body.data, options, base_url=body.base_url, auth=body.auth
    )
    doc = await timed(request, "template", work)
    return await pdf_response(doc, body.filename, body.post)


def _json_field(model: type[M], raw: str | None, field: str) -> M | None:
    if raw is None or not raw.strip():
        return None
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        message = f"{field}: {describe_validation_errors(exc.errors())}"
        raise ApiError("invalid_request", message) from None


@router.post("/bundle", response_class=Response, responses=PDF_RESPONSE)
async def render_bundle(
    request: Request,
    files: Annotated[
        list[UploadFile] | None,
        File(description="Assets. Each file's name is its path in the bundle, e.g. css/site.css."),
    ] = None,
    html: Annotated[
        str | None, Form(description="The HTML document (or send a file named index.html).")
    ] = None,
    data: Annotated[
        str | None,
        Form(description="JSON object: render the document as a Jinja2 template with it."),
    ] = None,
    options: Annotated[str | None, Form(description="RenderOptions, as JSON.")] = None,
    post: Annotated[str | None, Form(description="PostProcess, as JSON.")] = None,
    auth: Annotated[
        str | None,
        Form(description="RenderAuth, as JSON: credentials for the remote resources loaded."),
    ] = None,
    filename: Annotated[str, Form(max_length=200)] = "document.pdf",
) -> Response:
    """Render HTML with its CSS, fonts, images and scripts sent in the same request.

    Relative references resolve inside the bundle; the files are served from memory
    and never reach the network. A missing file is a 404 in the render report.
    """
    assets: dict[str, bytes] = {}
    document = html
    for upload in files or []:
        path = check_path(upload.filename or "")  # fail fast, before waiting for a browser
        if path in assets:
            raise ApiError("invalid_request", f"duplicate asset path: {path}")
        content = await upload.read()
        if path == "index.html" and html is None:
            document = content.decode("utf-8", errors="replace")
            continue
        assets[path] = content
    if document is None:
        raise ApiError("invalid_request", "send the document as html or a file named index.html")
    values: dict[str, Any] | None = None
    if data is not None:
        try:
            values = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ApiError("invalid_request", f"data: not valid JSON ({exc.msg})") from None
        if not isinstance(values, dict):
            raise ApiError("invalid_request", "data: must be a JSON object")
    render_options = clamp_timeout(
        _json_field(RenderOptions, options, "options") or RenderOptions(), settings_of(request)
    )
    post_process = _json_field(PostProcess, post, "post")
    render_auth = _json_field(RenderAuth, auth, "auth")

    renderer = renderer_of(request)
    if values is None:
        work = renderer.from_html(document, render_options, assets=assets, auth=render_auth)
    else:
        work = renderer.from_template(
            document, values, render_options, assets=assets, auth=render_auth
        )
    doc = await timed(request, "bundle", work)
    return await pdf_response(doc, safe_filename(filename), post_process)
