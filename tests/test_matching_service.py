from __future__ import annotations

import math
import unittest

from kla_sync.audio.fingerprint import (
    DEFAULT_TEMPO_SCALES,
    FingerprintConfig,
    FingerprintExtractor,
    InMemoryFingerprintIndex,
)
from kla_sync.matching.service import LandmarkIndexService
from kla_sync.matching.store import (
    InMemoryLandmarkStore,
    decode_occurrence,
    encode_occurrence,
)

SAMPLE_RATE = 8_000
# A short but harmonically rich phrase; still produces thousands of landmarks.
FIXTURE_SECONDS = 9.0
CONFIG = FingerprintConfig(
    target_sample_rate=SAMPLE_RATE,
    window_size=512,
    hop_size=128,
    min_frequency_hz=70,
    max_frequency_hz=3_600,
    peak_neighborhood_frequency_bins=3,
    peak_neighborhood_time_frames=1,
    max_peaks_per_frame=8,
    max_delta_frames=30,
    fanout=6,
)


def musical_fixture(sample_rate: int, duration_seconds: float) -> tuple[float, ...]:
    """Same harmonic, rhythmic phrase used by the reference fingerprint tests."""

    notes = (196.0, 246.94, 293.66, 220.0, 329.63, 261.63, 392.0, 293.66)
    samples: list[float] = []
    for index in range(round(sample_rate * duration_seconds)):
        time = index / sample_rate
        note = notes[int(time * 1.6) % len(notes)]
        local = (time * 1.6) % 1.0
        envelope = 0.55 + 0.45 * math.sin(math.pi * local)
        samples.append(
            envelope
            * (
                0.52 * math.sin(2.0 * math.pi * note * time)
                + 0.28 * math.sin(2.0 * math.pi * note * 1.5 * time)
                + 0.17 * math.sin(2.0 * math.pi * note * 2.03 * time)
                + 0.10 * math.sin(2.0 * math.pi * (70.0 + (index % 400) / 4.0) * time)
            )
        )
    return tuple(samples)


# Synthesize and extract once, then reuse across tests (data is read-only).
_REFERENCE = musical_fixture(SAMPLE_RATE, FIXTURE_SECONDS)
_EXTRACTOR = FingerprintExtractor(CONFIG)
_CATALOG_FP = _EXTRACTOR.extract_samples(_REFERENCE, SAMPLE_RATE)


class MatchingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sample_rate = SAMPLE_RATE
        self.config = CONFIG
        self.extractor = _EXTRACTOR
        self.reference = _REFERENCE
        self.catalog_fp = _CATALOG_FP

    def test_occurrence_codec_roundtrip(self) -> None:
        encoded = encode_occurrence("rec-123", 456, 78)
        decoded = decode_occurrence(encoded)
        self.assertEqual((decoded.track_id, decoded.anchor_frame, decoded.hash_index), ("rec-123", 456, 78))

    def test_service_enroll_and_match_crop(self) -> None:
        service = LandmarkIndexService(InMemoryLandmarkStore(self.config), self.config)
        count = service.enroll("rec-crop", self.catalog_fp)
        self.assertGreater(count, 100)
        self.assertEqual(service.track_count, 1)

        query = self.reference[2 * self.sample_rate : 6 * self.sample_rate]
        query_fp = self.extractor.extract_samples(query, self.sample_rate)
        candidates = service.query(query_fp)
        self.assertTrue(candidates, "expected at least one candidate for a matching crop")
        self.assertEqual(candidates[0].track_id, "rec-crop")
        self.assertGreaterEqual(candidates[0].vote_count, 8)
        # confidence hint stays within [0, 1] and matcher version carries the schema.
        self.assertGreater(candidates[0].confidence_hint, 0.0)
        self.assertLessEqual(candidates[0].confidence_hint, 1.0)
        self.assertEqual(candidates[0].matcher_version, self.config.schema_id)

    def test_service_matches_reference_index_voting(self) -> None:
        # The store-backed service and the reference index must agree on the top
        # candidate for the same query (same voting semantics).
        reference_index = InMemoryFingerprintIndex(self.config)
        reference_index.add("rec-ref", self.catalog_fp)

        service = LandmarkIndexService(InMemoryLandmarkStore(self.config), self.config)
        service.enroll("rec-ref", self.catalog_fp)

        query = self.reference[3 * self.sample_rate : 7 * self.sample_rate]
        query_fp = self.extractor.extract_samples(query, self.sample_rate)

        ref_results = reference_index.match(query_fp, min_votes=8)
        service_results = service.query(query_fp, min_votes=8)

        self.assertTrue(ref_results and service_results)
        self.assertEqual(ref_results[0].track_id, service_results[0].track_id)
        self.assertEqual(ref_results[0].vote_count, service_results[0].vote_count)
        self.assertAlmostEqual(
            ref_results[0].reference_per_query_tempo_scale,
            service_results[0].reference_per_query_tempo_scale,
            places=4,
        )

    def test_silent_query_returns_no_candidate(self) -> None:
        from kla_sync.audio.fingerprint import Fingerprint

        service = LandmarkIndexService(InMemoryLandmarkStore(self.config), self.config)
        service.enroll("rec-ref", self.catalog_fp)
        # A fingerprint with no landmarks (silence/garbage) yields no votes.
        empty = Fingerprint(schema_id=self.config.schema_id, duration_seconds=1.0, peaks=(), hashes=())
        self.assertEqual(service.query(empty, min_votes=8), ())

    def test_schema_mismatch_is_rejected(self) -> None:
        other = FingerprintConfig(fanout=9)  # changes schema_id
        service = LandmarkIndexService(InMemoryLandmarkStore(self.config), self.config)
        foreign_fp = FingerprintExtractor(other).extract_samples(self.reference, self.sample_rate)
        with self.assertRaises(ValueError):
            service.enroll("rec-x", foreign_fp)
        with self.assertRaises(ValueError):
            service.query(foreign_fp)

    def test_enroll_replaces_previous_version(self) -> None:
        service = LandmarkIndexService(InMemoryLandmarkStore(self.config), self.config)
        service.enroll("rec-rep", self.catalog_fp)
        first = service.track_count
        # Re-enrolling the same track must not duplicate it.
        service.enroll("rec-rep", self.catalog_fp)
        self.assertEqual(service.track_count, first)
        query_fp = self.extractor.extract_samples(
            self.reference[2 * self.sample_rate : 6 * self.sample_rate], self.sample_rate
        )
        candidates = service.query(query_fp)
        self.assertEqual(len([c for c in candidates if c.track_id == "rec-rep"]), 1)

    def test_default_tempo_scales_cover_plus_minus_ten_percent(self) -> None:
        self.assertAlmostEqual(min(DEFAULT_TEMPO_SCALES), 0.90, places=2)
        self.assertAlmostEqual(max(DEFAULT_TEMPO_SCALES), 1.10, places=2)


if __name__ == "__main__":
    unittest.main()
