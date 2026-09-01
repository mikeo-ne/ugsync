"""Persistence for edge ingestion receipts and candidate detections.

``InMemoryIngestionStore`` supports tests and local pilots;
``PostgresIngestionStore`` writes ``capture_chunks`` and candidate
``detection_events`` rows (psycopg v3, lazily imported). Candidate rows are
created with status ``candidate`` — they never enter settlement without review.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

from ..matching.service import MatchCandidate
from .manifests import ChunkManifest


class UnknownSourceError(LookupError):
    """Raised when a manifest references a source that is not provisioned."""


class InMemoryIngestionStore:
    def __init__(self, sources: dict[str, str] | None = None) -> None:
        # source_code -> source_id
        self.sources: dict[str, str] = dict(sources or {})
        self.chunks: dict[str, dict[str, Any]] = {}
        self.match_jobs: list[dict[str, Any]] = []
        self.audit_events: list[dict[str, Any]] = []
        self._snapshot: Any = None

    def register_source(self, source_code: str, source_id: str | None = None) -> str:
        source_id = source_id or str(uuid4())
        self.sources[source_code] = source_id
        return source_id

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

    def resolve_source(self, tx: Any, source_code: str) -> str:
        source_id = self.sources.get(source_code)
        if source_id is None:
            raise UnknownSourceError(f"source_code '{source_code}' is not provisioned")
        return source_id

    def get_chunk(self, tx: Any, edge_chunk_id: str) -> dict[str, Any] | None:
        return self.chunks.get(edge_chunk_id)

    def record_chunk(
        self,
        tx: Any,
        *,
        edge_chunk_id: str,
        source_id: str,
        device_id: str,
        manifest: ChunkManifest,
    ) -> str:
        receipt_id = str(uuid4())
        self.chunks[edge_chunk_id] = {
            "id": receipt_id,
            "edge_chunk_id": edge_chunk_id,
            "source_id": source_id,
            "source_code": manifest.source_code,
            "device_id": device_id,
            "started_at": manifest.started_at,
            "ended_at": manifest.ended_at,
            "byte_count": manifest.byte_count,
            "content_sha256": manifest.content_sha256,
            "capture_policy": manifest.capture_policy,
            "fingerprint_schema_id": manifest.fingerprint_schema_id,
            "landmark_count": len(manifest.landmarks),
            "candidate_count": 0,
            "received_at": manifest.ended_at,
        }
        return receipt_id

    def record_match_jobs(
        self,
        tx: Any,
        *,
        chunk_id: str,
        candidates: tuple[MatchCandidate, ...],
        schema_id: str,
    ) -> int:
        chunk = next(c for c in self.chunks.values() if c["id"] == chunk_id)
        chunk["candidate_count"] = len(candidates)
        for candidate in candidates:
            self.match_jobs.append(
                {
                    "capture_chunk_id": chunk_id,
                    "recording_id": candidate.track_id,
                    "vote_count": candidate.vote_count,
                    "confidence_hint": candidate.confidence_hint,
                    "tempo_scale": candidate.reference_per_query_tempo_scale,
                    "offset_seconds": candidate.reference_offset_seconds,
                    "matcher_version": candidate.matcher_version,
                    "status": "candidate",
                }
            )
        return len(candidates)

    def write_audit(self, tx: Any, event: dict[str, Any]) -> None:
        self.audit_events.append({"id": str(uuid4()), **event})


class PostgresIngestionStore:
    """PostgreSQL store using the core schema (capture_chunks / detection_events)."""

    def __init__(self, connection: Any) -> None:
        self._conn = connection

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        with self._conn.transaction():
            yield self

    def resolve_source(self, tx: Any, source_code: str) -> str:
        with self._conn.cursor() as cursor:
            cursor.execute("SELECT id FROM monitoring_sources WHERE source_code = %s", (source_code,))
            row = cursor.fetchone()
        if row is None:
            raise UnknownSourceError(f"source_code '{source_code}' is not provisioned")
        return str(row[0])

    def get_chunk(self, tx: Any, edge_chunk_id: str) -> dict[str, Any] | None:
        with self._conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, source_id FROM capture_chunks WHERE edge_chunk_id = %s",
                (edge_chunk_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                "SELECT COUNT(*) FROM detection_events WHERE capture_chunk_id = %s", (row[0],)
            )
            count = cursor.fetchone()[0]
        return {"id": str(row[0]), "source_id": str(row[1]), "candidate_count": int(count)}

    def record_chunk(
        self,
        tx: Any,
        *,
        edge_chunk_id: str,
        source_id: str,
        device_id: str,
        manifest: ChunkManifest,
    ) -> str:
        with self._conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO capture_chunks (
                    edge_chunk_id, source_id, started_at, ended_at, byte_count,
                    content_sha256, capture_policy, encrypted_object_key, fingerprint_schema_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    edge_chunk_id,
                    source_id,
                    manifest.started_at,
                    manifest.ended_at,
                    manifest.byte_count,
                    manifest.content_sha256,
                    manifest.capture_policy,
                    manifest.encrypted_object_key,
                    manifest.fingerprint_schema_id,
                ),
            )
            return str(cursor.fetchone()[0])

    def record_match_jobs(
        self,
        tx: Any,
        *,
        chunk_id: str,
        candidates: tuple[MatchCandidate, ...],
        schema_id: str,
    ) -> int:
        with self._conn.cursor() as cursor:
            for candidate in candidates:
                # started_at/ended_at/duration are taken from the chunk at write time.
                cursor.execute(
                    """
                    INSERT INTO detection_events (
                        idempotency_key, capture_chunk_id, source_id, recording_id,
                        started_at, ended_at, duration_seconds, matcher_version,
                        fingerprint_schema_id, matched_hash_count, match_confidence,
                        reference_per_query_tempo_scale, status
                    )
                    SELECT %s, c.id, c.source_id, %s, c.started_at, c.ended_at,
                           EXTRACT(EPOCH FROM (c.ended_at - c.started_at)),
                           %s, %s, %s, %s, %s, 'candidate'
                      FROM capture_chunks c WHERE c.id = %s
                    """,
                    (
                        str(uuid4()),
                        candidate.track_id,
                        candidate.matcher_version,
                        schema_id,
                        candidate.vote_count,
                        candidate.confidence_hint,
                        candidate.reference_per_query_tempo_scale,
                        chunk_id,
                    ),
                )
        return len(candidates)

    def write_audit(self, tx: Any, event: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        with self._conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO audit_events (action, entity_type, entity_id, request_id, metadata)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    event["action"],
                    event["entity_type"],
                    event.get("entity_id"),
                    event.get("request_id"),
                    Jsonb(event.get("metadata", {})),
                ),
            )
