"""Fingerprint index service for catalog-scale matching.

The reference :class:`~kla_sync.audio.fingerprint.InMemoryFingerprintIndex`
validates the landmark contract in a single process. This package wraps that
contract in a service boundary that can be backed by a sharded Redis hot index
in production while keeping the same key/offset/voting semantics and
``schema_id`` isolation.
"""

from .service import LandmarkIndexService, MatchCandidate
from .store import InMemoryLandmarkStore, LandmarkIndexStore

__all__ = [
    "InMemoryLandmarkStore",
    "LandmarkIndexService",
    "LandmarkIndexStore",
    "MatchCandidate",
]
