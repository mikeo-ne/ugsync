"""Review portal error taxonomy mapped to HTTP responses."""

from __future__ import annotations


class ReviewError(Exception):
    http_status = 400


class ReviewValidationError(ReviewError):
    http_status = 422

    def __init__(self, errors: str | list[str]) -> None:
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


class ReviewForbidden(ReviewError):
    http_status = 403


class ReviewNotFound(ReviewError):
    http_status = 404


class ReviewStateConflict(ReviewError):
    http_status = 409
