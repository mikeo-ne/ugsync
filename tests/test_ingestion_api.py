from __future__ import annotations

import io
import json
import math
import unittest
import uuid
from datetime import UTC, datetime, timedelta

from kla_sync.audio.fingerprint import FingerprintConfig, FingerprintExtractor, serialize_landmarks
from kla_sync.http_api.auth import generate_token
from kla_sync.ingestion_api.device_auth import (
    DeviceIdentity,
    InMemoryDeviceRegistry,
    build_signed_request,
)
from kla_sync.ingestion_api.http import create_ingestion_app
from kla_sync.ingestion_api.service import IngestionService
from kla_sync.ingestion_api.stores import InMemoryIngestionStore
from kla_sync.matching.service import LandmarkIndexService
from kla_sync.matching.store import InMemoryLandmarkStore


def request(app: object, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], dict[str, object]]:
    environ: dict[str, object] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(body or b"")),
        "CONTENT_TYPE": "application/json",
        "wsgi.input": io.BytesIO(body or b""),
        "SERVER_NAME": "test",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
    }
    for key, value in (headers or {}).items():
        environ["HTTP_" + key.upper().replace("-", "_")] = value
    captured: dict[str, object] = {}

    def start_response(status: str, response_headers: list[tuple[str, str]]) -> None:
        captured["status"] = int(status.split()[0])
        captured["headers"] = dict(response_headers)

    chunks = app(environ, start_response)  # type: ignore[operator]
    payload = json.loads(b"".join(chunks).decode("utf-8"))
    return captured["status"], captured["headers"], payload  # type: ignore[return-value]


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


class IngestionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = CONFIG
        self.extractor = _EXTRACTOR
        self.reference = _REFERENCE
        self.catalog_fp = _CATALOG_FP

        self.index = LandmarkIndexService(InMemoryLandmarkStore(self.config), self.config)
        self.store = InMemoryIngestionStore(sources={"kampala-radio-01": "source-pk-1"})
        self.service = IngestionService(self.store, self.index)
        self.registry = InMemoryDeviceRegistry()
        self.device = DeviceIdentity(device_id="edge-1", source_id="src-1", secret="device-secret-value")
        self.registry.register(self.device)
        self.catalog_token = generate_token()
        self.app = create_ingestion_app(
            self.service,
            self.index,
            device_registry=self.registry,
            catalog_token=self.catalog_token,
        )
        self.catalog_auth = {"Authorization": f"Bearer {self.catalog_token}"}

    def _enroll_track(self) -> None:
        body = json.dumps(
            {
                "schema_id": self.config.schema_id,
                "duration_seconds": self.catalog_fp.duration_seconds,
                "landmarks": serialize_landmarks(self.catalog_fp.hashes),
            }
        ).encode()
        status, _, payload = request(
            self.app, "POST", "/v1/index/tracks/rec-crop/landmarks", body, self.catalog_auth
        )
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["hash_count"], len(self.catalog_fp.hashes))

    def test_healthz_reports_schema(self) -> None:
        status, _, payload = request(self.app, "GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["schema_id"], self.config.schema_id)

    def test_enrollment_requires_bearer_token(self) -> None:
        status, _, payload = request(self.app, "POST", "/v1/index/tracks/x/landmarks", b"{}")
        self.assertEqual(status, 401)
        self.assertIn("error", payload)

    def test_enrollment_rejects_wrong_schema(self) -> None:
        body = json.dumps(
            {"schema_id": "different-schema", "duration_seconds": 5.0,
             "landmarks": [{"anchor_frame": 1, "frequency_ratio_bin": 1, "delta_frames": 2}]}
        ).encode()
        status, _, payload = request(
            self.app, "POST", "/v1/index/tracks/x/landmarks", body, self.catalog_auth
        )
        self.assertEqual(status, 422)
        self.assertIn("schema", payload["error"]["message"])

    def test_unsigned_chunk_is_rejected(self) -> None:
        status, _, payload = request(self.app, "POST", "/v1/ingest/chunks", b"{}")
        self.assertEqual(status, 401)
        self.assertIn("unauthorized", payload["error"]["message"])

    def test_signed_chunk_ingests_and_matches(self) -> None:
        self._enroll_track()
        query = self.reference[2 * SAMPLE_RATE : 6 * SAMPLE_RATE]
        query_fp = self.extractor.extract_samples(query, 8000)
        start = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
        manifest = {
            "edge_chunk_id": str(uuid.uuid4()),
            "source_code": "kampala-radio-01",
            "started_at": start.isoformat(),
            "ended_at": (start + timedelta(seconds=30)).isoformat(),
            "content_sha256": "b" * 64,
            "byte_count": 999,
            "fingerprint_schema_id": self.config.schema_id,
            "capture_policy": "hashes_only",
            "landmarks": serialize_landmarks(query_fp.hashes),
        }
        body = json.dumps(manifest).encode()
        signed = build_signed_request(
            device=self.device, method="POST", path="/v1/ingest/chunks", body=body
        )
        status, _, payload = request(self.app, "POST", "/v1/ingest/chunks", body, signed)
        self.assertEqual(status, 201, payload)
        self.assertEqual(payload["status"], "received")
        self.assertTrue(payload["receipt_id"])
        self.assertGreater(len(payload["candidates"]), 0)
        self.assertEqual(payload["candidates"][0]["track_id"], "rec-crop")

    def test_replayed_chunk_returns_replayed_status(self) -> None:
        self._enroll_track()
        start = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
        manifest = {
            "edge_chunk_id": str(uuid.uuid4()),
            "source_code": "kampala-radio-01",
            "started_at": start.isoformat(),
            "ended_at": (start + timedelta(seconds=30)).isoformat(),
            "content_sha256": "c" * 64,
            "byte_count": 10,
            "fingerprint_schema_id": self.config.schema_id,
            "landmarks": [{"anchor_frame": 1, "frequency_ratio_bin": 1, "delta_frames": 2}],
        }
        body = json.dumps(manifest).encode()
        signed = build_signed_request(
            device=self.device, method="POST", path="/v1/ingest/chunks", body=body
        )
        status1, _, p1 = request(self.app, "POST", "/v1/ingest/chunks", body, signed)
        # Same body/signature is a replay within the window -> signature replay rejected.
        # A fresh signature for the same chunk id is the legitimate retry:
        signed2 = build_signed_request(
            device=self.device, method="POST", path="/v1/ingest/chunks", body=body
        )
        status2, _, p2 = request(self.app, "POST", "/v1/ingest/chunks", body, signed2)
        self.assertEqual(status1, 201)
        self.assertEqual(status2, 200)
        self.assertEqual(p2["status"], "replayed")
        self.assertEqual(p1["receipt_id"], p2["receipt_id"])

    def test_tampered_body_fails_signature(self) -> None:
        body = json.dumps({"edge_chunk_id": str(uuid.uuid4())}).encode()
        signed = build_signed_request(
            device=self.device, method="POST", path="/v1/ingest/chunks", body=body
        )
        tampered = body.replace(b"}", b',"x":1}')
        status, _, payload = request(self.app, "POST", "/v1/ingest/chunks", tampered, signed)
        self.assertEqual(status, 401)
        self.assertIn("signature", payload["error"]["message"])

    def test_invalid_manifest_returns_422(self) -> None:
        body = json.dumps({"source_code": "kampala-radio-01"}).encode()
        signed = build_signed_request(
            device=self.device, method="POST", path="/v1/ingest/chunks", body=body
        )
        status, _, payload = request(self.app, "POST", "/v1/ingest/chunks", body, signed)
        self.assertEqual(status, 422)
        self.assertTrue(payload["error"]["details"])


if __name__ == "__main__":
    unittest.main()
