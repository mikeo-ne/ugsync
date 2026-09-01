"""Review/dispute application service.

Encapsulates the detection lifecycle and role separation:

* Any authenticated portal user may read the redacted review queue.
* A ``reviewer`` or ``catalog_admin`` verifies or rejects candidate
  detections. Decisions are audit-logged with a required note.
* Any portal user acting for a rights party may open a dispute (only from a
  candidate/verified state). The disputed amount is *held*, never silently
  redistributed.
* Only a ``finance_reviewer`` or ``catalog_admin`` resolves a dispute
  (upheld -> the detection is rejected; dismissed -> it returns to verified),
  enforcing separation of duties from the reviewer who first triaged it.

Every state transition is validated and audit-logged in the same transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import ReviewForbidden, ReviewNotFound, ReviewStateConflict, ReviewValidationError
from .models import (
    DISPUTABLE_STATES,
    DetectionEvidence,
    Dispute,
    DisputeResolution,
    PortalUser,
    ReviewDecision,
)

REVIEW_ROLES = frozenset({"reviewer", "catalog_admin"})
DISPUTE_RESOLUTION_ROLES = frozenset({"finance_reviewer", "catalog_admin"})
DISPUTE_REASONS = frozenset(
    {"wrong_identity", "wrong_duration", "wrong_source", "wrong_split", "duplicate", "other"}
)
MAX_NOTE = 2000


@dataclass(frozen=True, slots=True)
class QueuePage:
    items: tuple[DetectionEvidence, ...]
    total: int
    limit: int
    offset: int


class ReviewService:
    def __init__(self, store: Any) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def list_detections(
        self,
        user: PortalUser,
        *,
        status: str | None = None,
        source_code: str | None = None,
        recording_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> QueuePage:
        self._require_authenticated(user)
        limit = self._bounded_limit(limit)
        if offset < 0:
            raise ReviewValidationError("offset must be >= 0")
        items, total = self._store.list_detections(
            None,
            status=status,
            source_code=source_code,
            recording_id=recording_id,
            limit=limit,
            offset=offset,
        )
        return QueuePage(items=tuple(items), total=total, limit=limit, offset=offset)

    def get_detection(self, user: PortalUser, detection_id: str) -> DetectionEvidence:
        self._require_authenticated(user)
        evidence = self._store.get_detection(None, detection_id)
        if evidence is None:
            raise ReviewNotFound(f"detection {detection_id} not found")
        return evidence

    # ------------------------------------------------------------------
    # Reviewer decisions
    # ------------------------------------------------------------------

    def decide(
        self,
        user: PortalUser,
        detection_id: str,
        decision: str,
        note: str,
        *,
        request_id: str | None = None,
    ) -> DetectionEvidence:
        self._require_role(user, REVIEW_ROLES, action="review detections")
        if decision not in (ReviewDecision.VERIFIED, ReviewDecision.REJECTED):
            raise ReviewValidationError(
                f"decision must be one of {[d.value for d in ReviewDecision]}"
            )
        note = self._require_note(note, "review note")

        with self._store.transaction() as tx:
            evidence = self._store.get_detection(tx, detection_id)
            if evidence is None:
                raise ReviewNotFound(f"detection {detection_id} not found")
            if evidence.status != "candidate":
                raise ReviewStateConflict(
                    f"detection {detection_id} is '{evidence.status}'; only candidates can be "
                    "verified or rejected"
                )
            self._store.set_detection_status(
                tx,
                detection_id,
                status=str(decision),
                reviewed_by=user.user_id,
                review_note=note,
            )
            self._audit(
                tx,
                action=f"detection.{decision}",
                entity_id=detection_id,
                user=user,
                request_id=request_id,
                metadata={"note": note, "from_status": "candidate"},
            )
            refreshed = self._store.get_detection(tx, detection_id)
        return refreshed

    # ------------------------------------------------------------------
    # Disputes
    # ------------------------------------------------------------------

    def open_dispute(
        self,
        user: PortalUser,
        detection_id: str,
        *,
        reason: str,
        detail: str,
        request_id: str | None = None,
    ) -> Dispute:
        self._require_authenticated(user)
        if reason not in DISPUTE_REASONS:
            raise ReviewValidationError(f"reason must be one of {sorted(DISPUTE_REASONS)}")
        detail = self._require_note(detail, "dispute detail", minimum=20)

        with self._store.transaction() as tx:
            evidence = self._store.get_detection(tx, detection_id)
            if evidence is None:
                raise ReviewNotFound(f"detection {detection_id} not found")
            if evidence.status not in DISPUTABLE_STATES:
                raise ReviewStateConflict(
                    f"detection {detection_id} is '{evidence.status}'; only "
                    f"{sorted(DISPUTABLE_STATES)} detections can be disputed"
                )
            if evidence.has_dispute:
                raise ReviewStateConflict("an open dispute already exists for this detection")
            dispute_id = self._store.create_dispute(
                tx,
                detection_event_id=detection_id,
                raised_by_user_id=user.user_id,
                raised_by_party_id=user.party_id,
                reason=reason,
                detail=detail,
            )
            # Move to disputed; the affected amount is held downstream.
            self._store.set_detection_status(
                tx,
                detection_id,
                status="disputed",
                reviewed_by=user.user_id,
                review_note=f"dispute {dispute_id} opened: {reason}",
            )
            self._audit(
                tx,
                action="dispute.opened",
                entity_id=dispute_id,
                user=user,
                request_id=request_id,
                metadata={"detection_event_id": detection_id, "reason": reason},
            )
            dispute = self._store.get_dispute(tx, dispute_id)
        return dispute

    def resolve_dispute(
        self,
        user: PortalUser,
        dispute_id: str,
        resolution: str,
        note: str,
        *,
        request_id: str | None = None,
    ) -> Dispute:
        self._require_role(user, DISPUTE_RESOLUTION_ROLES, action="resolve disputes")
        if resolution not in (DisputeResolution.UPHELD, DisputeResolution.DISMISSED):
            raise ReviewValidationError(
                f"resolution must be one of {[r.value for r in DisputeResolution]}"
            )
        note = self._require_note(note, "resolution note")

        with self._store.transaction() as tx:
            dispute = self._store.get_dispute(tx, dispute_id)
            if dispute is None:
                raise ReviewNotFound(f"dispute {dispute_id} not found")
            if dispute.status != "open":
                raise ReviewStateConflict(
                    f"dispute {dispute_id} is '{dispute.status}'; only open disputes resolve"
                )
            resulting_status = "rejected" if resolution == DisputeResolution.UPHELD else "verified"
            resolved = self._store.resolve_dispute(
                tx,
                dispute_id,
                status=str(resolution),
                resolved_by=user.user_id,
                resolution_note=note,
                detection_resulting_status=resulting_status,
            )
            self._audit(
                tx,
                action=f"dispute.{resolution}",
                entity_id=dispute_id,
                user=user,
                request_id=request_id,
                metadata={
                    "detection_event_id": dispute.detection_event_id,
                    "resulting_status": resulting_status,
                },
            )
        return resolved

    def list_disputes(self, user: PortalUser, *, status: str | None = None, limit: int = 50) -> tuple[Dispute, ...]:
        self._require_authenticated(user)
        limit = self._bounded_limit(limit)
        return tuple(self._store.list_disputes(None, status=status, limit=limit))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _audit(
        self,
        tx: Any,
        *,
        action: str,
        entity_id: str,
        user: PortalUser,
        request_id: str | None,
        metadata: dict[str, Any],
    ) -> None:
        self._store.write_audit(
            tx,
            {
                "action": action,
                "entity_type": "detection_event" if action.startswith("detection") else "dispute",
                "entity_id": entity_id,
                "actor_id": user.user_id,
                "request_id": request_id,
                "metadata": metadata,
            },
        )

    @staticmethod
    def _require_authenticated(user: PortalUser) -> None:
        if not isinstance(user, PortalUser) or not user.user_id:
            raise ReviewForbidden("authentication required")

    def _require_role(self, user: PortalUser, roles: frozenset[str], *, action: str) -> None:
        self._require_authenticated(user)
        if user.role not in roles:
            raise ReviewForbidden(f"role '{user.role}' may not {action}")

    @staticmethod
    def _bounded_limit(limit: int) -> int:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ReviewValidationError("limit must be a positive integer")
        return min(limit, 200)

    @staticmethod
    def _require_note(note: object, field: str, *, minimum: int = 10) -> str:
        if not isinstance(note, str) or len(note.strip()) < minimum:
            raise ReviewValidationError(f"{field} must be a string of at least {minimum} characters")
        cleaned = note.strip()
        if len(cleaned) > MAX_NOTE:
            raise ReviewValidationError(f"{field} must be at most {MAX_NOTE} characters")
        return cleaned
