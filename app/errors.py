from typing import Any, Dict, List, Optional

import structlog
from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)


class AppException(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        self.headers = headers or {}


class SeatTakenException(AppException):
    def __init__(self, unavailable_seats: List[str]):
        super().__init__(
            code="seat_taken",
            message=f"Requested seats unavailable: {', '.join(unavailable_seats)}",
            status_code=status.HTTP_409_CONFLICT,
            details={"unavailable_seats": unavailable_seats},
        )


class PerUserLimitException(AppException):
    def __init__(self, message: str = "User quota exceeded for this show"):
        super().__init__(
            code="per_user_limit",
            message=message,
            status_code=status.HTTP_409_CONFLICT,
        )


class IdempotencyKeyReuseException(AppException):
    def __init__(self, message: str = "Idempotency key reused with different request payload"):
        super().__init__(
            code="idempotency_key_reuse",
            message=message,
            status_code=status.HTTP_409_CONFLICT,
        )


class UnknownSeatException(AppException):
    def __init__(self, message: str = "One or more requested seats do not exist in this show"):
        super().__init__(
            code="unknown_seat",
            message=message,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )


class ValidationException(AppException):
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(
            code="validation_error",
            message=message,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            details=details,
        )


class UnauthorizedException(AppException):
    def __init__(self, message: str = "Authentication credentials missing or invalid"):
        super().__init__(
            code="unauthorized",
            message=message,
            status_code=status.HTTP_401_UNAUTHORIZED,
        )


class ForbiddenException(AppException):
    def __init__(self, message: str = "Access forbidden"):
        super().__init__(
            code="forbidden",
            message=message,
            status_code=status.HTTP_403_FORBIDDEN,
        )


class NotFoundException(AppException):
    def __init__(self, message: str = "Resource not found"):
        super().__init__(
            code="not_found",
            message=message,
            status_code=status.HTTP_404_NOT_FOUND,
        )


class OverloadedException(AppException):
    def __init__(self, retry_after: int = 2):
        super().__init__(
            code="overloaded",
            message="Server is temporarily overloaded. Please retry later.",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(retry_after)},
        )


class DatabaseUnavailableException(AppException):
    def __init__(self, message: str = "Database service unavailable"):
        super().__init__(
            code="database_unavailable",
            message=message,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )


def format_error_response(
    code: str,
    message: str,
    request_id: str,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    error_dict: Dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": request_id,
    }
    if details:
        error_dict.update(details)
    return {"error": error_dict}


async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    request.state.reason = exc.code
    request_id = getattr(request.state, "request_id", "unknown")
    content = format_error_response(
        code=exc.code,
        message=exc.message,
        request_id=request_id,
        details=exc.details,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers=exc.headers,
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    request.state.reason = "validation_error"
    request_id = getattr(request.state, "request_id", "unknown")
    error_messages = [
        f"{'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}" for err in exc.errors()
    ]
    combined_message = "; ".join(error_messages) if error_messages else "Request validation failed"
    sanitized_errors = [
        {
            "loc": list(err.get("loc", [])),
            "msg": str(err.get("msg", "")),
            "type": str(err.get("type", "")),
        }
        for err in exc.errors()
    ]
    content = format_error_response(
        code="validation_error",
        message=combined_message,
        request_id=request_id,
        details={"validation_errors": sanitized_errors},
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=content,
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request.state.reason = "internal_error"
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception("unhandled_exception", error=str(exc), request_id=request_id)
    content = format_error_response(
        code="internal_error",
        message="An internal server error occurred",
        request_id=request_id,
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=content,
    )
