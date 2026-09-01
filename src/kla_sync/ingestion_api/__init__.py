"""Authenticated edge ingestion API.

Edge nodes (Raspberry Pi listeners and stream workers) upload compact chunk
manifests and landmark hashes over HTTPS using a provisioned per-device HMAC
secret. The API verifies the signature, records a durable receipt, and hands
the chunk to the fingerprint index service for candidate matching. Raw audio is
never accepted by the public edge plane; it stays on device unless an explicit
retention/consent policy requests it through a separate channel.
"""

from .device_auth import (
    DeviceIdentity,
    DeviceRegistry,
    InMemoryDeviceRegistry,
    build_signed_request,
    generate_device_secret,
    verify_request,
)

__all__ = [
    "DeviceIdentity",
    "DeviceRegistry",
    "InMemoryDeviceRegistry",
    "build_signed_request",
    "generate_device_secret",
    "verify_request",
]
