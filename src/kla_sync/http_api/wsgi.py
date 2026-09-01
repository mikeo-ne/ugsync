"""Dependency-free WSGI application for the catalog onboarding API.

Runs with the standard library (``wsgiref`` in development, any WSGI server in
production). The API is a server-to-server surface: bearer-token auth, JSON in
and out, no cookies, no browser session, no PII beyond catalog metadata, and no
payment or wallet data.

Endpoints
---------
``GET  /healthz``                    unauthenticated liveness check
``POST /v1/catalog/onboard``         validate + persist an onboarding batch
``GET  /v1/split-sheets/<uuid>``     fetch a split sheet with line total
``POST /v1/split-sheets/<uuid>/activate``
                                     approve a draft (10,000-bp enforced)

All mutations accept an ``Idempotency-Key`` header (UUID) and an optional
``X-Request-ID`` that is echoed into audit events.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from ..catalog.errors import CatalogError
from ..catalog.service import CatalogService
from ..catalog.store import InMemoryCatalogStore
from .auth import authenticate, generate_token

MAX_BODY_BYTES = 1_048_576  # 1 MiB; catalog metadata is compact JSON.

UUID_RE_MARK = "/<uuid>"


class _HttpError(Exception):
    def __init__(self, status: int, message: str, *, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details or []


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


def _json_response(start_response: Callable[..., Any], status: int, payload: object) -> list[bytes]:
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
    return _json_response(
        start_response,
        status,
        {"error": {"code": _status_code(status), "message": message, "details": details or []}},
    )


def _status_code(status: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        405: "method_not_allowed",
        409: "conflict",
        413: "payload_too_large",
        415: "unsupported_media_type",
        422: "validation_failed",
        500: "internal_error",
    }.get(status, "error")


def _read_json(environ: dict[str, Any]) -> object:
    method = environ.get("REQUEST_METHOD", "")
    if method in ("POST", "PUT", "PATCH"):
        content_type = environ.get("CONTENT_TYPE", "")
        if content_type and "application/json" not in content_type.lower():
            raise _HttpError(415, "Content-Type must be application/json")
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError as error:
            raise _HttpError(400, "invalid Content-Length") from error
        if length > MAX_BODY_BYTES:
            raise _HttpError(413, f"request body exceeds {MAX_BODY_BYTES} bytes")
        raw = environ["wsgi.input"].read(length) if length else b""
        if not raw:
            raise _HttpError(400, "request body is empty")
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise _HttpError(400, f"invalid JSON body: {error}") from error
    return None


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except (ValueError, AttributeError):
        return False


def create_app(
    service: CatalogService,
    *,
    bearer_token: str | None = None,
    allow_anonymous_health: bool = True,
) -> Callable[..., list[bytes]]:
    """Build the WSGI callable around a configured :class:`CatalogService`."""

    def application(environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        try:
            return _route(environ, start_response)
        except _HttpError as error:
            return _error(start_response, error.status, error.message, error.details)
        except CatalogError as error:
            details = getattr(error, "errors", None)
            return _error(start_response, error.http_status, str(error), details)
        except Exception as error:  # noqa: BLE001 - never leak internals
            print(f"[catalog-api] unhandled error: {type(error).__name__}", file=sys.stderr)
            return _error(start_response, 500, "internal server error")

    def _route(environ: dict[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
        path = urlsplit(environ.get("PATH_INFO", "/")).path.rstrip("/") or "/"
        method = environ.get("REQUEST_METHOD", "GET")
        request_id = environ.get("HTTP_X_REQUEST_ID")

        if path == "/healthz":
            if method != "GET":
                raise _HttpError(405, "use GET")
            return _json_response(
                start_response,
                200,
                {"status": "ok", "service": "kla-sync-catalog-api", "version": "0.1.0"},
            )

        # Everything else requires authentication.
        auth = authenticate(environ.get("HTTP_AUTHORIZATION"), bearer_token)
        if not auth.authenticated:
            status = 403 if not bearer_token else 401
            return _error(start_response, status, auth.reason)

        actor_id = environ.get("HTTP_X_ACTOR_ID")
        idempotency_key = environ.get("HTTP_IDEMPOTENCY_KEY")

        if path == "/v1/catalog/onboard":
            if method != "POST":
                raise _HttpError(405, "use POST")
            payload = _read_json(environ)
            result = service.onboard(
                payload,
                idempotency_key=idempotency_key,
                actor_id=actor_id,
                request_id=request_id,
            )
            return _json_response(
                start_response,
                201,
                {"status": "draft_catalog_created", "catalog": result.as_dict()},
            )

        if path.startswith("/v1/split-sheets/"):
            remainder = path[len("/v1/split-sheets/") :]
            parts = [part for part in remainder.split("/") if part]
            if len(parts) == 1 and _is_uuid(parts[0]):
                if method != "GET":
                    raise _HttpError(405, "use GET")
                sheet = service.get_split_sheet(parts[0])
                return _json_response(start_response, 200, {"split_sheet": sheet})
            if len(parts) == 2 and parts[1] == "activate" and _is_uuid(parts[0]):
                if method != "POST":
                    raise _HttpError(405, "use POST")
                body = _read_json(environ) or {}
                if not isinstance(body, dict) or not _is_uuid(str(body.get("approver_party_id", ""))):
                    raise _HttpError(
                        422, "body must include approver_party_id (UUID of the approving party)"
                    )
                sheet = service.activate_split_sheet(
                    parts[0],
                    approver_party_id=str(body["approver_party_id"]),
                    actor_id=actor_id,
                    request_id=request_id,
                )
                return _json_response(start_response, 200, {"status": "active", "split_sheet": sheet})
            raise _HttpError(404, "unknown split-sheet route")

        if path == "/" or path == "":
            return _json_response(
                start_response,
                200,
                {
                    "service": "kla-sync-catalog-api",
                    "endpoints": [
                        "GET /healthz",
                        "POST /v1/catalog/onboard",
                        "GET /v1/split-sheets/<uuid>",
                        "POST /v1/split-sheets/<uuid>/activate",
                    ],
                },
            )
        raise _HttpError(404, "not found")

    return application


def build_default_app(*, require_token: bool = True) -> tuple[Any, str | None]:
    """Construct an app from environment configuration.

    With ``DATABASE_URL`` set and psycopg installed, the app uses PostgreSQL
    (migrations must already have been applied). Otherwise it falls back to the
    in-memory store, which is for tests and local demos only.
    """

    import os

    token = os.environ.get("KLA_SYNC_CATALOG_API_TOKEN")
    database_url = os.environ.get("DATABASE_URL")

    if database_url:
        try:
            import psycopg

            from ..catalog.store import PostgresCatalogStore

            connection = psycopg.connect(database_url, autocommit=False)
            service = CatalogService(PostgresCatalogStore(connection))
        except ImportError as error:
            raise SystemExit(
                "DATABASE_URL set but psycopg is not installed; "
                "run `pip install -e '.[production]'`"
            ) from error
    else:
        if require_token and not token:
            raise SystemExit(
                "KLA_SYNC_CATALOG_API_TOKEN is required. Generate one with "
                "`kla-sync catalog-api --print-dev-token` or set the environment variable."
            )
        service = CatalogService(InMemoryCatalogStore())
        if not token:
            token = generate_token()
            print(
                f"[catalog-api] WARNING: using in-memory store with ephemeral dev token: {token}",
                file=sys.stderr,
            )
            print(
                "[catalog-api] data does not persist across restarts; set DATABASE_URL for production.",
                file=sys.stderr,
            )

    return create_app(service, bearer_token=token), token
