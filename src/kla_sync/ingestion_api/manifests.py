"""Validated edge chunk manifests.

A chunk manifest is the compact, privacy-preserving unit an edge node uploads:
integrity hashes, timestamps, the fingerprint schema id, and the landmark
hashes. Raw audio is *not* part of this manifest — the default capture policy
is ``hashes_only``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from ..audio.fingerprint import LandmarkHash

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
CAPTURE_POLICIES = frozenset({"hashes_only", "encrypted_audio"})


class ManifestValidationError(ValueError):
    """The chunk manifest failed validation; carries the full field list."""

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True, slots=True)
class ChunkManifest:
    edge_chunk_id: str
    source_code: str
    device_id: str
    started_at: datetime
    ended_at: datetime
    content_sha256: str
    byte_count: int
    fingerprint_schema_id: str
    capture_policy: str = "hashes_only"
    encrypted_object_key: str | None = None
    matcher_hint: str | None = None
    landmarks: tuple[LandmarkHash, ...] = ()

    @property
    def duration_seconds(self) -> float:
        return (self.ended_at - self.started_at).total_seconds()


def _require_str(value: object, field: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field}: required non-empty string")
        return None
    return value.strip()


def _parse_timestamp(value: object, field: str, errors: list[str]) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field}: required ISO-8601 timestamp")
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        errors.append(f"{field}: invalid ISO-8601 timestamp")
        return None
    if parsed.tzinfo is None:
        errors.append(f"{field}: timestamp must include a timezone offset")
        return None
    return parsed


def _parse_landmarks(value: object, errors: list[str]) -> tuple[LandmarkHash, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        errors.append("landmarks: expected an array")
        return ()
    landmarks: list[LandmarkHash] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            errors.append(f"landmarks[{index}]: expected object")
            continue
        anchor = raw.get("anchor_frame")
        ratio = raw.get("frequency_ratio_bin")
        delta = raw.get("delta_frames")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (anchor, ratio, delta)):
            errors.append(f"landmarks[{index}]: anchor_frame, frequency_ratio_bin and delta_frames must be integers")
            continue
        # anchor_frame >= 0; frequency_ratio_bin may be negative (ratio < 1);
        # delta_frames is always positive by the landmark contract.
        if anchor < 0 or delta < 1:
            errors.append(f"landmarks[{index}]: fields out of range (anchor_frame >= 0, delta_frames >= 1)")
            continue
        landmarks.append(LandmarkHash(anchor_frame=anchor, frequency_ratio_bin=ratio, delta_frames=delta))
    return tuple(landmarks)


def parse_chunk_manifest(payload: object, *, device_id: str) -> ChunkManifest:
    """Validate and normalize one JSON chunk manifest for a verified device."""

    errors: list[str] = []
    if not isinstance(payload, dict):
        raise ManifestValidationError(["manifest body: expected a JSON object"])

    edge_chunk_id = _require_str(payload.get("edge_chunk_id"), "edge_chunk_id", errors)
    if edge_chunk_id is not None and not UUID_RE.match(edge_chunk_id):
        errors.append("edge_chunk_id: must be a UUID")

    source_code = _require_str(payload.get("source_code"), "source_code", errors)

    started_at = _parse_timestamp(payload.get("started_at"), "started_at", errors)
    ended_at = _parse_timestamp(payload.get("ended_at"), "ended_at", errors)
    if started_at and ended_at and ended_at <= started_at:
        errors.append("ended_at must be after started_at")

    content_sha256 = _require_str(payload.get("content_sha256"), "content_sha256", errors)
    if content_sha256 and not SHA256_RE.match(content_sha256):
        errors.append("content_sha256: expected 64 lowercase hex characters")

    byte_count = payload.get("byte_count")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
        errors.append("byte_count: required non-negative integer")
        byte_count = 0

    schema_id = _require_str(payload.get("fingerprint_schema_id"), "fingerprint_schema_id", errors)

    policy = payload.get("capture_policy", "hashes_only")
    if policy not in CAPTURE_POLICIES:
        errors.append(f"capture_policy: must be one of {sorted(CAPTURE_POLICIES)}")
        policy = "hashes_only"

    object_key = payload.get("encrypted_object_key")
    if object_key is not None and (not isinstance(object_key, str) or not object_key.strip()):
        errors.append("encrypted_object_key: must be a non-empty string when provided")
        object_key = None
    if policy == "hashes_only" and object_key:
        errors.append("encrypted_object_key is not permitted under the hashes_only policy")
    if policy == "encrypted_audio" and not object_key:
        errors.append("encrypted_audio policy requires an encrypted_object_key")

    landmarks = _parse_landmarks(payload.get("landmarks"), errors)

    if errors:
        raise ManifestValidationError(errors)

    return ChunkManifest(
        edge_chunk_id=edge_chunk_id,  # type: ignore[arg-type]
        source_code=source_code,  # type: ignore[arg-type]
        device_id=device_id,
        started_at=started_at,  # type: ignore[arg-type]
        ended_at=ended_at,  # type: ignore[arg-type]
        content_sha256=content_sha256,  # type: ignore[arg-type]
        byte_count=byte_count,
        fingerprint_schema_id=schema_id,  # type: ignore[arg-type]
        capture_policy=policy,
        encrypted_object_key=object_key,
        matcher_hint=payload.get("matcher_hint") if isinstance(payload.get("matcher_hint"), str) else None,
        landmarks=landmarks,
    )
