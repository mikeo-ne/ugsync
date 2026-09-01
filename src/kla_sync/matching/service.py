"""Landmark index service: enrollment and tempo-aware candidate voting.

The voting logic mirrors :class:`~kla_sync.audio.fingerprint.InMemoryFingerprintIndex`
but reads occurrences through the :class:`~kla_sync.matching.store.LandmarkIndexStore`
read surface, so the exact same candidate-generation semantics run over the
in-memory reference index or a Redis hot shard. Scores remain *candidate*
evidence: min-votes and thresholds are triage values, never payout approvals.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from ..audio.fingerprint import (
    DEFAULT_TEMPO_SCALES,
    Fingerprint,
    FingerprintConfig,
)
from .store import LandmarkIndexStore, StoredOccurrence


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    """Explainable candidate for one catalog recording."""

    track_id: str
    vote_count: int
    query_coverage: float
    track_coverage: float
    reference_per_query_tempo_scale: float
    reference_offset_seconds: float
    matcher_version: str

    @property
    def confidence_hint(self) -> float:
        """Uncalibrated triage hint only; calibrate against labelled Ugandan audio."""

        coverage = min(self.query_coverage, self.track_coverage)
        vote_factor = min(1.0, self.vote_count / 20.0)
        return round(coverage * vote_factor, 4)


def vote_candidates(
    store: LandmarkIndexStore,
    query: Fingerprint,
    *,
    config: FingerprintConfig,
    min_votes: int = 8,
    tempo_scales: Iterable[float] = DEFAULT_TEMPO_SCALES,
    limit: int = 5,
) -> tuple[MatchCandidate, ...]:
    """Hash + scaled-time offset consensus across the configured tempo variants."""

    if min_votes < 1:
        raise ValueError("min_votes must be positive")
    if limit < 1 or not query.hashes:
        return ()

    scales = tuple(sorted({round(float(scale), 5) for scale in tempo_scales}))
    if not scales or any(scale <= 0.0 for scale in scales):
        raise ValueError("at least one positive tempo scale is required")

    candidate_key = tuple[str, float, int]
    query_hits: defaultdict[candidate_key, set[int]] = defaultdict(set)
    reference_hits: defaultdict[candidate_key, set[int]] = defaultdict(set)
    schema_id = query.schema_id

    def occurrences(ratio_bin: int, delta: int) -> tuple[StoredOccurrence, ...]:
        if delta < 1:
            return ()
        return store.fetch(schema_id, ratio_bin, delta)

    for scale in scales:
        for query_hash_index, landmark in enumerate(query.hashes):
            expected_ratio = landmark.frequency_ratio_bin
            expected_delta = max(1, round(landmark.delta_frames * scale))
            scaled_anchor = round(landmark.anchor_frame * scale)
            seen: set[candidate_key] = set()
            for ratio_delta in range(-config.ratio_tolerance_bins, config.ratio_tolerance_bins + 1):
                for time_delta in range(
                    -config.tempo_delta_tolerance_frames,
                    config.tempo_delta_tolerance_frames + 1,
                ):
                    for occurrence in occurrences(
                        expected_ratio + ratio_delta, expected_delta + time_delta
                    ):
                        offset = occurrence.anchor_frame - scaled_anchor
                        key = (occurrence.track_id, scale, offset)
                        if key in seen:
                            continue
                        seen.add(key)
                        query_hits[key].add(query_hash_index)
                        reference_hits[key].add(occurrence.hash_index)

    best_per_track: dict[str, candidate_key] = {}
    for key, hits in query_hits.items():
        track_id = key[0]
        current = best_per_track.get(track_id)
        if current is None or len(hits) > len(query_hits[current]):
            best_per_track[track_id] = key

    results: list[MatchCandidate] = []
    query_hash_count = len(query.hashes)
    for track_id, key in best_per_track.items():
        _, scale, offset = key
        vote_count = len(query_hits[key])
        if vote_count < min_votes:
            continue
        track_hash_count = max(1, store.track_hash_count(schema_id, track_id))
        results.append(
            MatchCandidate(
                track_id=track_id,
                vote_count=vote_count,
                query_coverage=round(vote_count / query_hash_count, 6),
                track_coverage=round(len(reference_hits[key]) / track_hash_count, 6),
                reference_per_query_tempo_scale=scale,
                reference_offset_seconds=round(
                    offset * config.hop_size / config.target_sample_rate, 6
                ),
                matcher_version=schema_id,
            )
        )
    results.sort(
        key=lambda c: (-c.vote_count, -c.query_coverage, -c.track_coverage, c.track_id)
    )
    return tuple(results[:limit])


class LandmarkIndexService:
    """Enroll catalog fingerprints and run candidate queries against a store."""

    def __init__(self, store: LandmarkIndexStore, config: FingerprintConfig | None = None) -> None:
        self._store = store
        self.config = config or FingerprintConfig()
        if self._store.schema_id != self.config.schema_id:
            raise ValueError(
                "store schema_id does not match the configured FingerprintConfig; "
                "do not mix fingerprint schema versions"
            )

    @property
    def schema_id(self) -> str:
        return self._store.schema_id

    @property
    def track_count(self) -> int:
        return self._store.track_count()

    def enroll(self, track_id: str, fingerprint: Fingerprint) -> int:
        """Register or replace one catalog recording; return the stored hash count."""

        if fingerprint.schema_id != self.schema_id:
            raise ValueError(
                "fingerprint schema mismatch; re-extract with the configured FingerprintConfig"
            )
        if not fingerprint.hashes:
            raise ValueError("cannot enroll a fingerprint with no landmarks")
        return self._store.register(track_id, fingerprint)

    def remove(self, track_id: str) -> None:
        self._store.remove(track_id)

    def query(
        self,
        query: Fingerprint,
        *,
        min_votes: int = 8,
        tempo_scales: Iterable[float] = DEFAULT_TEMPO_SCALES,
        limit: int = 5,
    ) -> tuple[MatchCandidate, ...]:
        if query.schema_id != self.schema_id:
            raise ValueError(
                "query fingerprint schema mismatch; edge and server workers must share the schema"
            )
        return vote_candidates(
            self._store,
            query,
            config=self.config,
            min_votes=min_votes,
            tempo_scales=tempo_scales,
            limit=limit,
        )
