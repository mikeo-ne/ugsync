"""WSGI application for authenticated edge ingestion and landmark enrollment.

Two trust planes on one port:

* **Edge plane** — devices authenticate per-request with an HMAC signature
  (``X-Device-Id`` / ``X-Timestamp`` / ``X-Signature``) and post chunk
  manifests to ``/v1/ingest/chunks``. Raw audio is never accepted here.
* **Catalog plane** — the catalog onboarding worker enrolls reference
  fingerprints into the landmark index using the server bearer token.

Both planes write audit events; neither is exposed to browsers.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from ..audio.fingerprint import LandmarkHash
from ..catalog.errors import CatalogError
from ..http_api.auth import authenticate
from ..matching.service import LandmarkIndexService
from .device_auth import (
    InMemoryReplayCache,
    ReplayCache,
    build_signed_request,
    verify_request,
)
from .manifests import ManifestValidationError, parse_chunk_manifest
from .service import IngestionService
from .stores import UnknownSourceError

MAX_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB; manifests are compact JSON.

_STATUS_TEXT = {
    200: "200 OK",
    201: "201 Created",
    400: "400 Bad Request",
    401: "401 Unauthorized",
    403: "403 Forbidden",
    404: "404 Not Found",
    405: "405 Method Not Allowed",
    409: "409 Conflict",
    413: "413 Payload Too Large",
    415: "415 Unsupported Media Type",
    422: "422 Unprocessable Entity",
    500: "500 Internal Server Error",
}


class HttpError(Exception):
    def __init__(self, status: int, message: str, *, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details or []


def _json(start_response: Callable[..., Any], status: int, payload: object) -> list[bytes]:
    body = json.dumps(payload, indent=2).encode("utf-8")
    start_response(
        _STATUS_TEXT.get(status, f"{status} Status"),
        [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
        ],
    )
    return [body]


def _error(start_response: Callable[..., Any], status: int, message: str, details: list[str] | None = None) -> list[bytes]:
    return _json(
        start_response,
        status,
        {"error": {"code": {401: "unauthorized", 403: "forbidden", 404: "not_found",
                            409: "conflict", 422: "validation_failed", 400: "bad_request",
                            413: "payload_too_large", 415: "unsupported_media_type",
                            405: "method_not_allowed"}.get(status, "error"),
                    "message": message, "details": details or []}},
    )


def _headers(environ: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            name = key[5:].replace("_", "-").title()
            # Preserve conventional casing for our custom headers.
            headers[name] = value
    return headers


def _read_body(environ: dict[str, Any]) -> bytes:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError as error:
        raise HttpError(400, "invalid Content-Length") from error
    if length > MAX_BODY_BYTES:
        raise HttpError(413, f"request body exceeds {MAX_BODY_BYTES} bytes")
    return environ["wsgi.input"].read(length) if length else b""


def _read_json(environ: dict[str, Any]) -> tuple[object, bytes]:
    content_type = environ.get("CONTENT_TYPE", "")
    if content_type and "application/json" not in content_type.lower():
        raise HttpError(415, "Content-Type must be application/json")
    raw = _read_body(environ)
    if not raw:
        raise HttpError(400, "request body is empty")
    try:
        return json.loads(raw.decode("utf-8")), raw
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise HttpError(400, f"invalid JSON body: {error}") from error


def create_ingestion_app(
    ingestion: IngestionService,
    index: LandmarkIndexService,
    *,
    device_registry: Any,
    catalog_token: str | None = None,
    replay_cache: ReplayCache | None = None,
) -> Callable[..., list[bytes]]:
    """Build the ingestion WSGI app.

    ``ingestion`` and ``index`` share the same fingerprint schema; the device
    registry supplies per-node HMAC secrets and the catalog token protects the
    enrollment plane.
    """

    replay = replay_cache or InMemoryReplayCache()

    def application(environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        try:
            return _route(environ, start_response)
        except HttpError as error:
            return _error(start_response, error.status, error.message, error.details)
        except ManifestValidationError as error:
            return _error(start_response, 422, "manifest validation failed", error.errors)
        except UnknownSourceError as error:
            return _error(start_response, 422, str(error))
        except CatalogError as error:
            return _error(start_response, error.http_status, str(error), getattr(error, "errors", None))
        except Exception as error:  # noqa: BLE001 - never leak internals
            print(f"[ingest-api] unhandled error: {type(error).__name__}", file=sys.stderr)
            return _error(start_response, 500, "internal server error")

    def _route(environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        path = urlsplit(environ.get("PATH_INFO", "/")).path.rstrip("/") or "/"
        method = environ.get("REQUEST_METHOD", "GET")
        request_id = environ.get("HTTP_X_REQUEST_ID")

        if path == "/healthz":
            if method != "GET":
                raise HttpError(405, "use GET")
            return _json(
                start_response,
                200,
                {
                    "status": "ok",
                    "service": "kla-sync-ingestion-api",
                    "version": "0.1.0",
                    "schema_id": index.schema_id,
                    "indexed_tracks": index.track_count,
                },
            )

        # Edge plane: device-signed chunk ingestion.
        if path == "/v1/ingest/chunks":
            if method != "POST":
                raise HttpError(405, "use POST")
            raw = _read_body(environ)
            headers = _headers(environ)
            try:
                verified = verify_request(
                    registry=device_registry,
                    method=method,
                    path="/v1/ingest/chunks",
                    headers=headers,
                    body=raw,
                    replay_cache=replay,
                )
            except PermissionError as error:
                return _error(start_response, 401, str(error))
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise HttpError(400, f"invalid JSON body: {error}") from error
            manifest = parse_chunk_manifest(payload, device_id=verified.device.device_id)
            result = ingestion.ingest(manifest, request_id=request_id)
            return _json(
                start_response,
                200 if result.replayed else 201,
                {
                    "status": "replayed" if result.replayed else "received",
                    "receipt_id": result.receipt_id,
                    "edge_chunk_id": result.edge_chunk_id,
                    "landmarks": result.landmark_count,
                    "schema_compatible": result.schema_compatible,
                    "candidates": [
                        {
                            "track_id": c.track_id,
                            "vote_count": c.vote_count,
                            "query_coverage": c.query_coverage,
                            "track_coverage": c.track_coverage,
                            "confidence_hint": c.confidence_hint,
                            "tempo_scale": c.reference_per_query_tempo_scale,
                            "offset_seconds": c.reference_offset_seconds,
                            "matcher_version": c.matcher_version,
                        }
                        for c in result.candidates
                    ],
                },
            )

        # Catalog plane: bearer-token protected landmark enrollment.
        if path.startswith("/v1/index/tracks/"):
            auth = authenticate(environ.get("HTTP_AUTHORIZATION"), catalog_token)
            if not auth.authenticated:
                status = 403 if not catalog_token else 401
                return _error(start_response, status, auth.reason)
            remainder = path[len("/v1/index/tracks/") :]
            parts = [part for part in remainder.split("/") if part]
            if len(parts) == 2 and parts[1] == "landmarks" and method == "POST":
                track_id = parts[0]
                payload, _ = _read_json(environ)
                count = _enroll_landmarks(index, track_id, payload)
                return _json(start_response, 201, {"status": "enrolled", "track_id": track_id, "hash_count": count})
            raise HttpError(404, "unknown index route")

        if path in ("/", ""):
            return _json(
                start_response,
                200,
                {
                    "service": "kla-sync-ingestion-api",
                    "endpoints": [
                        "GET /healthz",
                        "POST /v1/ingest/chunks (device HMAC)",
                        "POST /v1/index/tracks/<id>/landmarks (bearer token)",
                    ],
                },
            )
        raise HttpError(404, "not found")

    return application


def _enroll_landmarks(index: LandmarkIndexService, track_id: str, payload: object) -> int:
    if not isinstance(payload, dict):
        raise HttpError(422, "expected a JSON object")
    schema_id = payload.get("schema_id")
    if not isinstance(schema_id, str) or not schema_id:
        raise HttpError(422, "schema_id is required")
    raw_landmarks = payload.get("landmarks")
    if not isinstance(raw_landmarks, list) or not raw_landmarks:
        raise HttpError(422, "landmarks must be a non-empty array")
    duration = payload.get("duration_seconds", 0.0)
    if not isinstance(duration, (int, float)) or duration <= 0:
        raise HttpError(422, "duration_seconds must be a positive number")

    landmarks: list[LandmarkHash] = []
    for i, raw in enumerate(raw_landmarks):
        if not isinstance(raw, dict):
            raise HttpError(422, f"landmarks[{i}] must be an object")
        try:
            landmarks.append(
                LandmarkHash(
                    anchor_frame=int(raw["anchor_frame"]),
                    frequency_ratio_bin=int(raw["frequency_ratio_bin"]),
                    delta_frames=int(raw["delta_frames"]),
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise HttpError(422, f"landmarks[{i}] needs integer anchor_frame, frequency_ratio_bin, delta_frames") from error

    # The index pins one fingerprint schema; landmarks extracted under a
    # different config must never be enrolled alongside it.
    if schema_id != index.schema_id:
        raise HttpError(
            422, f"schema_id '{schema_id}' does not match index schema '{index.schema_id}'"
        )

    from ..audio.fingerprint import Fingerprint

    fingerprint = Fingerprint(
        schema_id=index.schema_id,
        duration_seconds=float(duration),
        peaks=(),
        hashes=tuple(landmarks),
    )
    return index.enroll(track_id, fingerprint)


__all__ = ["build_signed_request", "create_ingestion_app"]
