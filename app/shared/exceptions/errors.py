from typing import Any

from app.shared.exceptions.codes import MESSAGES, ErrorCode


class AppError(Exception):
    """An error returned to the client as {"error": {"code", "message", "details"}}.

    code is stable UPPER_SNAKE English; message is a short Arabic sentence the app can show
    (defaults to the registered message for the code).
    """

    default_status: int = 400

    def __init__(
        self,
        code: ErrorCode | str,
        message: str | None = None,
        status_code: int | None = None,
        details: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = str(code)
        if message is None:
            message = MESSAGES.get(ErrorCode(code), "") if code in ErrorCode else ""
        self.message = message
        self.status_code = status_code or self.default_status
        self.details = details
        self.headers = headers
        super().__init__(f"{self.code}: {self.message}")


class Unauthorized(AppError):
    default_status = 401

    def __init__(self, code: ErrorCode | str, message: str | None = None, details: Any = None) -> None:
        super().__init__(code, message, details=details, headers={"WWW-Authenticate": "Bearer"})


class Forbidden(AppError):
    default_status = 403


class NotFound(AppError):
    default_status = 404


class Conflict(AppError):
    default_status = 409


class ValidationFailed(AppError):
    default_status = 422


def out_of_stock(product_id: int, variant_id: int | None) -> Conflict:
    """409 OUT_OF_STOCK, details {product_id, variant_id} (variant_id null for simple/bundle)."""
    return Conflict(ErrorCode.OUT_OF_STOCK, details={"product_id": product_id, "variant_id": variant_id})


def insufficient_stock(product_id: int, variant_id: int | None, available: int) -> Conflict:
    """409 INSUFFICIENT_STOCK, details {product_id, variant_id, available}: the same shape everywhere."""
    return Conflict(
        ErrorCode.INSUFFICIENT_STOCK,
        details={"product_id": product_id, "variant_id": variant_id, "available": max(0, available)},
    )


class TooManyRequests(AppError):
    default_status = 429

    def __init__(
        self, code: ErrorCode | str, retry_after: int, message: str | None = None, details: Any = None
    ) -> None:
        self.retry_after = max(1, int(retry_after))
        super().__init__(
            code,
            message,
            details={"retry_after": self.retry_after, **(details or {})},
            headers={"Retry-After": str(self.retry_after)},
        )
