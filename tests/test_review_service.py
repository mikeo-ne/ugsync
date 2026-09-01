from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from kla_sync.review.errors import ReviewForbidden, ReviewStateConflict, ReviewValidationError
from kla_sync.review.models import DisputeResolution, PortalUser, ReviewDecision
from kla_sync.review.service import ReviewService
from kla_sync.review.store import InMemoryReviewStore


def _seed_candidate(store: InMemoryReviewStore, *, confidence: float = 0.7, votes: int = 40) -> str:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    return store.record_candidate(
        source_id="source-1",
        source_code="kampala-radio-01",
        recording_id="rec-1",
        capture_chunk_id="11111111-1111-4111-8111-111111111111",
        started_at=now,
        ended_at=now + timedelta(seconds=30),
        matcher_version="kla-landmark-ratio-v1",
        fingerprint_schema_id="schema-1",
        matched_hash_count=votes,
        match_confidence=confidence,
        tempo_scale=1.0,
        offset_seconds=0.0,
    )


class ReviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryReviewStore()
        self.service = ReviewService(self.store)
        self.reviewer = PortalUser("u-reviewer", "reviewer", "Reviewer Rae")
        self.admin = PortalUser("u-admin", "catalog_admin", "Admin A")
        self.finance = PortalUser("u-finance", "finance_reviewer", "Finance F")
        self.creator = PortalUser("u-creator", "viewer", "Creator C", party_id="party-creator")
        self.editor = PortalUser("u-editor", "catalog_editor", "Editor E")

    # --- role separation -------------------------------------------------

    def test_viewer_cannot_review_or_resolve(self) -> None:
        detection_id = _seed_candidate(self.store)
        with self.assertRaises(ReviewForbidden):
            self.service.decide(self.creator, detection_id, ReviewDecision.VERIFIED, "looks good to me 123")
        with self.assertRaises(ReviewForbidden):
            self.service.decide(self.editor, detection_id, ReviewDecision.VERIFIED, "editor note 123")

    def test_reviewer_cannot_resolve_disputes(self) -> None:
        detection_id = _seed_candidate(self.store)
        dispute = self.service.open_dispute(
            self.creator, detection_id, reason="wrong_identity",
            detail="this is a different song than the one registered here, please check",
        )
        with self.assertRaises(ReviewForbidden):
            self.service.resolve_dispute(
                self.reviewer, dispute.id, DisputeResolution.UPHELD, "reviewer tried to resolve 123"
            )

    def test_unauthenticated_is_forbidden(self) -> None:
        with self.assertRaises(ReviewForbidden):
            self.service.list_detections(None)  # type: ignore[arg-type]

    # --- review decisions ------------------------------------------------

    def test_reviewer_verifies_candidate_with_note(self) -> None:
        detection_id = _seed_candidate(self.store)
        evidence = self.service.decide(
            self.reviewer, detection_id, ReviewDecision.VERIFIED,
            "waveform and landmarks confirm the track across the full 30 seconds",
        )
        self.assertEqual(evidence.status, "verified")
        self.assertEqual(evidence.reviewed_by, "u-reviewer")
        actions = [e["action"] for e in self.store.audit_events]
        self.assertIn("detection.verified", actions)

    def test_decision_requires_min_note(self) -> None:
        detection_id = _seed_candidate(self.store)
        with self.assertRaises(ReviewValidationError):
            self.service.decide(self.reviewer, detection_id, ReviewDecision.VERIFIED, "short")

    def test_cannot_review_non_candidate(self) -> None:
        detection_id = _seed_candidate(self.store)
        self.service.decide(self.reviewer, detection_id, ReviewDecision.REJECTED, "insufficient landmark votes here")
        with self.assertRaises(ReviewStateConflict):
            self.service.decide(self.reviewer, detection_id, ReviewDecision.VERIFIED, "changing my mind later")

    def test_invalid_decision_rejected(self) -> None:
        detection_id = _seed_candidate(self.store)
        with self.assertRaises(ReviewValidationError):
            self.service.decide(self.reviewer, detection_id, "maybe", "a sufficiently long review note")

    # --- dispute lifecycle -----------------------------------------------

    def test_open_dispute_moves_to_disputed_and_flags_hold(self) -> None:
        detection_id = _seed_candidate(self.store)
        dispute = self.service.open_dispute(
            self.creator, detection_id, reason="wrong_split",
            detail="the producer share was changed without the signed split sheet",
        )
        self.assertEqual(dispute.status, "open")
        self.assertEqual(dispute.raised_by_party_id, "party-creator")
        evidence = self.service.get_detection(self.creator, detection_id)
        self.assertEqual(evidence.status, "disputed")
        self.assertTrue(evidence.has_dispute)

    def test_cannot_dispute_rejected_detection(self) -> None:
        detection_id = _seed_candidate(self.store)
        self.service.decide(self.reviewer, detection_id, ReviewDecision.REJECTED, "weak match evidence overall")
        with self.assertRaises(ReviewStateConflict):
            self.service.open_dispute(
                self.creator, detection_id, reason="other", detail="trying to dispute a rejected detection anyway"
            )

    def test_duplicate_open_dispute_rejected(self) -> None:
        detection_id = _seed_candidate(self.store)
        self.service.open_dispute(
            self.creator, detection_id, reason="duplicate",
            detail="same play already counted from a different chunk in this window",
        )
        with self.assertRaises(ReviewStateConflict):
            self.service.open_dispute(
                self.creator, detection_id, reason="duplicate",
                detail="opening a second dispute for the same detection which must be blocked",
            )

    def test_dispute_reason_must_be_known(self) -> None:
        detection_id = _seed_candidate(self.store)
        with self.assertRaises(ReviewValidationError):
            self.service.open_dispute(
                self.creator, detection_id, reason="nonsense", detail="a sufficiently long reason here"
            )

    def test_finance_upholds_dispute_rejects_detection(self) -> None:
        detection_id = _seed_candidate(self.store)
        dispute = self.service.open_dispute(
            self.creator, detection_id, reason="wrong_identity",
            detail="the matched recording is a different title entirely please reject this",
        )
        resolved = self.service.resolve_dispute(
            self.finance, dispute.id, DisputeResolution.UPHELD,
            "confirmed identity error with the rightsholder; amount remains held and rejected",
        )
        self.assertEqual(resolved.status, "upheld")
        evidence = self.service.get_detection(self.finance, detection_id)
        self.assertEqual(evidence.status, "rejected")
        actions = [e["action"] for e in self.store.audit_events]
        self.assertIn("dispute.upheld", actions)

    def test_finance_dismisses_dispute_returns_to_verified(self) -> None:
        detection_id = _seed_candidate(self.store)
        self.service.decide(
            self.reviewer, detection_id, ReviewDecision.VERIFIED,
            "verified after listening comparison with strong landmark agreement",
        )
        dispute = self.service.open_dispute(
            self.creator, detection_id, reason="wrong_duration",
            detail="disputing the counted duration for this particular play window segment",
        )
        resolved = self.service.resolve_dispute(
            self.finance, dispute.id, DisputeResolution.DISMISSED,
            "duration recomputed against segmenter evidence; original verified amount stands",
        )
        self.assertEqual(resolved.status, "dismissed")
        evidence = self.service.get_detection(self.finance, detection_id)
        self.assertEqual(evidence.status, "verified")

    def test_cannot_resolve_non_open_dispute(self) -> None:
        detection_id = _seed_candidate(self.store)
        dispute = self.service.open_dispute(
            self.creator, detection_id, reason="other", detail="initial challenge with enough detail"
        )
        self.service.resolve_dispute(
            self.finance, dispute.id, DisputeResolution.UPHELD, "resolved once with a clear note here"
        )
        with self.assertRaises(ReviewStateConflict):
            self.service.resolve_dispute(
                self.finance, dispute.id, DisputeResolution.UPHELD, "attempting a second resolution now"
            )

    # --- listing / filtering ---------------------------------------------

    def test_list_filters_by_status_and_paginates(self) -> None:
        first = _seed_candidate(self.store, votes=10)
        second = _seed_candidate(self.store, votes=50)
        self.service.decide(self.reviewer, second, ReviewDecision.VERIFIED, "clearly matched with high votes")
        candidates = self.service.list_detections(self.reviewer, status="candidate")
        self.assertEqual(candidates.total, 1)
        self.assertEqual(candidates.items[0].id, first)
        page = self.service.list_detections(self.reviewer, limit=1, offset=1)
        self.assertEqual(page.total, 2)
        self.assertEqual(len(page.items), 1)

    def test_evidence_is_redacted(self) -> None:
        detection_id = _seed_candidate(self.store)
        evidence = self.service.get_detection(self.reviewer, detection_id)
        data = evidence.as_dict()
        flat = str(data)
        # No wallet, object-key, or raw capture data leaks into the portal view.
        self.assertNotIn("encrypted_object_key", flat)
        self.assertNotIn("account_reference", flat)
        self.assertIn("evidence", data)
        self.assertIn("matcher_version", data["evidence"])


if __name__ == "__main__":
    unittest.main()
