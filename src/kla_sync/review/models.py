"""Review portal domain models.

These are redacted views and decision commands for the reviewer/dispute
workflow. They never carry raw audio keys, wallet data, or party PII — the
portal reads evidence summaries, not the underlying capture or payment rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

# Roles used by the reference portal. In production these map onto the
# Supabase catalog membership roles (migration 002); a dedicated detection
# reviewer role should be added to the kla_catalog_member_role enum.
ROLES = frozenset({"viewer", "catalog_editor", "reviewer", "finance_reviewer", "catalog_admin"})

# Detection lifecycle states from the core schema.
DETECTION_STATES = frozenset({"candidate", "verified", "rejected", "disputed", "expired"})

# States from which a rightsholder/CMO user may open a dispute.
DISPUTABLE_STATES = frozenset({"candidate", "verified"})


class ReviewDecision(StrEnum):
    VERIFIED = "verified"
    REJECTED = "rejected"


class DisputeStatus(StrEnum):
    OPEN = "open"
    UPHELD = "upheld"
    DISMISSED = "dismissed"


class DisputeResolution(StrEnum):
    UPHELD = "upheld"      # challenge succeeds -> detection rejected, amount stays held
    DISMISSED = "dismissed"  # challenge fails -> detection returns to verified


@dataclass(frozen=True, slots=True)
class PortalUser:
    """An authenticated portal user (Supabase Auth user in production)."""

    user_id: str
    role: str
    display_name: str = ""
    party_id: str | None = None  # linked rights_party for rightsholder disputes

    def has_role(self, *roles: str) -> bool:
        return self.role in roles


@dataclass(frozen=True, slots=True)
class DetectionEvidence:
    """Redacted detection evidence safe to show in a reviewer queue."""

    id: str
    capture_chunk_id: str
    source_id: str
    source_code: str
    recording_id: str
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    matcher_version: str
    fingerprint_schema_id: str
    matched_hash_count: int
    match_confidence: float
    tempo_scale: float | None
    offset_seconds: float
    status: str
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_note: str | None
    has_dispute: bool = False
    dispute_status: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "capture_chunk_id": self.capture_chunk_id,
            "source": {"id": self.source_id, "code": self.source_code},
            "recording_id": self.recording_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_seconds": round(self.duration_seconds, 3),
            "evidence": {
                "matcher_version": self.matcher_version,
                "fingerprint_schema_id": self.fingerprint_schema_id,
                "matched_hash_count": self.matched_hash_count,
                "match_confidence": round(self.match_confidence, 4),
                "tempo_scale": self.tempo_scale,
                "offset_seconds": round(self.offset_seconds, 3),
            },
            "status": self.status,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at.isoformat() if self.reviewed_at else None,
            "review_note": self.review_note,
            "dispute": {"open": self.has_dispute, "status": self.dispute_status},
        }


@dataclass(frozen=True, slots=True)
class Dispute:
    id: str
    detection_event_id: str
    raised_by_user_id: str
    raised_by_party_id: str | None
    reason: str
    detail: str
    status: str
    resolved_by_user_id: str | None
    resolution_note: str | None
    created_at: datetime
    resolved_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "detection_event_id": self.detection_event_id,
            "raised_by_user_id": self.raised_by_user_id,
            "raised_by_party_id": self.raised_by_party_id,
            "reason": self.reason,
            "detail": self.detail,
            "status": self.status,
            "resolved_by_user_id": self.resolved_by_user_id,
            "resolution_note": self.resolution_note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }
