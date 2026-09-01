from __future__ import annotations

import io
import json
import unittest
from datetime import UTC, datetime, timedelta

from kla_sync.review.auth import PortalAccount, PortalUserDirectory, issue_portal_token
from kla_sync.review.http import create_review_app
from kla_sync.review.service import ReviewService
from kla_sync.review.store import InMemoryReviewStore


def request(app: object, method: str, path: str, body: object | None = None, token: str | None = None) -> tuple[int, dict[str, object]]:
    raw = json.dumps(body).encode() if body is not None else b""
    environ: dict[str, object] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": path.split("?", 1)[1] if "?" in path else "",
        "CONTENT_LENGTH": str(len(raw)),
        "CONTENT_TYPE": "application/json",
        "wsgi.input": io.BytesIO(raw),
        "SERVER_NAME": "test",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
    }
    if token:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = int(status.split()[0])

    chunks = app(environ, start_response)  # type: ignore[operator]
    payload = json.loads(b"".join(chunks).decode("utf-8"))
    return captured["status"], payload  # type: ignore[return-value]


class ReviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryReviewStore()
        self.service = ReviewService(self.store)
        self.tokens = {role: issue_portal_token() for role in
                       ("viewer", "reviewer", "finance_reviewer", "catalog_admin")}
        accounts = [
            PortalAccount(user_id=f"user-{role}", role=role, token=token, party_id="party-1" if role == "viewer" else None)
            for role, token in self.tokens.items()
        ]
        self.directory = PortalUserDirectory(accounts)
        self.app = create_review_app(self.service, self.directory)

        now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
        self.detection_id = self.store.record_candidate(
            source_id="source-1", source_code="kampala-radio-01", recording_id="rec-1",
            capture_chunk_id="22222222-2222-4222-8222-222222222222",
            started_at=now, ended_at=now + timedelta(seconds=30),
            matcher_version="kla-landmark-ratio-v1", fingerprint_schema_id="schema-1",
            matched_hash_count=40, match_confidence=0.7, tempo_scale=1.0, offset_seconds=0.0,
        )

    def test_healthz_open(self) -> None:
        status, body = request(self.app, "GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_queue_requires_token(self) -> None:
        status, body = request(self.app, "GET", "/v1/review/detections")
        self.assertEqual(status, 403)
        self.assertIn("error", body)

    def test_viewer_can_read_queue(self) -> None:
        status, body = request(self.app, "GET", "/v1/review/detections", token=self.tokens["viewer"])
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["detections"][0]["status"], "candidate")

    def test_viewer_cannot_decide(self) -> None:
        status, _body = request(
            self.app, "POST", f"/v1/review/detections/{self.detection_id}/decision",
            {"decision": "verified", "note": "viewer should not be able to approve this detection"},
            self.tokens["viewer"],
        )
        self.assertEqual(status, 403)

    def test_reviewer_verifies(self) -> None:
        status, body = request(
            self.app, "POST", f"/v1/review/detections/{self.detection_id}/decision",
            {"decision": "verified", "note": "landmarks align across the full capture window here"},
            self.tokens["reviewer"],
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["status"], "verified")

    def test_decision_validation_returns_422(self) -> None:
        status, body = request(
            self.app, "POST", f"/v1/review/detections/{self.detection_id}/decision",
            {"decision": "maybe", "note": "an invalid decision value is rejected properly"},
            self.tokens["reviewer"],
        )
        self.assertEqual(status, 422)
        self.assertIn("decision", body["error"]["message"])

    def test_open_dispute_then_finance_resolves(self) -> None:
        status, body = request(
            self.app, "POST", f"/v1/review/detections/{self.detection_id}/disputes",
            {"reason": "wrong_identity", "detail": "this play is a different recording than the one matched"},
            self.tokens["viewer"],
        )
        self.assertEqual(status, 201, body)
        dispute_id = body["dispute"]["id"]
        self.assertEqual(body["status"], "open")

        # A reviewer may not resolve; finance can uphold.
        status_forbidden, _ = request(
            self.app, "POST", f"/v1/review/disputes/{dispute_id}/resolve",
            {"resolution": "upheld", "note": "reviewer should not be allowed to resolve this"},
            self.tokens["reviewer"],
        )
        self.assertEqual(status_forbidden, 403)

        status2, body2 = request(
            self.app, "POST", f"/v1/review/disputes/{dispute_id}/resolve",
            {"resolution": "upheld", "note": "identity error confirmed with the rights party; reject"},
            self.tokens["finance_reviewer"],
        )
        self.assertEqual(status2, 200, body2)
        self.assertEqual(body2["status"], "upheld")

        status3, body3 = request(
            self.app, "GET", f"/v1/review/detections/{self.detection_id}",
            token=self.tokens["viewer"],
        )
        self.assertEqual(status3, 200)
        self.assertEqual(body3["detection"]["status"], "rejected")

    def test_disputes_listed(self) -> None:
        request(
            self.app, "POST", f"/v1/review/detections/{self.detection_id}/disputes",
            {"reason": "duplicate", "detail": "the same play was already counted from an adjacent chunk"},
            self.tokens["viewer"],
        )
        status, body = request(
            self.app, "GET", "/v1/review/disputes?status=open", token=self.tokens["finance_reviewer"]
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(body["disputes"]), 1)

    def test_unknown_route_404(self) -> None:
        status, body = request(self.app, "GET", "/v1/review/nope", token=self.tokens["viewer"])
        self.assertEqual(status, 404)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
