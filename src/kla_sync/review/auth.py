"""Portal user authentication.

In production the dashboard backend (BFF) verifies a Supabase Auth JWT and maps
the caller to a :class:`~kla_sync.review.models.PortalUser` using the catalog
membership roles from migration 002. The reference/dev resolver instead maps an
opaque bearer token to a seeded user directory — the same role checks apply, so
authorization logic is identical across environments.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass

from .errors import ReviewForbidden
from .models import ROLES, PortalUser


@dataclass(frozen=True, slots=True)
class PortalAccount:
    user_id: str
    role: str
    token: str
    display_name: str = ""
    party_id: str | None = None

    def to_user(self) -> PortalUser:
        return PortalUser(
            user_id=self.user_id,
            role=self.role,
            display_name=self.display_name,
            party_id=self.party_id,
        )


def issue_portal_token() -> str:
    return secrets.token_urlsafe(24)


class PortalUserDirectory:
    """Token -> PortalUser resolver for the reference server."""

    def __init__(self, accounts: list[PortalAccount] | None = None) -> None:
        self._by_token: dict[str, PortalAccount] = {}
        for account in accounts or ():
            self.register(account)

    def register(self, account: PortalAccount) -> PortalAccount:
        if account.role not in ROLES:
            raise ValueError(f"unknown portal role '{account.role}'")
        self._by_token[account.token] = account
        return account

    def resolve(self, authorization_header: str | None) -> PortalUser:
        if not authorization_header:
            raise ReviewForbidden("missing Authorization header")
        parts = authorization_header.split(" ", 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            raise ReviewForbidden("malformed Authorization header; expected 'Bearer <token>'")
        presented = parts[1].strip()
        match = next(
            (
                account
                for account in self._by_token.values()
                if hmac.compare_digest(account.token, presented)
            ),
            None,
        )
        if match is None:
            raise ReviewForbidden("invalid portal token")
        return match.to_user()
