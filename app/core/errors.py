"""Exception handlers: every error response is {"error": {"code", "message", "details"}}."""

import logging
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.shared.exceptions import MESSAGES, AppError, ErrorCode

logger = logging.getLogger("app.errors")

_HTTP_STATUS_CODES: dict[int, ErrorCode] = {
    status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
    status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
}


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details}},
        headers=headers,
    )


def _field_name(loc: tuple[int | str, ...]) -> str:
    parts = [str(part) for part in loc]
    if parts and parts[0] in ("body", "query", "path", "header", "cookie"):
        parts = parts[1:]
    return ".".join(parts) or "body"


async def handle_app_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    details = [
        {"field": _field_name(tuple(err.get("loc", ()))), "message": err.get("msg", "")}
        for err in exc.errors()
    ]
    return error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorCode.VALIDATION_ERROR,
        MESSAGES[ErrorCode.VALIDATION_ERROR],
        details,
    )


async def handle_http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _HTTP_STATUS_CODES.get(exc.status_code, ErrorCode.HTTP_ERROR)
    return error_response(exc.status_code, code, MESSAGES[code], None, exc.headers)


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        ErrorCode.INTERNAL_ERROR,
        MESSAGES[ErrorCode.INTERNAL_ERROR],
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, handle_app_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, handle_http_error)
    app.add_exception_handler(Exception, handle_unexpected_error)
