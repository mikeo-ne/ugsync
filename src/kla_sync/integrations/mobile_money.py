"""Provider-neutral, idempotent mobile-money payout contracts.

MTN MoMo and Airtel Money integration details, products, and approval flows vary
by commercial agreement and country.  This module intentionally provides a safe
contract and sandbox implementation rather than guessing private production API
paths or credentials.  A production adapter must be certified against the
partner-provided Uganda specification before being enabled.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol


class WalletProvider(StrEnum):
    MTN_MOMO = "mtn_momo"
    AIRTEL_MONEY = "airtel_money"


class PayoutState(StrEnum):
    ACCEPTED = "accepted"
    PENDING = "pending"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PayoutInstruction:
    """A post-review payment request with no raw phone number or secret."""

    payout_id: str
    idempotency_key: str
    provider: WalletProvider
    payment_account_id: str
    amount_ugx: Decimal
    narration: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.payout_id.strip(),
                self.idempotency_key.strip(),
                self.payment_account_id.strip(),
                self.narration.strip(),
            )
        ):
            raise ValueError("payout identifiers and narration are required")
        if self.amount_ugx <= 0:
            raise ValueError("payout amount must be positive")


@dataclass(frozen=True, slots=True)
class ProviderSubmission:
    """Provider response persisted before any retry is attempted."""

    state: PayoutState
    provider_reference: str | None
    message: str


class MobileMoneyGateway(Protocol):
    """Contract implemented by approved MTN/Airtel server-side adapters."""

    def submit(self, instruction: PayoutInstruction) -> ProviderSubmission:
        """Submit exactly once per idempotency key, returning a durable response."""


class SandboxMobileMoneyGateway:
    """Deterministic fake gateway for integration tests and demos only."""

    def __init__(self) -> None:
        self._responses: dict[str, ProviderSubmission] = {}

    def submit(self, instruction: PayoutInstruction) -> ProviderSubmission:
        existing = self._responses.get(instruction.idempotency_key)
        if existing is not None:
            return existing
        response = ProviderSubmission(
            state=PayoutState.ACCEPTED,
            provider_reference=f"sandbox-{instruction.provider}-{instruction.payout_id}",
            message="Accepted by KLA-Sync sandbox; no funds moved.",
        )
        self._responses[instruction.idempotency_key] = response
        return response


class PayoutService:
    """Thin application service enforcing an idempotent hand-off boundary."""

    def __init__(self, gateway: MobileMoneyGateway) -> None:
        self.gateway = gateway

    def dispatch_approved(self, instruction: PayoutInstruction) -> ProviderSubmission:
        """Dispatch an already-approved payout; persistence is owned by caller.

        Callers must atomically record an outbox row before this invocation and
        persist the returned provider reference/status before acknowledging the
        worker message.  Never retry an unknown response without first looking up
        the instruction's idempotency key in the payout table/provider API.
        """

        return self.gateway.submit(instruction)
