"""Domain exceptions and error handling."""

from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger(__name__)


class DomainError(Exception):
    """Base exception for domain errors."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "DOMAIN_ERROR",
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message
        self.code = code
        self.details = details or {}
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        """Convert exception to dictionary for JSON response."""
        result = {"error": {"code": self.code, "message": self.message}}
        if self.details:
            result["error"]["details"] = self.details
        return result


class ValidationError(DomainError):
    """Exception for validation errors."""

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            code="VALIDATION_ERROR",
            details={**({"field": field} if field else {}), **(details or {})},
        )


class NotFoundError(DomainError):
    """Exception for resource not found errors."""

    def __init__(
        self,
        message: str,
        *,
        resource: str | None = None,
        resource_id: str | None = None,
    ) -> None:
        super().__init__(
            message,
            code="NOT_FOUND",
            details={
                **({"resource": resource} if resource else {}),
                **({"resource_id": resource_id} if resource_id else {}),
            },
        )


class ConflictError(DomainError):
    """Exception for conflict errors (e.g., duplicate resources)."""

    def __init__(
        self,
        message: str,
        *,
        resource: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            code="CONFLICT",
            details={**({"resource": resource} if resource else {}), **(details or {})},
        )


class ExternalServiceError(DomainError):
    """Exception for external service failures."""

    def __init__(
        self,
        message: str,
        *,
        service: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            code="EXTERNAL_SERVICE_ERROR",
            details={**({"service": service} if service else {}), **(details or {})},
        )


class JobNotFoundError(NotFoundError):
    """Exception for job not found."""

    def __init__(self, job_id: str) -> None:
        super().__init__(
            f"Job with id {job_id} not found",
            resource="job",
            resource_id=job_id,
        )


class VideoNotFoundError(NotFoundError):
    """Exception for video not found."""

    def __init__(self, video_id: str) -> None:
        super().__init__(
            f"Video with id {video_id} not found",
            resource="video",
            resource_id=video_id,
        )


class VideoNotTranscribedError(DomainError):
    """Exception for video not transcribed yet."""

    def __init__(self, video_id: str) -> None:
        super().__init__(
            f"Video {video_id} has not been transcribed yet",
            code="VIDEO_NOT_TRANSCRIBED",
            details={"video_id": video_id},
        )


class DownloadNotFoundError(NotFoundError):
    """Exception for a video download request that does not resolve."""

    def __init__(self, download_id: str) -> None:
        super().__init__(
            f"Download with id {download_id} not found",
            resource="download",
            resource_id=download_id,
        )


class InvalidYouTubeURLError(DomainError):
    """Exception for invalid YouTube URL (domain validation, not format validation)."""

    def __init__(self, url: str) -> None:
        super().__init__(
            f"Invalid YouTube URL: {url}",
            code="INVALID_YOUTUBE_URL",
            details={"url": url},
        )


class InvalidVideoURLError(DomainError):
    """Exception for a malformed URL on a source the app does support."""

    def __init__(self, url: str) -> None:
        super().__init__(
            f"Invalid video URL: {url}",
            code="INVALID_VIDEO_URL",
            details={"url": url},
        )


class UnsupportedVideoSourceError(DomainError):
    """Exception for a video URL on a host the app does not support."""

    def __init__(self, url: str) -> None:
        super().__init__(
            f"Unsupported video source: {url}",
            code="UNSUPPORTED_VIDEO_SOURCE",
            details={"url": url},
        )


# Auth errors. Codes mirror app.schemas.auth.ErrorCode so the client can key off
# one vocabulary; the client localizes, the backend only supplies the code.


class AuthenticationFailedError(DomainError):
    """Credentials did not match, or the address is unknown.

    Both cases deliberately share one code: separating them turns the endpoint
    into an account-enumeration oracle.
    """

    def __init__(self) -> None:
        super().__init__("Invalid email or password", code="INVALID_CREDENTIALS")


class WrongCurrentPasswordError(DomainError):
    """The password offered to replace an existing one is not the current one.

    Distinct from :class:`AuthenticationFailedError` on purpose. That code is
    deliberately vague because a sign-in must not reveal whether an address is
    registered, but this endpoint is already authenticated as the account's owner
    and can only ever be asked about *their own* password - so it discloses
    nothing, and "that is not your current password" tells the visitor which of
    the two boxes on the form was wrong.
    """

    def __init__(self) -> None:
        super().__init__("The current password is not correct", code="WRONG_CURRENT_PASSWORD")


class AccountLockedError(DomainError):
    """The account exceeded the failed-login threshold and awaits an admin unlock."""

    def __init__(self) -> None:
        super().__init__("Account is locked; contact support", code="ACCOUNT_LOCKED")


class EmailNotVerifiedError(DomainError):
    """The account exists but its email address is not verified."""

    def __init__(self) -> None:
        super().__init__("Email address is not verified", code="EMAIL_UNVERIFIED")


class TooManyAttemptsError(DomainError):
    """Per-IP failed-login ceiling reached."""

    def __init__(self) -> None:
        super().__init__("Too many attempts; try again later", code="TOO_MANY_ATTEMPTS")


class UnauthenticatedError(DomainError):
    """No valid session was presented."""

    def __init__(self) -> None:
        super().__init__("Authentication required", code="UNAUTHENTICATED")


class CsrfError(DomainError):
    """A cookie-authenticated mutation arrived without a matching CSRF token.

    The browser attaches the session cookie to a cross-site POST automatically,
    so without this check another site could log the visitor out or delete
    their history on their behalf.
    """

    def __init__(self) -> None:
        super().__init__("CSRF token missing or incorrect", code="CSRF_FAILED")


class InvalidTokenError(DomainError):
    """A verification, reset, or Google token was malformed, expired, or reused."""

    def __init__(self) -> None:
        super().__init__("Link is invalid or has expired", code="INVALID_REQUEST")


class EmailAlreadyRegisteredError(DomainError):
    """The address is already in use."""

    def __init__(self) -> None:
        super().__init__("Email already registered", code="CONFLICT")


class AuthServiceUnavailableError(DomainError):
    """Session storage is unreachable, so the request is refused.

    Auth fails closed: a dependency blip must never degrade into allowing
    everyone through.
    """

    def __init__(self) -> None:
        super().__init__("Service temporarily unavailable", code="SERVICE_UNAVAILABLE")


# Exception handlers


async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    """Handle domain errors and return appropriate HTTP responses."""
    logger.warning(
        "Domain error",
        path=request.url.path,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )

    status_code = _get_status_code(exc.code)
    return JSONResponse(
        status_code=status_code,
        content=exc.to_dict(),
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Handle HTTP exceptions and return consistent error format."""
    logger.warning(
        "HTTP exception",
        path=request.url.path,
        status_code=exc.status_code,
        detail=exc.detail,
    )

    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": "HTTP_ERROR", "message": str(exc.detail)}},
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle unexpected exceptions and return safe error response."""
    logger.exception(
        "Unexpected error",
        path=request.url.path,
        error_type=type(exc).__name__,
        error=str(exc),
    )

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An unexpected error occurred",
            }
        },
    )


def _get_status_code(error_code: str) -> int:
    """Map error codes to HTTP status codes."""
    mapping = {
        "VALIDATION_ERROR": status.HTTP_422_UNPROCESSABLE_ENTITY,
        "INVALID_YOUTUBE_URL": status.HTTP_400_BAD_REQUEST,
        "INVALID_VIDEO_URL": status.HTTP_400_BAD_REQUEST,
        "UNSUPPORTED_VIDEO_SOURCE": status.HTTP_400_BAD_REQUEST,
        "NOT_FOUND": status.HTTP_404_NOT_FOUND,
        "VIDEO_NOT_TRANSCRIBED": status.HTTP_404_NOT_FOUND,
        "CONFLICT": status.HTTP_409_CONFLICT,
        "EXTERNAL_SERVICE_ERROR": status.HTTP_502_BAD_GATEWAY,
        "DOMAIN_ERROR": status.HTTP_400_BAD_REQUEST,
        "INVALID_CREDENTIALS": status.HTTP_401_UNAUTHORIZED,
        "WRONG_CURRENT_PASSWORD": status.HTTP_401_UNAUTHORIZED,
        "ACCOUNT_LOCKED": status.HTTP_423_LOCKED,
        "EMAIL_UNVERIFIED": status.HTTP_403_FORBIDDEN,
        "TOO_MANY_ATTEMPTS": status.HTTP_429_TOO_MANY_REQUESTS,
        "UNAUTHENTICATED": status.HTTP_401_UNAUTHORIZED,
        "CSRF_FAILED": status.HTTP_403_FORBIDDEN,
        "INVALID_REQUEST": status.HTTP_400_BAD_REQUEST,
        "SERVICE_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    return mapping.get(error_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
