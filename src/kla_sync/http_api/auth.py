"""Bearer-token authentication for server-to-server catalog onboarding.

This is a service credential, not a user login: edge workers and catalog
operators call the onboarding API with a long random token provisioned through
the deployment secret manager. Tokens are compared in constant time and are
never logged. The portal-facing reviewer dashboard (a later increment) uses
Supabase Auth with per-user roles instead.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuthResult:
    authenticated: bool
    reason: str = ""


def generate_token() -> str:
    """Generate a strong bearer token for local/dev use."""

    return secrets.token_urlsafe(32)


def authenticate(authorization_header: str | None, expected_token: str | None) -> AuthResult:
    """Validate an ``Authorization: Bearer <token>`` header in constant time."""

    if not expected_token:
        return AuthResult(False, "server token not configured")
    if not authorization_header:
        return AuthResult(False, "missing Authorization header")
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        return AuthResult(False, "malformed Authorization header; expected 'Bearer <token>'")
    presented = parts[1].strip()
    if not hmac.compare_digest(presented, expected_token):
        return AuthResult(False, "invalid token")
    return AuthResult(True)
