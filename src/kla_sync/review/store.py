"""Persistence for the review/dispute portal.

``InMemoryReviewStore`` supports tests and local demos; ``PostgresReviewStore``
reads reviewer-safe columns from ``detection_events`` and writes
``detection_disputes`` (migration 005). The portal only projects evidence
summaries: no capture object keys, wallet data, or party PII are selected.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from .models import DetectionEvidence, Dispute


def _utcnow() -> datetime:
    return datetime.now(UTC)


class InMemoryReviewStore:
    def __init__(self) -> None:
        self.detections: dict[str, dict[str, Any]] = {}
        self.disputes: dict[str, dict[str, Any]] = {}
        self.audit_events: list[dict[str, Any]] = []
        self._snapshot: Any = None

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        self._snapshot = copy.deepcopy(self.__dict__)
        try:
            yield self
        except Exception:
            self.__dict__.update(copy.deepcopy(self._snapshot))
            raise
        finally:
            self._snapshot = None

    # --- seeding (demo / pipeline hand-off) ------------------------------

    def record_candidate(
        self,
        *,
        source_id: str,
        source_code: str,
        recording_id: str,
        capture_chunk_id: str,
        started_at: datetime,
        ended_at: datetime,
        matcher_version: str,
        fingerprint_schema_id: str,
        matched_hash_count: int,
        match_confidence: float,
        tempo_scale: float | None,
        offset_seconds: float,
    ) -> str:
        detection_id = str(uuid4())
        self.detections[detection_id] = {
            "id": detection_id,
            "capture_chunk_id": capture_chunk_id,
            "source_id": source_id,
            "source_code": source_code,
            "recording_id": recording_id,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": max(0.1, (ended_at - started_at).total_seconds()),
            "matcher_version": matcher_version,
            "fingerprint_schema_id": fingerprint_schema_id,
            "matched_hash_count": matched_hash_count,
            "match_confidence": match_confidence,
            "reference_per_query_tempo_scale": tempo_scale,
            "reference_offset_seconds": offset_seconds,
            "status": "candidate",
            "reviewed_by": None,
            "reviewed_at": None,
            "review_note": None,
        }
        return detection_id

    # ------------------------------------------------------------------

    def _open_dispute(self, detection_id: str) -> dict[str, Any] | None:
        for dispute in self.disputes.values():
            if dispute["detection_event_id"] == detection_id and dispute["status"] == "open":
                return dispute
        return None

    def get_detection(self, tx: Any, detection_id: str) -> DetectionEvidence | None:
        row = self.detections.get(detection_id)
        return self._to_evidence(row) if row else None

    def list_detections(
        self,
        tx: Any,
        *,
        status: str | None = None,
        source_code: str | None = None,
        recording_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[DetectionEvidence], int]:
        rows = list(self.detections.values())
        if status:
            rows = [row for row in rows if row["status"] == status]
        if source_code:
            rows = [row for row in rows if row["source_code"] == source_code]
        if recording_id:
            rows = [row for row in rows if row["recording_id"] == recording_id]
        rows.sort(key=lambda row: row["started_at"], reverse=True)
        total = len(rows)
        return [self._to_evidence(row) for row in rows[offset : offset + limit]], total

    def set_detection_status(
        self,
        tx: Any,
        detection_id: str,
        *,
        status: str,
        reviewed_by: str,
        review_note: str | None,
    ) -> None:
        row = self.detections[detection_id]
        row["status"] = status
        row["reviewed_by"] = reviewed_by
        row["reviewed_at"] = _utcnow()
        row["review_note"] = review_note

    def create_dispute(
        self,
        tx: Any,
        *,
        detection_event_id: str,
        raised_by_user_id: str,
        raised_by_party_id: str | None,
        reason: str,
        detail: str,
    ) -> str:
        dispute_id = str(uuid4())
        self.disputes[dispute_id] = {
            "id": dispute_id,
            "detection_event_id": detection_event_id,
            "raised_by_user_id": raised_by_user_id,
            "raised_by_party_id": raised_by_party_id,
            "reason": reason,
            "detail": detail,
            "status": "open",
            "resolved_by_user_id": None,
            "resolution_note": None,
            "created_at": _utcnow(),
            "resolved_at": None,
        }
        return dispute_id

    def get_dispute(self, tx: Any, dispute_id: str) -> Dispute | None:
        row = self.disputes.get(dispute_id)
        return self._to_dispute(row) if row else None

    def list_disputes(self, tx: Any, *, status: str | None = None, limit: int = 50) -> list[Dispute]:
        rows = list(self.disputes.values())
        if status:
            rows = [row for row in rows if row["status"] == status]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return [self._to_dispute(row) for row in rows[:limit]]

    def resolve_dispute(
        self,
        tx: Any,
        dispute_id: str,
        *,
        status: str,
        resolved_by: str,
        resolution_note: str,
        detection_resulting_status: str,
    ) -> Dispute:
        row = self.disputes[dispute_id]
        row["status"] = status
        row["resolved_by_user_id"] = resolved_by
        row["resolution_note"] = resolution_note
        row["resolved_at"] = _utcnow()
        detection = self.detections.get(row["detection_event_id"])
        if detection is not None:
            detection["status"] = detection_resulting_status
            detection["reviewed_by"] = resolved_by
            detection["reviewed_at"] = _utcnow()
            detection["review_note"] = f"dispute {dispute_id} {status}: {resolution_note}"
        return self._to_dispute(row)

    def write_audit(self, tx: Any, event: dict[str, Any]) -> None:
        self.audit_events.append({"id": str(uuid4()), **event})

    def _to_evidence(self, row: dict[str, Any]) -> DetectionEvidence:
        dispute = self._open_dispute(row["id"])
        return DetectionEvidence(
            id=row["id"],
            capture_chunk_id=row["capture_chunk_id"],
            source_id=row["source_id"],
            source_code=row["source_code"],
            recording_id=row["recording_id"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            duration_seconds=row["duration_seconds"],
            matcher_version=row["matcher_version"],
            fingerprint_schema_id=row["fingerprint_schema_id"],
            matched_hash_count=row["matched_hash_count"],
            match_confidence=row["match_confidence"],
            tempo_scale=row.get("reference_per_query_tempo_scale"),
            offset_seconds=row.get("reference_offset_seconds", 0.0),
            status=row["status"],
            reviewed_by=row.get("reviewed_by"),
            reviewed_at=row.get("reviewed_at"),
            review_note=row.get("review_note"),
            has_dispute=dispute is not None,
            dispute_status=dispute["status"] if dispute else None,
        )

    @staticmethod
    def _to_dispute(row: dict[str, Any]) -> Dispute:
        return Dispute(
            id=row["id"],
            detection_event_id=row["detection_event_id"],
            raised_by_user_id=row["raised_by_user_id"],
            raised_by_party_id=row.get("raised_by_party_id"),
            reason=row["reason"],
            detail=row["detail"],
            status=row["status"],
            resolved_by_user_id=row.get("resolved_by_user_id"),
            resolution_note=row.get("resolution_note"),
            created_at=row["created_at"],
            resolved_at=row.get("resolved_at"),
        )


class PostgresReviewStore:
    """Reviewer-safe read/write access over detection_events + detection_disputes."""

    # Column order is fixed; _row_to_evidence maps by the same indices.
    DETECTION_SELECT = """
        SELECT d.id::text, d.capture_chunk_id::text, d.source_id::text,
               s.source_code, d.recording_id::text,
               d.started_at, d.ended_at,
               EXTRACT(EPOCH FROM (d.ended_at - d.started_at)),
               d.matcher_version, d.fingerprint_schema_id,
               d.matched_hash_count, d.match_confidence::float,
               d.reference_per_query_tempo_scale,
               d.status, d.reviewed_at, d.review_note
          FROM detection_events d
          JOIN monitoring_sources s ON s.id = d.source_id
    """

    def __init__(self, connection: Any) -> None:
        self._conn = connection

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._conn.transaction():
            yield self

    def get_detection(self, tx: Any, detection_id: str) -> DetectionEvidence | None:
        with self._conn.cursor() as cursor:
            cursor.execute(self.DETECTION_SELECT + " WHERE d.id = %s", (detection_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                "SELECT status FROM detection_disputes "
                "WHERE detection_event_id = %s AND status = 'open'",
                (detection_id,),
            )
            open_dispute = cursor.fetchone()
        return self._row_to_evidence(row, open_dispute[0] if open_dispute else None)

    def list_detections(
        self,
        tx: Any,
        *,
        status: str | None = None,
        source_code: str | None = None,
        recording_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[DetectionEvidence], int]:
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("d.status = %s")
            params.append(status)
        if source_code:
            where.append("s.source_code = %s")
            params.append(source_code)
        if recording_id:
            where.append("d.recording_id = %s")
            params.append(recording_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM detection_events d "
                "JOIN monitoring_sources s ON s.id = d.source_id" + clause,
                params,
            )
            total = int(cursor.fetchone()[0])
            cursor.execute(
                self.DETECTION_SELECT + clause + " ORDER BY d.started_at DESC LIMIT %s OFFSET %s",
                [*params, limit, offset],
            )
            rows = cursor.fetchall()
        return [self._row_to_evidence(row, None) for row in rows], total

    def set_detection_status(
        self,
        tx: Any,
        detection_id: str,
        *,
        status: str,
        reviewed_by: str,
        review_note: str | None,
    ) -> None:
        with self._conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE detection_events
                   SET status = %s, reviewed_at = now(), review_note = %s
                 WHERE id = %s
                """,
                (status, review_note, detection_id),
            )

    def create_dispute(
        self,
        tx: Any,
        *,
        detection_event_id: str,
        raised_by_user_id: str,
        raised_by_party_id: str | None,
        reason: str,
        detail: str,
    ) -> str:
        with self._conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO detection_disputes
                    (detection_event_id, raised_by_user_id, raised_by_party_id,
                     reason, detail, status)
                VALUES (%s, %s, %s, %s, %s, 'open')
                RETURNING id::text
                """,
                (detection_event_id, raised_by_user_id, raised_by_party_id, reason, detail),
            )
            dispute_id = str(cursor.fetchone()[0])
            cursor.execute(
                "UPDATE detection_events SET status = 'disputed' WHERE id = %s",
                (detection_event_id,),
            )
        return dispute_id

    def resolve_dispute(
        self,
        tx: Any,
        dispute_id: str,
        *,
        status: str,
        resolved_by: str,
        resolution_note: str,
        detection_resulting_status: str,
    ) -> Dispute:
        with self._conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE detection_disputes
                   SET status = %s, resolved_by_user_id = %s, resolution_note = %s,
                       resolved_at = now()
                 WHERE id = %s
                 RETURNING id::text, detection_event_id::text, raised_by_user_id::text,
                           raised_by_party_id::text, reason, detail, status,
                           created_at, resolved_at
                """,
                (status, resolved_by, resolution_note, dispute_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise LookupError(dispute_id)
            cursor.execute(
                "UPDATE detection_events SET status = %s, reviewed_at = now() WHERE id = %s",
                (detection_resulting_status, row[1]),
            )
        return Dispute(
            id=row[0],
            detection_event_id=row[1],
            raised_by_user_id=row[2],
            raised_by_party_id=row[3],
            reason=row[4],
            detail=row[5],
            status=row[6],
            resolved_by_user_id=resolved_by,
            resolution_note=resolution_note,
            created_at=row[7],
            resolved_at=row[8],
        )

    def write_audit(self, tx: Any, event: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        with self._conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO audit_events (action, entity_type, entity_id, actor_id,
                                          request_id, metadata)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    event["action"],
                    event["entity_type"],
                    event.get("entity_id"),
                    event.get("actor_id"),
                    event.get("request_id"),
                    Jsonb(event.get("metadata", {})),
                ),
            )

    @staticmethod
    def _row_to_evidence(row: Any, dispute_status: str | None) -> DetectionEvidence:
        return DetectionEvidence(
            id=row[0],
            capture_chunk_id=row[1],
            source_id=row[2],
            source_code=row[3],
            recording_id=row[4],
            started_at=row[5],
            ended_at=row[6],
            duration_seconds=float(row[7] or 0.0),
            matcher_version=row[8],
            fingerprint_schema_id=row[9],
            matched_hash_count=int(row[10]),
            match_confidence=float(row[11]),
            tempo_scale=float(row[12]) if row[12] is not None else None,
            offset_seconds=0.0,  # alignment offset is matcher metadata, not a core column
            status=row[13],
            reviewed_by=None,
            reviewed_at=row[14],
            review_note=row[15],
            has_dispute=dispute_status is not None,
            dispute_status=dispute_status,
        )
