"""Per-device HMAC authentication for edge ingestion.

Each edge node is provisioned with a random secret (via the deployment secret
manager; never embedded in client-facing code). A request is authenticated by
an ``X-Device-Id`` header and an HMAC-SHA256 signature over a canonical string
binding the device, an ISO-8601 timestamp, the SHA-256 of the exact request
body, and the target path.

The timestamp is compared within a replay window and every (device, timestamp,
body) tuple is single-use within the window, so a captured signature cannot be
replayed.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

REPLAY_WINDOW = timedelta(minutes=5)
SIGNATURE_VERSION = "v1"


def generate_device_secret(num_bytes: int = 32) -> str:
    """Generate a strong per-device signing secret."""

    return secrets.token_urlsafe(num_bytes)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_string(*, device_id: str, timestamp: str, path: str, body_sha256: str) -> str:
    """Deterministic, newline-delimited string that is signed.

    Field order is fixed and each field is stripped of newlines so a caller
    cannot smuggle ambiguity into the verified payload.
    """

    fields = (SIGNATURE_VERSION, device_id, timestamp, path, body_sha256)
    if any("\n" in field or "\r" in field for field in fields[1:]):
        raise ValueError("canonical string fields must not contain newlines")
    return "\n".join(fields)


def sign(secret: str, canonical: str) -> str:
    return hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    # Accept a trailing Z and normalize to aware UTC.
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone offset")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DeviceIdentity:
    device_id: str
    source_id: str
    secret: str
    is_active: bool = True


class DeviceRegistry(Protocol):
    def get(self, device_id: str) -> DeviceIdentity | None: ...


class InMemoryDeviceRegistry:
    """Pilot/dev registry. Provision secrets via a secret manager in production."""

    def __init__(self, devices: dict[str, DeviceIdentity] | None = None) -> None:
        self._devices = dict(devices or {})

    def register(self, device: DeviceIdentity) -> None:
        self._devices[device.device_id] = device

    def get(self, device_id: str) -> DeviceIdentity | None:
        return self._devices.get(device_id)

    def deactivate(self, device_id: str) -> None:
        device = self._devices.get(device_id)
        if device is not None:
            self._devices[device_id] = DeviceIdentity(
                device_id=device.device_id,
                source_id=device.source_id,
                secret=device.secret,
                is_active=False,
            )


@dataclass(frozen=True, slots=True)
class VerifiedRequest:
    device: DeviceIdentity
    timestamp: datetime


class ReplayCache(Protocol):
    def seen(self, key: str) -> bool: ...


class InMemoryReplayCache:
    """Bounded single-use signature cache for the replay window."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def seen(self, key: str) -> bool:
        if key in self._seen:
            return True
        self._seen.add(key)
        return False


def build_signed_request(
    *,
    device: DeviceIdentity,
    method: str,
    path: str,
    body: bytes,
    timestamp: datetime | None = None,
) -> dict[str, str]:
    """Construct the auth headers for one request (used by edge workers/tests)."""

    moment = (timestamp or datetime.now(UTC)).astimezone(UTC)
    timestamp_iso = moment.isoformat().replace("+00:00", "Z")
    body_hash = sha256_hex(body)
    canonical = canonical_string(
        device_id=device.device_id, timestamp=timestamp_iso, path=path, body_sha256=body_hash
    )
    signature = sign(device.secret, canonical)
    return {
        "X-Device-Id": device.device_id,
        "X-Timestamp": timestamp_iso,
        "X-Signature": f"{SIGNATURE_VERSION}:{signature}",
    }


def verify_request(
    *,
    registry: DeviceRegistry,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    now: datetime | None = None,
    replay_cache: ReplayCache | None = None,
) -> VerifiedRequest:
    """Validate device identity, freshness, body binding, and signature.

    Raises :class:`PermissionError` on any failure with a generic reason; do not
    reveal whether the device id or the signature was the failing factor.
    """

    def reject(reason: str) -> None:
        raise PermissionError(f"unauthorized: {reason}")

    device_id = headers.get("X-Device-Id", "").strip()
    timestamp_raw = headers.get("X-Timestamp", "").strip()
    signature_raw = headers.get("X-Signature", "").strip()
    if not device_id or not timestamp_raw or not signature_raw:
        reject("missing device auth headers")

    device = registry.get(device_id)
    if device is None or not device.is_active:
        reject("device not recognized")

    version, _, signature_hex = signature_raw.partition(":")
    if version != SIGNATURE_VERSION or len(signature_hex) != 64:
        reject("malformed signature")

    try:
        timestamp = _parse_timestamp(timestamp_raw)
    except ValueError:
        reject("invalid timestamp")
        return None  # pragma: no cover - reject raises

    current = (now or datetime.now(UTC)).astimezone(UTC)
    if abs(current - timestamp) > REPLAY_WINDOW:
        reject("stale or future-dated request")

    body_hash = sha256_hex(body)
    canonical = canonical_string(
        device_id=device_id, timestamp=timestamp_raw, path=path, body_sha256=body_hash
    )
    expected = sign(device.secret, canonical)
    if not hmac.compare_digest(expected, signature_hex):
        reject("signature mismatch")

    if replay_cache is not None:
        replay_key = f"{device_id}:{timestamp_raw}:{body_hash}"
        if replay_cache.seen(replay_key):
            reject("replayed request")

    return VerifiedRequest(device=device, timestamp=timestamp)


def random_device_id() -> str:
    """Generate a non-secret device identifier (the secret is separate)."""

    return f"edge-{os.urandom(8).hex()}"
