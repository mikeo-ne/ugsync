"""Approved-registry synchronization contracts.

URSB registry access and data-sharing fields must be confirmed in a signed
partnership and data-protection review.  The interfaces below let the catalog
worker retain provenance and reconciliation state without fabricating an API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol


class RegistryRecordState(StrEnum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RegistryLookup:
    provider: str
    identifier_type: str
    identifier_value: str

    def __post_init__(self) -> None:
        if not all((self.provider.strip(), self.identifier_type.strip(), self.identifier_value.strip())):
            raise ValueError("registry lookup fields are required")


@dataclass(frozen=True, slots=True)
class RegistryRecord:
    state: RegistryRecordState
    external_record_id: str | None
    retrieved_at: datetime
    source_payload_sha256: str | None
    message: str


class CopyrightRegistryGateway(Protocol):
    def lookup(self, request: RegistryLookup) -> RegistryRecord:
        """Retrieve metadata under an approved data-sharing agreement."""


class UnavailableRegistryGateway:
    """Safe default until an authorized registry adapter is installed."""

    def lookup(self, request: RegistryLookup) -> RegistryRecord:
        return RegistryRecord(
            state=RegistryRecordState.UNAVAILABLE,
            external_record_id=None,
            retrieved_at=datetime.now(UTC),
            source_payload_sha256=None,
            message=(
                f"No approved adapter is configured for {request.provider}; "
                "queue this lookup for registry reconciliation."
            ),
        )
