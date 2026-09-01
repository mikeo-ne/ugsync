"""Deterministic, auditable royalty allocation calculations.

The calculator intentionally stops before calling a mobile-money provider.  A
royalty run should persist its formula inputs and allocation snapshots, complete
a review/dispute period, and only then create idempotent payout instructions.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

BASIS_POINTS_TOTAL = 10_000
UGX_QUANTUM = Decimal(1)  # Uganda shilling settlements are normally whole UGX.


def _finite_decimal(value: Decimal | str | float, field_name: str) -> Decimal:
    """Normalize public numeric input without allowing binary float arithmetic."""

    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a decimal number, not a boolean")
    try:
        normalized = value if isinstance(value, Decimal) else Decimal(str(value))
    except (ArithmeticError, ValueError) as error:
        raise ValueError(f"{field_name} must be a valid decimal number") from error
    if not normalized.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return normalized


class SplitValidationError(ValueError):
    """Raised when a split sheet cannot be safely used for settlement."""


@dataclass(frozen=True, slots=True)
class UsageForRoyalty:
    """Verified usage and its tariff inputs for exactly one rights category."""

    usage_event_id: str
    right_type: str
    base_rate_ugx: Decimal
    venue_or_station_weight: Decimal
    detected_duration_seconds: Decimal
    reference_duration_seconds: Decimal

    def __post_init__(self) -> None:
        if not self.usage_event_id.strip() or not self.right_type.strip():
            raise ValueError("usage_event_id and right_type are required")
        for field_name in (
            "base_rate_ugx",
            "venue_or_station_weight",
            "detected_duration_seconds",
            "reference_duration_seconds",
        ):
            object.__setattr__(self, field_name, _finite_decimal(getattr(self, field_name), field_name))
        if self.base_rate_ugx < 0 or self.venue_or_station_weight < 0:
            raise ValueError("rate and source weight cannot be negative")
        if self.detected_duration_seconds < 0 or self.reference_duration_seconds <= 0:
            raise ValueError("durations must be non-negative and reference duration must be positive")

    @property
    def duration_ratio(self) -> Decimal:
        return self.detected_duration_seconds / self.reference_duration_seconds

    @property
    def gross_ugx(self) -> Decimal:
        """Base Rate × Source Weight × Duration Ratio, before rights splits."""

        return self.base_rate_ugx * self.venue_or_station_weight * self.duration_ratio


@dataclass(frozen=True, slots=True)
class SplitRecipient:
    """A frozen split-line snapshot, expressed in integer basis points."""

    split_line_id: str
    party_id: str
    role: str
    share_basis_points: int

    def __post_init__(self) -> None:
        if not self.split_line_id.strip() or not self.party_id.strip() or not self.role.strip():
            raise SplitValidationError("split_line_id, party_id, and role are required")
        if not 0 <= self.share_basis_points <= BASIS_POINTS_TOTAL:
            raise SplitValidationError("split shares must be between 0 and 10,000 basis points")


@dataclass(frozen=True, slots=True)
class RoyaltyAllocation:
    """One rounded allocation plus unrounded evidence for the audit trail."""

    usage_event_id: str
    right_type: str
    split_line_id: str
    party_id: str
    role: str
    share_basis_points: int
    raw_amount_ugx: Decimal
    settled_amount_ugx: Decimal


@dataclass(frozen=True, slots=True)
class RoyaltyCalculation:
    """The complete immutable calculation result for a usage/right pair."""

    usage: UsageForRoyalty
    gross_raw_ugx: Decimal
    gross_settled_ugx: Decimal
    allocations: tuple[RoyaltyAllocation, ...]


class RoyaltyCalculator:
    """Apply the published KLA-Sync weighted payout formula using Decimal math."""

    def __init__(self, settlement_quantum_ugx: Decimal = UGX_QUANTUM) -> None:
        quantum = _finite_decimal(settlement_quantum_ugx, "settlement_quantum_ugx")
        if quantum <= 0:
            raise ValueError("settlement_quantum_ugx must be positive")
        self.settlement_quantum_ugx = quantum

    def calculate(
        self, usage: UsageForRoyalty, recipients: Iterable[SplitRecipient]
    ) -> RoyaltyCalculation:
        """Allocate one verified usage event to a complete split sheet.

        Inputs must already be approved under the detection, dispute, and tariff
        policies.  The method validates that shares total exactly 100%, then uses
        a largest-remainder method so rounded recipient amounts sum exactly to
        the rounded gross amount.
        """

        split_lines = tuple(recipients)
        self._validate_split(split_lines)
        gross_raw = usage.gross_ugx
        gross_settled = gross_raw.quantize(self.settlement_quantum_ugx, rounding=ROUND_HALF_UP)
        raw_amounts = [
            gross_raw * Decimal(recipient.share_basis_points) / Decimal(BASIS_POINTS_TOTAL)
            for recipient in split_lines
        ]
        settled_amounts = self._largest_remainder_round(raw_amounts, gross_settled)
        allocations = tuple(
            RoyaltyAllocation(
                usage_event_id=usage.usage_event_id,
                right_type=usage.right_type,
                split_line_id=recipient.split_line_id,
                party_id=recipient.party_id,
                role=recipient.role,
                share_basis_points=recipient.share_basis_points,
                raw_amount_ugx=raw_amount,
                settled_amount_ugx=settled_amount,
            )
            for recipient, raw_amount, settled_amount in zip(
                split_lines, raw_amounts, settled_amounts, strict=True
            )
        )
        return RoyaltyCalculation(
            usage=usage,
            gross_raw_ugx=gross_raw,
            gross_settled_ugx=gross_settled,
            allocations=allocations,
        )

    def _largest_remainder_round(
        self, raw_amounts: list[Decimal], rounded_total: Decimal
    ) -> list[Decimal]:
        floored = [amount.quantize(self.settlement_quantum_ugx, rounding=ROUND_FLOOR) for amount in raw_amounts]
        remaining = rounded_total - sum(floored, Decimal(0))
        units_to_distribute = int(remaining / self.settlement_quantum_ugx)
        if units_to_distribute < 0:
            raise ArithmeticError("non-negative royalty rounding unexpectedly underflowed")
        order = sorted(
            range(len(raw_amounts)),
            key=lambda index: (
                -(raw_amounts[index] - floored[index]),
                index,
            ),
        )
        for index in order[:units_to_distribute]:
            floored[index] += self.settlement_quantum_ugx
        if sum(floored, Decimal(0)) != rounded_total:
            raise ArithmeticError("allocation rounding did not preserve the gross total")
        return floored

    @staticmethod
    def _validate_split(recipients: tuple[SplitRecipient, ...]) -> None:
        if not recipients:
            raise SplitValidationError("an active split sheet requires at least one recipient")
        if len({recipient.split_line_id for recipient in recipients}) != len(recipients):
            raise SplitValidationError("split line IDs must be unique within a calculation")
        total = sum((recipient.share_basis_points for recipient in recipients), start=0)
        if total != BASIS_POINTS_TOTAL:
            raise SplitValidationError(
                f"split shares must total {BASIS_POINTS_TOTAL} basis points, received {total}"
            )
