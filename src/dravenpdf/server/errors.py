"""The one place where library errors become HTTP responses."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from dravenpdf.errors import DravenPdfError

logger = logging.getLogger("dravenpdf.server")

STATUS_BY_CODE: dict[str, int] = {
    "invalid_request": 400,
    "invalid_template": 400,
    "unauthorized": 401,
    "payload_too_large": 413,
    "unsupported_media_type": 415,
    "invalid_pdf": 422,
    "blocked_request": 422,
    "render_failed": 422,
    "busy": 503,
    "render_timeout": 504,
    "internal_error": 500,
}


class ApiError(Exception):
    """An error raised by the service itself (not the library)."""

    def __init__(self, code: str, message: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.headers = headers


def error_response(code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=STATUS_BY_CODE.get(code, 500),
        headers=headers,
    )


def install(app: FastAPI) -> None:
    @app.exception_handler(DravenPdfError)
    async def _library(request: Request, exc: DravenPdfError) -> JSONResponse:
        headers = {"Retry-After": "5"} if exc.code == "busy" else None
        if STATUS_BY_CODE.get(exc.code, 500) >= 500:
            logger.warning("%s: %s", exc.code, exc.message)
        return error_response(exc.code, exc.message, headers)

    @app.exception_handler(ApiError)
    async def _api(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'] if p != 'body')}: {e['msg']}" for e in exc.errors()
        )
        return error_response("invalid_request", details or "invalid request")

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException) -> JSONResponse:
        code = {401: "unauthorized", 404: "not_found", 405: "method_not_allowed"}.get(
            exc.status_code, "invalid_request"
        )
        return JSONResponse(
            {"error": {"code": code, "message": str(exc.detail)}}, status_code=exc.status_code
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "-")
        logger.exception("unhandled error (request %s)", request_id)
        # 500s are sent by Starlette's outermost middleware, outside RequestIdMiddleware,
        # so the header has to be added here.
        return error_response(
            "internal_error",
            f"internal error (request id {request_id})",
            {"X-Request-ID": request_id},
        )
