"""WSGI application for the reviewer/dispute dashboard backend.

The dashboard is a portal plane: every route requires a portal user (Supabase
Auth in production; a token directory in the reference server). It exposes only
redacted detection evidence and review/dispute actions — never raw audio keys,
wallet data, or PII. Static dashboard assets are served separately; this is the
JSON backend they call.

Endpoints
---------
GET  /v1/review/detections[?status=&source=&recording=&limit=&offset=]
GET  /v1/review/detections/<uuid>
POST /v1/review/detections/<uuid>/decision     {"decision": "verified|rejected", "note": "..."}
POST /v1/review/detections/<uuid>/disputes      {"reason": "...", "detail": "..."}
GET  /v1/review/disputes[?status=]
POST /v1/review/disputes/<uuid>/resolve        {"resolution": "upheld|dismissed", "note": "..."}
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .auth import PortalUserDirectory
from .errors import ReviewError
from .models import DisputeResolution
from .service import ReviewService

MAX_BODY_BYTES = 64 * 1024

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

_ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "state_conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_failed",
}


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


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
        {"error": {"code": _ERROR_CODES.get(status, "error"), "message": message, "details": details or []}},
    )


def _read_json(environ: dict[str, Any]) -> object:
    method = environ.get("REQUEST_METHOD", "")
    if method in ("POST", "PUT", "PATCH"):
        content_type = environ.get("CONTENT_TYPE", "")
        if content_type and "application/json" not in content_type.lower():
            raise HttpError(415, "Content-Type must be application/json")
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError as error:
            raise HttpError(400, "invalid Content-Length") from error
        if length > MAX_BODY_BYTES:
            raise HttpError(413, f"request body exceeds {MAX_BODY_BYTES} bytes")
        raw = environ["wsgi.input"].read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise HttpError(400, f"invalid JSON body: {error}") from error
    return {}


def _is_uuid(value: str) -> bool:
    from uuid import UUID

    try:
        UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


def create_review_app(
    service: ReviewService,
    directory: PortalUserDirectory,
) -> Callable[..., list[bytes]]:
    def application(environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        try:
            return _route(environ, start_response)
        except HttpError as error:
            return _error(start_response, error.status, error.message)
        except ReviewError as error:
            details = getattr(error, "errors", None)
            return _error(start_response, error.http_status, str(error), details)
        except Exception as error:  # noqa: BLE001 - never leak internals
            print(f"[review-api] unhandled error: {type(error).__name__}", file=sys.stderr)
            return _error(start_response, 500, "internal server error")

    def _route(environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        path = urlsplit(environ.get("PATH_INFO", "/")).path.rstrip("/") or "/"
        method = environ.get("REQUEST_METHOD", "GET")
        request_id = environ.get("HTTP_X_REQUEST_ID")

        if path == "/healthz":
            return _json(start_response, 200, {"status": "ok", "service": "kla-sync-review-api"})

        # Every dashboard route requires a portal user.
        user = directory.resolve(environ.get("HTTP_AUTHORIZATION"))

        if path == "/v1/review/detections":
            if method != "GET":
                raise HttpError(405, "use GET")
            query = parse_qs(urlsplit(environ.get("REQUEST_URI", "")).query)
            # Fall back to QUERY_STRING for WSGI servers without REQUEST_URI.
            if not query:
                query = parse_qs(environ.get("QUERY_STRING", ""))
            limit = _int_query(query, "limit", 50)
            offset = _int_query(query, "offset", 0)
            page = service.list_detections(
                user,
                status=_first(query, "status"),
                source_code=_first(query, "source"),
                recording_id=_first(query, "recording"),
                limit=limit,
                offset=offset,
            )
            return _json(
                start_response,
                200,
                {
                    "detections": [item.as_dict() for item in page.items],
                    "total": page.total,
                    "limit": page.limit,
                    "offset": page.offset,
                },
            )

        if path.startswith("/v1/review/detections/"):
            remainder = path[len("/v1/review/detections/") :]
            parts = [part for part in remainder.split("/") if part]

            if len(parts) == 1 and _is_uuid(parts[0]):
                if method != "GET":
                    raise HttpError(405, "use GET")
                evidence = service.get_detection(user, parts[0])
                return _json(start_response, 200, {"detection": evidence.as_dict()})

            if len(parts) == 2 and parts[1] == "decision" and _is_uuid(parts[0]):
                if method != "POST":
                    raise HttpError(405, "use POST")
                body = _read_json(environ)
                evidence = service.decide(
                    user,
                    parts[0],
                    str(body.get("decision", "")) if isinstance(body, dict) else "",
                    str(body.get("note", "")) if isinstance(body, dict) else "",
                    request_id=request_id,
                )
                return _json(start_response, 200, {"status": evidence.status, "detection": evidence.as_dict()})

            if len(parts) == 2 and parts[1] == "disputes" and _is_uuid(parts[0]):
                if method != "POST":
                    raise HttpError(405, "use POST")
                body = _read_json(environ)
                dispute = service.open_dispute(
                    user,
                    parts[0],
                    reason=str(body.get("reason", "")) if isinstance(body, dict) else "",
                    detail=str(body.get("detail", "")) if isinstance(body, dict) else "",
                    request_id=request_id,
                )
                return _json(start_response, 201, {"status": "open", "dispute": dispute.as_dict()})

            raise HttpError(404, "unknown detection route")

        if path == "/v1/review/disputes":
            if method != "GET":
                raise HttpError(405, "use GET")
            query = parse_qs(environ.get("QUERY_STRING", ""))
            disputes = service.list_disputes(
                user, status=_first(query, "status"), limit=_int_query(query, "limit", 50)
            )
            return _json(start_response, 200, {"disputes": [d.as_dict() for d in disputes]})

        if path.startswith("/v1/review/disputes/"):
            remainder = path[len("/v1/review/disputes/") :]
            parts = [part for part in remainder.split("/") if part]
            if len(parts) == 2 and parts[1] == "resolve" and _is_uuid(parts[0]):
                if method != "POST":
                    raise HttpError(405, "use POST")
                body = _read_json(environ)
                resolution_value = str(body.get("resolution", "")) if isinstance(body, dict) else ""
                if resolution_value not in (DisputeResolution.UPHELD, DisputeResolution.DISMISSED):
                    raise HttpError(422, "resolution must be 'upheld' or 'dismissed'")
                dispute = service.resolve_dispute(
                    user,
                    parts[0],
                    resolution_value,
                    str(body.get("note", "")) if isinstance(body, dict) else "",
                    request_id=request_id,
                )
                return _json(start_response, 200, {"status": dispute.status, "dispute": dispute.as_dict()})
            raise HttpError(404, "unknown dispute route")

        if path in ("/", ""):
            return _json(
                start_response,
                200,
                {
                    "service": "kla-sync-review-api",
                    "endpoints": [
                        "GET /v1/review/detections",
                        "GET /v1/review/detections/<uuid>",
                        "POST /v1/review/detections/<uuid>/decision",
                        "POST /v1/review/detections/<uuid>/disputes",
                        "GET /v1/review/disputes",
                        "POST /v1/review/disputes/<uuid>/resolve",
                    ],
                },
            )
        raise HttpError(404, "not found")

    return application


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def _int_query(query: dict[str, list[str]], key: str, default: int) -> int:
    raw = _first(query, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise HttpError(400, f"query parameter '{key}' must be an integer") from error
