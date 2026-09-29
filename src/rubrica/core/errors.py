"""Domain errors. The HTTP layer maps these to status codes in one place."""


class DomainError(Exception):
    status = 400

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.message = message
        if status is not None:
            self.status = status


class NotFound(DomainError):
    status = 404


class Unauthorized(DomainError):
    status = 401


class Forbidden(DomainError):
    status = 403


class Conflict(DomainError):
    status = 409


class Closed(DomainError):
    """Deadline passed / window not open."""
    status = 403


class RateLimited(DomainError):
    status = 429
