"""Error types mapped to the api-contracts README error envelope and code table."""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from bookie_emulator.api.envelope import ErrorBody, ErrorEnvelope, make_meta


class ApiError(Exception):
    status_code = 500
    code = "INTERNAL_ERROR"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class InvalidParameterError(ApiError):
    status_code = 400
    code = "INVALID_PARAMETER"


class NotFoundError(ApiError):
    status_code = 404
    code = "RESOURCE_NOT_FOUND"


class DuplicateResourceError(ApiError):
    status_code = 409
    code = "DUPLICATE_RESOURCE"


class UnprocessableError(ApiError):
    status_code = 422
    code = "UNPROCESSABLE_ENTITY"


class DependencyError(ApiError):
    status_code = 502
    code = "DEPENDENCY_ERROR"


class DependencyTimeoutError(ApiError):
    status_code = 504
    code = "TIMEOUT"


def _error_response(status_code: int, code: str, message: str, details: dict[str, Any]) -> JSONResponse:
    body = ErrorEnvelope(error=ErrorBody(code=code, message=message, details=details), meta=make_meta())
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        details = {"errors": [{"loc": list(map(str, e["loc"])), "msg": e["msg"]} for e in exc.errors()]}
        return _error_response(400, "VALIDATION_ERROR", "Request validation failed", details)

    @app.exception_handler(Exception)
    async def handle_unexpected(_request: Request, _exc: Exception) -> JSONResponse:
        return _error_response(500, "INTERNAL_ERROR", "An unexpected error occurred", {})
