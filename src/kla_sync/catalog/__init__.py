"""Catalog onboarding: validated documents, persistence stores, and the service."""

from .errors import (
    CatalogConflictError,
    CatalogError,
    CatalogNotFoundError,
    CatalogStateError,
    CatalogValidationError,
)
from .models import (
    ArtistCredit,
    OnboardingDocument,
    OrganizationDocument,
    PartyDocument,
    RecordingDocument,
    ReleaseDocument,
    SplitLineDocument,
    SplitSheetDocument,
    WorkContributor,
    WorkDocument,
)
from .service import CatalogService, OnboardingResult, parse_onboarding_document

__all__ = [
    "ArtistCredit",
    "CatalogConflictError",
    "CatalogError",
    "CatalogNotFoundError",
    "CatalogService",
    "CatalogStateError",
    "CatalogValidationError",
    "OnboardingDocument",
    "OnboardingResult",
    "OrganizationDocument",
    "PartyDocument",
    "RecordingDocument",
    "ReleaseDocument",
    "SplitLineDocument",
    "SplitSheetDocument",
    "WorkContributor",
    "WorkDocument",
    "parse_onboarding_document",
]
