"""Reviewer/dispute portal: redacted evidence, decisions, and disputes.

This is the portal plane behind Supabase Auth in production. Reviewers see
candidate detections (never raw audio keys, wallet data, or PII), verify or
reject them with an audit trail, and rightsholders/CMO users can open disputes
that put an amount on hold until a finance/governance user resolves it.

A match is evidence until a governed decision changes it:
``candidate → verified | rejected`` and ``candidate | verified → disputed →
verified | rejected``.
"""

from .errors import (
    ReviewError,
    ReviewForbidden,
    ReviewNotFound,
    ReviewStateConflict,
    ReviewValidationError,
)
from .models import (
    ROLES,
    DetectionEvidence,
    Dispute,
    DisputeResolution,
    PortalUser,
    ReviewDecision,
)
from .service import ReviewService

__all__ = [
    "ROLES",
    "DetectionEvidence",
    "Dispute",
    "DisputeResolution",
    "PortalUser",
    "ReviewDecision",
    "ReviewError",
    "ReviewForbidden",
    "ReviewNotFound",
    "ReviewService",
    "ReviewStateConflict",
    "ReviewValidationError",
]
