from __future__ import annotations

import io
import json
import unittest
import uuid

from kla_sync.catalog.service import CatalogService
from kla_sync.catalog.store import InMemoryCatalogStore
from kla_sync.http_api.auth import authenticate, generate_token
from kla_sync.http_api.wsgi import create_app
from tests._factories import valid_payload as _valid_payload


def make_request(
    app: object,
    method: str,
    path: str,
    body: object | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], dict[str, object]]:
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    environ: dict[str, object] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(raw)),
        "CONTENT_TYPE": "application/json",
        "wsgi.input": io.BytesIO(raw),
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


class AuthTests(unittest.TestCase):
    def test_constant_time_token_check(self) -> None:
        token = generate_token()
        self.assertTrue(authenticate(f"Bearer {token}", token).authenticated)
        self.assertFalse(authenticate(f"Bearer {token[:-1]}x", token).authenticated)
        self.assertFalse(authenticate(None, token).authenticated)
        self.assertFalse(authenticate("Bearer x", None).authenticated)
        self.assertFalse(authenticate("Token abc", token).authenticated)


class CatalogApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.token = generate_token()
        self.service = CatalogService(InMemoryCatalogStore())
        self.app = create_app(self.service, bearer_token=self.token)
        self.auth = {"Authorization": f"Bearer {self.token}"}

    def test_healthz_is_open(self) -> None:
        status, _, body = make_request(self.app, "GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_onboard_requires_authentication(self) -> None:
        status, _, body = make_request(self.app, "POST", "/v1/catalog/onboard", _valid_payload())
        self.assertEqual(status, 401)
        self.assertIn("error", body)

    def test_onboard_creates_catalog(self) -> None:
        status, _, body = make_request(
            self.app, "POST", "/v1/catalog/onboard", _valid_payload(), self.auth
        )
        self.assertEqual(status, 201)
        catalog = body["catalog"]
        self.assertTrue(catalog["catalog_id"])
        self.assertEqual(len(catalog["split_sheets"]), 1)

    def test_validation_errors_return_422_with_details(self) -> None:
        bad = _valid_payload(catalog_name="")
        status, _, body = make_request(self.app, "POST", "/v1/catalog/onboard", bad, self.auth)
        self.assertEqual(status, 422)
        self.assertIn("details", body["error"])
        self.assertTrue(body["error"]["details"])

    def test_idempotency_key_replays_same_catalog(self) -> None:
        key = str(uuid.uuid4())
        headers = {**self.auth, "Idempotency-Key": key}
        status1, _, body1 = make_request(self.app, "POST", "/v1/catalog/onboard", _valid_payload(), headers)
        status2, _, body2 = make_request(self.app, "POST", "/v1/catalog/onboard", _valid_payload(), headers)
        self.assertEqual(status1, 201)
        self.assertEqual(status2, 201)
        self.assertEqual(body1["catalog"]["catalog_id"], body2["catalog"]["catalog_id"])

    def test_get_split_sheet(self) -> None:
        _, _, body = make_request(
            self.app, "POST", "/v1/catalog/onboard", _valid_payload(), self.auth
        )
        sheet_id = next(iter(body["catalog"]["split_sheets"].values()))
        status, _, fetched = make_request(
            self.app, "GET", f"/v1/split-sheets/{sheet_id}", headers=self.auth
        )
        self.assertEqual(status, 200)
        self.assertEqual(fetched["split_sheet"]["status"], "draft")
        self.assertEqual(fetched["split_sheet"]["total_basis_points"], 10000)

    def test_activate_split_sheet_endpoint(self) -> None:
        _, _, body = make_request(
            self.app, "POST", "/v1/catalog/onboard", _valid_payload(), self.auth
        )
        sheet_id = next(iter(body["catalog"]["split_sheets"].values()))
        approver = body["catalog"]["parties"]["p-label"]
        status, _, activated = make_request(
            self.app,
            "POST",
            f"/v1/split-sheets/{sheet_id}/activate",
            {"approver_party_id": approver},
            self.auth,
        )
        self.assertEqual(status, 200)
        self.assertEqual(activated["split_sheet"]["status"], "active")

    def test_bad_json_returns_400(self) -> None:
        environ: dict[str, object] = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/v1/catalog/onboard",
            "CONTENT_LENGTH": "5",
            "CONTENT_TYPE": "application/json",
            "wsgi.input": io.BytesIO(b"{bad"),
            "SERVER_NAME": "test",
            "SERVER_PORT": "80",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "HTTP_AUTHORIZATION": f"Bearer {self.token}",
        }
        captured: dict[str, object] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            captured["status"] = int(status.split()[0])

        chunks = self.app(environ, start_response)  # type: ignore[operator]
        body = json.loads(b"".join(chunks))
        self.assertEqual(captured["status"], 400)
        self.assertIn("invalid JSON", body["error"]["message"])

    def test_unknown_route_returns_404(self) -> None:
        status, _, body = make_request(self.app, "GET", "/v1/nope", headers=self.auth)
        self.assertEqual(status, 404)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
