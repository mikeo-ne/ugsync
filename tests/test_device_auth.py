from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime, timedelta

from kla_sync.ingestion_api.device_auth import (
    DeviceIdentity,
    InMemoryDeviceRegistry,
    InMemoryReplayCache,
    build_signed_request,
    canonical_string,
    generate_device_secret,
    sha256_hex,
    sign,
    verify_request,
)


class DeviceAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = InMemoryDeviceRegistry()
        self.device = DeviceIdentity(device_id="edge-1", source_id="src-1", secret=generate_device_secret())
        self.registry.register(self.device)
        self.now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

    def _verify(self, headers: dict[str, str], body: bytes, **kwargs: object) -> object:
        return verify_request(
            registry=self.registry,
            method="POST",
            path="/v1/ingest/chunks",
            headers=headers,
            body=body,
            now=self.now,
            **kwargs,
        )

    def test_valid_signature_is_accepted(self) -> None:
        body = b'{"edge_chunk_id":"123"}'
        headers = build_signed_request(
            device=self.device, method="POST", path="/v1/ingest/chunks", body=body, timestamp=self.now
        )
        verified = self._verify(headers, body)
        self.assertEqual(verified.device.device_id, "edge-1")

    def test_tampered_body_is_rejected(self) -> None:
        body = b'{"a":1}'
        headers = build_signed_request(
            device=self.device, method="POST", path="/v1/ingest/chunks", body=body, timestamp=self.now
        )
        with self.assertRaises(PermissionError):
            self._verify(headers, b'{"a":2}')

    def test_wrong_device_secret_is_rejected(self) -> None:
        body = b"{}"
        headers = build_signed_request(
            device=DeviceIdentity("edge-1", "src-1", "wrong-secret"),
            method="POST",
            path="/v1/ingest/chunks",
            body=body,
            timestamp=self.now,
        )
        with self.assertRaises(PermissionError):
            self._verify(headers, body)

    def test_unknown_and_deactivated_devices_rejected(self) -> None:
        body = b"{}"
        headers = build_signed_request(
            device=DeviceIdentity("edge-ghost", "src-1", self.device.secret),
            method="POST",
            path="/v1/ingest/chunks",
            body=body,
            timestamp=self.now,
        )
        with self.assertRaises(PermissionError):
            self._verify(headers, body)
        self.registry.deactivate("edge-1")
        headers = build_signed_request(
            device=self.device, method="POST", path="/v1/ingest/chunks", body=body, timestamp=self.now
        )
        with self.assertRaises(PermissionError):
            self._verify(headers, body)

    def test_stale_timestamp_outside_window_rejected(self) -> None:
        body = b"{}"
        old = build_signed_request(
            device=self.device,
            method="POST",
            path="/v1/ingest/chunks",
            body=body,
            timestamp=self.now - timedelta(minutes=6),
        )
        with self.assertRaises(PermissionError):
            self._verify(old, body)
        future = build_signed_request(
            device=self.device,
            method="POST",
            path="/v1/ingest/chunks",
            body=body,
            timestamp=self.now + timedelta(minutes=6),
        )
        with self.assertRaises(PermissionError):
            self._verify(future, body)

    def test_replayed_signature_is_rejected(self) -> None:
        body = b'{"edge_chunk_id":"x"}'
        headers = build_signed_request(
            device=self.device, method="POST", path="/v1/ingest/chunks", body=body, timestamp=self.now
        )
        cache = InMemoryReplayCache()
        self._verify(headers, body, replay_cache=cache)
        with self.assertRaises(PermissionError):
            self._verify(headers, body, replay_cache=cache)

    def test_canonical_string_binds_all_fields(self) -> None:
        body_hash = sha256_hex(b"abc")
        canonical = canonical_string(
            device_id="d", timestamp="2026-09-01T12:00:00Z", path="/p", body_sha256=body_hash
        )
        self.assertEqual(
            canonical,
            f"v1\nd\n2026-09-01T12:00:00Z\n/p\n{body_hash}",
        )
        self.assertEqual(len(sign("s", canonical)), 64)

    def test_missing_headers_rejected(self) -> None:
        with self.assertRaises(PermissionError):
            self._verify({}, b"{}")

    def test_signed_request_helper_headers_are_json_safe(self) -> None:
        headers = build_signed_request(
            device=self.device, method="POST", path="/x", body=b"{}", timestamp=self.now
        )
        roundtrip = json.loads(json.dumps(headers))
        self.assertEqual(set(roundtrip), {"X-Device-Id", "X-Timestamp", "X-Signature"})


if __name__ == "__main__":
    unittest.main()
