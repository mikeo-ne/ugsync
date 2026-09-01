"""Ingestion service: durable receipt, idempotency, and match hand-off.

A verified chunk manifest is recorded exactly once (``edge_chunk_id`` is the
idempotency key), then its landmarks are reconstructed into a
:class:`~kla_sync.audio.fingerprint.Fingerprint` and passed to the landmark
index service for candidate matching. The service writes append-only audit
events. Candidate detections are *evidence* for the review queue — never
auto-approved payouts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from ..audio.fingerprint import Fingerprint, LandmarkHash
from ..matching.service import LandmarkIndexService, MatchCandidate
from .manifests import ChunkManifest, ManifestValidationError


class IngestionStore(Protocol):
    def get_chunk(self, tx: Any, edge_chunk_id: str) -> dict[str, Any] | None: ...

    def record_chunk(
        self,
        tx: Any,
        *,
        edge_chunk_id: str,
        source_id: str,
        device_id: str,
        manifest: ChunkManifest,
    ) -> str: ...

    def record_match_jobs(
        self, tx: Any, *, chunk_id: str, candidates: tuple[MatchCandidate, ...], schema_id: str
    ) -> int: ...

    def write_audit(self, tx: Any, event: dict[str, Any]) -> None: ...

    @property
    def transaction(self) -> Any: ...


@dataclass(frozen=True, slots=True)
class IngestionResult:
    edge_chunk_id: str
    receipt_id: str
    replayed: bool
    landmark_count: int
    candidate_count: int
    candidates: tuple[MatchCandidate, ...]
    schema_compatible: bool
    source_id: str


def manifest_to_fingerprint(manifest: ChunkManifest) -> Fingerprint:
    """Rebuild a query fingerprint from the manifest's landmarks."""

    duration = max(0.0, manifest.duration_seconds)
    hashes: tuple[LandmarkHash, ...] = tuple(manifest.landmarks)
    return Fingerprint(
        schema_id=manifest.fingerprint_schema_id,
        duration_seconds=duration,
        peaks=(),  # peaks are not uploaded; hashes are the retention/transport unit
        hashes=hashes,
    )


class IngestionService:
    """Coordinates receipt persistence and fingerprint matching."""

    def __init__(
        self,
        store: IngestionStore,
        index: LandmarkIndexService | None = None,
    ) -> None:
        self._store = store
        self._index = index

    def ingest(
        self,
        manifest: ChunkManifest,
        *,
        request_id: str | None = None,
        min_votes: int = 8,
    ) -> IngestionResult:
        # Schema compatibility: a query built for a different schema cannot match.
        schema_compatible = (
            self._index is None or manifest.fingerprint_schema_id == self._index.schema_id
        )

        with self._store.transaction() as tx:
            existing = self._store.get_chunk(tx, manifest.edge_chunk_id)
            if existing is not None:
                return IngestionResult(
                    edge_chunk_id=manifest.edge_chunk_id,
                    receipt_id=str(existing["id"]),
                    replayed=True,
                    landmark_count=len(manifest.landmarks),
                    candidate_count=int(existing.get("candidate_count", 0)),
                    candidates=(),
                    schema_compatible=schema_compatible,
                    source_id=str(existing.get("source_id", manifest.source_code)),
                )

            source_id = self._store.resolve_source(tx, manifest.source_code) if hasattr(
                self._store, "resolve_source"
            ) else manifest.source_code

            chunk_id = self._store.record_chunk(
                tx,
                edge_chunk_id=manifest.edge_chunk_id,
                source_id=source_id,
                device_id=manifest.device_id,
                manifest=manifest,
            )

            candidates: tuple[MatchCandidate, ...] = ()
            if self._index is not None and schema_compatible and manifest.landmarks:
                query = manifest_to_fingerprint(manifest)
                candidates = self._index.query(query, min_votes=min_votes)
                self._store.record_match_jobs(
                    tx,
                    chunk_id=chunk_id,
                    candidates=candidates,
                    schema_id=manifest.fingerprint_schema_id,
                )

            self._store.write_audit(
                tx,
                {
                    "action": "capture.ingested",
                    "entity_type": "capture_chunk",
                    "entity_id": chunk_id,
                    "metadata": {
                        "edge_chunk_id": manifest.edge_chunk_id,
                        "source_code": manifest.source_code,
                        "device_id": manifest.device_id,
                        "schema_id": manifest.fingerprint_schema_id,
                        "landmarks": len(manifest.landmarks),
                        "candidates": len(candidates),
                        "schema_compatible": schema_compatible,
                    },
                    "request_id": request_id,
                },
            )

        return IngestionResult(
            edge_chunk_id=manifest.edge_chunk_id,
            receipt_id=chunk_id,
            replayed=False,
            landmark_count=len(manifest.landmarks),
            candidate_count=len(candidates),
            candidates=candidates,
            schema_compatible=schema_compatible,
            source_id=source_id,
        )


__all__ = [
    "IngestionResult",
    "IngestionService",
    "IngestionStore",
    "ManifestValidationError",
]


# Re-exported for callers constructing timestamps.
def utc_now() -> datetime:  # pragma: no cover - trivial helper
    from datetime import UTC

    return datetime.now(UTC)
