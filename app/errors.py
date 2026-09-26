"""统一错误响应:所有失败都返回 {"error": {...}} 结构。"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)

STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}


def error_payload(code: str, message: str, fields: dict[str, str] | None = None) -> dict:
    return {"error": {"code": code, "message": message, "fields": fields}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            payload = detail
        else:
            payload = error_payload(STATUS_CODES.get(exc.status_code, "error"), str(detail))
        headers = getattr(exc, "headers", None)
        return JSONResponse(payload, status_code=exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields: dict[str, str] = {}
        for item in exc.errors():
            location = [str(part) for part in item.get("loc", ()) if part not in ("body", "query")]
            key = ".".join(location) or "request"
            fields.setdefault(key, item.get("msg", "输入不合法"))
        return JSONResponse(
            error_payload("validation_error", "请求参数校验失败", fields),
            status_code=422,
        )
