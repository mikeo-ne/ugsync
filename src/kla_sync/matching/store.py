"""Persistence boundary for the landmark inverted index.

A :class:`LandmarkIndexStore` stores registered landmark occurrences keyed by
``(frequency_ratio_bin, delta_frames)``, partitioned by fingerprint
``schema_id`` so different algorithm versions never mix. The in-memory store is
used by tests and single-process workers; the Redis adapter is the production
hot shard. Both expose the same read surface used by
:func:`~kla_sync.matching.service.vote_candidates`.

Keys in Redis are namespaced ``kla:fp:{schema_id}:{ratio_bin}:{delta}``. A
stored occurrence is a compact string ``"{track_id}:{anchor_frame}:{hash_index}"``
and the number of registered hashes per track is kept in a hash so coverage can
be computed without storing full fingerprints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..audio.fingerprint import (
    Fingerprint,
    InMemoryFingerprintIndex,
)

OCCURRENCE_SEPARATOR = ":"


@dataclass(frozen=True, slots=True)
class StoredOccurrence:
    track_id: str
    anchor_frame: int
    hash_index: int


class LandmarkIndexStore(Protocol):
    """Read/write surface shared by the in-memory and Redis backends."""

    schema_id: str

    def register(self, track_id: str, fingerprint: Fingerprint) -> int:
        """Add or replace one track's landmarks; return the stored hash count."""

    def remove(self, track_id: str) -> None:
        """Remove all occurrences for a track (best effort)."""

    def fetch(self, schema_id: str, ratio_bin: int, delta_frames: int) -> tuple[StoredOccurrence, ...]:
        """Return every occurrence for one inverted-index key."""

    def track_hash_count(self, schema_id: str, track_id: str) -> int:
        """Registered landmark count for a track (0 if unknown)."""

    def track_count(self) -> int:
        """Number of registered tracks."""


def encode_occurrence(track_id: str, anchor_frame: int, hash_index: int) -> str:
    if OCCURRENCE_SEPARATOR in track_id:
        raise ValueError("track_id must not contain ':' in the Redis occurrence encoding")
    return f"{track_id}{OCCURRENCE_SEPARATOR}{anchor_frame}{OCCURRENCE_SEPARATOR}{hash_index}"


def decode_occurrence(raw: str) -> StoredOccurrence:
    parts = raw.split(OCCURRENCE_SEPARATOR)
    if len(parts) != 3:
        raise ValueError(f"malformed index occurrence: {raw!r}")
    return StoredOccurrence(track_id=parts[0], anchor_frame=int(parts[1]), hash_index=int(parts[2]))


class InMemoryLandmarkStore:
    """Process-local store backed by the reference inverted index."""

    def __init__(self, config: Any | None = None) -> None:
        self._index = InMemoryFingerprintIndex(config)

    @property
    def schema_id(self) -> str:
        return self._index.schema_id

    def register(self, track_id: str, fingerprint: Fingerprint) -> int:
        self._index.add(track_id, fingerprint)
        return self._index.indexed_hash_count(track_id)

    def remove(self, track_id: str) -> None:
        self._index.remove(track_id)

    def fetch(self, schema_id: str, ratio_bin: int, delta_frames: int) -> tuple[StoredOccurrence, ...]:
        if schema_id != self.schema_id:
            return ()
        occurrences = self._index.occurrences_for((ratio_bin, delta_frames))
        return tuple(
            StoredOccurrence(o.track_id, o.anchor_frame, o.hash_index) for o in occurrences
        )

    def track_hash_count(self, schema_id: str, track_id: str) -> int:
        if schema_id != self.schema_id:
            return 0
        return self._index.track_hash_count(track_id)

    def track_count(self) -> int:
        return len(self._index.track_ids())


class RedisLandmarkStore:
    """Redis hot-shard adapter (redis client injected; part of the production extra).

    Matching lookups are exact-key ``SMEMBERS`` on
    ``kla:fp:{schema}:{ratio_bin}:{delta}`` — never an unbounded scan. A
    per-track set ``kla:fp:trackkeys:{schema}:{track}`` records which bucket
    keys hold that track's occurrences so a replacement or removal can delete
    them precisely, and a hash records each track's registered landmark count
    for coverage. Different ``schema_id`` values live in disjoint keyspaces.
    """

    KEY_PREFIX = "kla:fp"
    META_KEY = "kla:fp:tracks"
    TRACKKEY_PREFIX = "kla:fp:trackkeys"

    def __init__(self, redis_client: Any, schema_id: str) -> None:
        self._redis = redis_client
        self._schema_id = schema_id

    @property
    def schema_id(self) -> str:
        return self._schema_id

    def _bucket_key(self, ratio_bin: int, delta_frames: int) -> str:
        return f"{self.KEY_PREFIX}:{self._schema_id}:{ratio_bin}:{delta_frames}"

    def _track_keys_key(self, track_id: str) -> str:
        return f"{self.TRACKKEY_PREFIX}:{self._schema_id}:{track_id}"

    def _track_meta_field(self, track_id: str) -> str:
        return f"{self._schema_id}{OCCURRENCE_SEPARATOR}{track_id}"

    def register(self, track_id: str, fingerprint: Fingerprint) -> int:
        if fingerprint.schema_id != self._schema_id:
            raise ValueError("fingerprint schema_id does not match this index partition")
        if not fingerprint.hashes:
            raise ValueError("cannot register a fingerprint with no landmarks")

        self.remove(track_id)  # replace any prior version cleanly

        pipe = self._redis.pipeline()
        track_keys: set[str] = set()
        for hash_index, landmark in enumerate(fingerprint.hashes):
            bucket = self._bucket_key(landmark.frequency_ratio_bin, landmark.delta_frames)
            track_keys.add(bucket)
            pipe.sadd(bucket, encode_occurrence(track_id, landmark.anchor_frame, hash_index))
        if track_keys:
            pipe.sadd(self._track_keys_key(track_id), *track_keys)
        pipe.hset(self.META_KEY, self._track_meta_field(track_id), len(fingerprint.hashes))
        pipe.execute()
        return len(fingerprint.hashes)

    def remove(self, track_id: str) -> None:
        track_keys_key = self._track_keys_key(track_id)
        bucket_keys = list(self._redis.smembers(track_keys_key))
        if bucket_keys:
            pipe = self._redis.pipeline()
            for raw_key in bucket_keys:
                bucket = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
                # Remove every occurrence of this track from the bucket.
                for member in self._redis.sscan_iter(bucket, match=f"{track_id}:*"):
                    pipe.srem(bucket, member)
            pipe.delete(track_keys_key)
            pipe.hdel(self.META_KEY, self._track_meta_field(track_id))
            pipe.execute()
        else:
            self._redis.delete(track_keys_key)
            self._redis.hdel(self.META_KEY, self._track_meta_field(track_id))

    def fetch(self, schema_id: str, ratio_bin: int, delta_frames: int) -> tuple[StoredOccurrence, ...]:
        if schema_id != self._schema_id:
            return ()
        raw = self._redis.smembers(self._bucket_key(ratio_bin, delta_frames))
        return tuple(decode_occurrence(value.decode() if isinstance(value, bytes) else value) for value in raw)

    def track_hash_count(self, schema_id: str, track_id: str) -> int:
        if schema_id != self._schema_id:
            return 0
        count = self._redis.hget(self.META_KEY, self._track_meta_field(track_id))
        if count is None:
            return 0
        return int(count)

    def track_count(self) -> int:
        return self._redis.hlen(self.META_KEY)
