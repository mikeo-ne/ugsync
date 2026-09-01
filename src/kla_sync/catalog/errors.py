"""Catalog service error taxonomy mapped to HTTP responses by the API layer."""

from __future__ import annotations


class CatalogError(Exception):
    """Base class for catalog onboarding failures."""

    http_status = 400


class CatalogValidationError(CatalogError):
    """The onboarding document failed field or cross-reference validation."""

    http_status = 422

    def __init__(self, errors: str | list[str]) -> None:
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


class CatalogConflictError(CatalogError):
    """The request conflicts with existing catalog state (duplicate identifier)."""

    http_status = 409


class CatalogNotFoundError(CatalogError):
    """A referenced entity does not exist."""

    http_status = 404


class CatalogStateError(CatalogError):
    """The entity cannot transition in the requested way."""

    http_status = 409
