from __future__ import annotations

import math
import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from kla_sync.audio.fingerprint import FingerprintConfig, FingerprintExtractor
from kla_sync.ingestion_api.manifests import ManifestValidationError, parse_chunk_manifest
from kla_sync.ingestion_api.service import IngestionService, manifest_to_fingerprint
from kla_sync.ingestion_api.stores import InMemoryIngestionStore, UnknownSourceError
from kla_sync.matching.service import LandmarkIndexService
from kla_sync.matching.store import InMemoryLandmarkStore

SAMPLE_RATE = 8_000
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
                0.52 * math.sin(2 * math.pi * note * time)
                + 0.28 * math.sin(2 * math.pi * note * 1.5 * time)
                + 0.17 * math.sin(2 * math.pi * note * 2.03 * time)
                + 0.10 * math.sin(2 * math.pi * (70.0 + (index % 400) / 4.0) * time)
            )
        )
    return tuple(samples)


_REFERENCE = musical_fixture(SAMPLE_RATE, 9.0)
_EXTRACTOR = FingerprintExtractor(CONFIG)
_CATALOG_FP = _EXTRACTOR.extract_samples(_REFERENCE, SAMPLE_RATE)


def valid_manifest_payload(**overrides: object) -> dict[str, object]:
    start = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    payload: dict[str, object] = {
        "edge_chunk_id": str(uuid4()),
        "source_code": "kampala-radio-01",
        "started_at": start.isoformat(),
        "ended_at": (start + timedelta(seconds=30)).isoformat(),
        "content_sha256": "a" * 64,
        "byte_count": 12345,
        "fingerprint_schema_id": FingerprintConfig().schema_id,
        "capture_policy": "hashes_only",
        "landmarks": [
            {"anchor_frame": 10, "frequency_ratio_bin": 3, "delta_frames": 4},
            {"anchor_frame": 20, "frequency_ratio_bin": -2, "delta_frames": 9},
        ],
    }
    payload.update(overrides)
    return payload


class ManifestValidationTests(unittest.TestCase):
    def test_valid_manifest_parses(self) -> None:
        manifest = parse_chunk_manifest(valid_manifest_payload(), device_id="edge-1")
        self.assertEqual(manifest.device_id, "edge-1")
        self.assertEqual(len(manifest.landmarks), 2)
        self.assertAlmostEqual(manifest.duration_seconds, 30.0)

    def test_collects_field_errors(self) -> None:
        bad = valid_manifest_payload(
            edge_chunk_id="not-a-uuid",
            content_sha256="xyz",
            byte_count=-3,
            started_at=datetime(2026, 9, 1, 10, 0, 1, tzinfo=UTC).isoformat(),
            ended_at=datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC).isoformat(),
        )
        with self.assertRaises(ManifestValidationError) as ctx:
            parse_chunk_manifest(bad, device_id="edge-1")
        joined = " ".join(ctx.exception.errors)
        self.assertIn("edge_chunk_id", joined)
        self.assertIn("content_sha256", joined)
        self.assertIn("byte_count", joined)
        self.assertIn("ended_at", joined)

    def test_hashes_only_policy_rejects_object_key(self) -> None:
        with self.assertRaises(ManifestValidationError) as ctx:
            parse_chunk_manifest(
                valid_manifest_payload(capture_policy="hashes_only", encrypted_object_key="k"),
                device_id="edge-1",
            )
        self.assertTrue(any("hashes_only" in e for e in ctx.exception.errors))

    def test_encrypted_audio_requires_object_key(self) -> None:
        with self.assertRaises(ManifestValidationError):
            parse_chunk_manifest(
                valid_manifest_payload(capture_policy="encrypted_audio", encrypted_object_key=None),
                device_id="edge-1",
            )

    def test_bad_landmark_rejected(self) -> None:
        with self.assertRaises(ManifestValidationError) as ctx:
            parse_chunk_manifest(
                valid_manifest_payload(landmarks=[{"anchor_frame": 1, "frequency_ratio_bin": 2}]),
                device_id="edge-1",
            )
        self.assertTrue(any("landmarks[0]" in e for e in ctx.exception.errors))


class IngestionServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = CONFIG
        self.extractor = _EXTRACTOR
        self.reference = _REFERENCE
        self.catalog_fp = _CATALOG_FP
        self.index = LandmarkIndexService(InMemoryLandmarkStore(self.config), self.config)
        self.index.enroll("rec-crop", self.catalog_fp)
        self.store = InMemoryIngestionStore(sources={"kampala-radio-01": "source-pk-1"})
        self.service = IngestionService(self.store, self.index)

    def _matching_manifest(self) -> dict[str, object]:
        query = self.reference[2 * SAMPLE_RATE : 6 * SAMPLE_RATE]
        query_fp = self.extractor.extract_samples(query, SAMPLE_RATE)
        landmarks = [
            {"anchor_frame": h.anchor_frame, "frequency_ratio_bin": h.frequency_ratio_bin,
             "delta_frames": h.delta_frames}
            for h in query_fp.hashes
        ]
        return valid_manifest_payload(
            fingerprint_schema_id=self.config.schema_id,
            landmarks=landmarks,
        )

    def test_ingest_records_receipt_and_candidates(self) -> None:
        payload = self._matching_manifest()
        manifest = parse_chunk_manifest(payload, device_id="edge-1")
        result = self.service.ingest(manifest)
        self.assertFalse(result.replayed)
        self.assertTrue(result.receipt_id)
        self.assertGreater(result.landmark_count, 10)
        self.assertEqual(result.source_id, "source-pk-1")
        top = result.candidates[0]
        self.assertEqual(top.track_id, "rec-crop")
        self.assertGreaterEqual(top.vote_count, 8)
        # Candidate detections were persisted for the review queue.
        self.assertGreaterEqual(len(self.store.match_jobs), 1)
        self.assertEqual(self.store.match_jobs[0]["status"], "candidate")

    def test_duplicate_chunk_is_idempotent_replay(self) -> None:
        payload = self._matching_manifest()
        manifest = parse_chunk_manifest(payload, device_id="edge-1")
        first = self.service.ingest(manifest)
        second = self.service.ingest(parse_chunk_manifest(payload, device_id="edge-1"))
        self.assertTrue(second.replayed)
        self.assertEqual(first.receipt_id, second.receipt_id)
        self.assertEqual(len(self.store.chunks), 1)

    def test_unknown_source_rejected(self) -> None:
        payload = self._matching_manifest()
        payload["source_code"] = "ghost-station"
        manifest = parse_chunk_manifest(payload, device_id="edge-1")
        with self.assertRaises(UnknownSourceError):
            self.service.ingest(manifest)

    def test_incompatible_schema_skips_matching_but_still_receives(self) -> None:
        payload = self._matching_manifest()
        payload["fingerprint_schema_id"] = "kla-landmark-ratio-v1:deadbeefdeadbeef"
        manifest = parse_chunk_manifest(payload, device_id="edge-1")
        result = self.service.ingest(manifest)
        self.assertFalse(result.schema_compatible)
        self.assertEqual(result.candidate_count, 0)
        self.assertTrue(result.receipt_id)  # still accepted

    def test_audit_event_written(self) -> None:
        self.service.ingest(parse_chunk_manifest(self._matching_manifest(), device_id="edge-1"))
        self.assertEqual(self.store.audit_events[0]["action"], "capture.ingested")

    def test_manifest_to_fingerprint_round_trip(self) -> None:
        manifest = parse_chunk_manifest(valid_manifest_payload(), device_id="edge-1")
        fp = manifest_to_fingerprint(manifest)
        self.assertEqual(fp.schema_id, manifest.fingerprint_schema_id)
        self.assertEqual(len(fp.hashes), 2)
        self.assertEqual(fp.peaks, ())


if __name__ == "__main__":
    unittest.main()
