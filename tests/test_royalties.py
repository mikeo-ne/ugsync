from __future__ import annotations

import unittest
from decimal import Decimal

from kla_sync.royalties.calculator import (
    RoyaltyCalculator,
    SplitRecipient,
    SplitValidationError,
    UsageForRoyalty,
)


class RoyaltyCalculatorTests(unittest.TestCase):
    def test_weighted_formula_and_split_sum_are_exact(self) -> None:
        usage = UsageForRoyalty(
            usage_event_id="usage-1",
            right_type="master",
            base_rate_ugx=Decimal(1000),
            venue_or_station_weight=Decimal(2),
            detected_duration_seconds=Decimal(90),
            reference_duration_seconds=Decimal(180),
        )
        recipients = (
            SplitRecipient("line-1", "producer", "producer", 5000),
            SplitRecipient("line-2", "artist", "performer", 2500),
            SplitRecipient("line-3", "label", "label", 2500),
        )
        calculation = RoyaltyCalculator().calculate(usage, recipients)

        self.assertEqual(calculation.gross_raw_ugx, Decimal("1000.0"))
        self.assertEqual(calculation.gross_settled_ugx, Decimal(1000))
        self.assertEqual(
            sum(allocation.settled_amount_ugx for allocation in calculation.allocations), Decimal(1000)
        )
        self.assertEqual([item.settled_amount_ugx for item in calculation.allocations], [500, 250, 250])

    def test_largest_remainder_preserves_rounded_gross(self) -> None:
        usage = UsageForRoyalty(
            usage_event_id="usage-2",
            right_type="performance",
            base_rate_ugx=Decimal(5),
            venue_or_station_weight=Decimal(1),
            detected_duration_seconds=Decimal(1),
            reference_duration_seconds=Decimal(1),
        )
        recipients = (
            SplitRecipient("line-1", "a", "composer", 3333),
            SplitRecipient("line-2", "b", "composer", 3333),
            SplitRecipient("line-3", "c", "publisher", 3334),
        )
        calculation = RoyaltyCalculator().calculate(usage, recipients)
        self.assertEqual([item.settled_amount_ugx for item in calculation.allocations], [2, 1, 2])
        self.assertEqual(sum(item.settled_amount_ugx for item in calculation.allocations), Decimal(5))

    def test_non_finite_money_input_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            UsageForRoyalty(
                usage_event_id="usage-nan",
                right_type="master",
                base_rate_ugx=Decimal("NaN"),
                venue_or_station_weight=Decimal(1),
                detected_duration_seconds=Decimal(1),
                reference_duration_seconds=Decimal(1),
            )

    def test_incomplete_split_is_not_payable(self) -> None:
        usage = UsageForRoyalty(
            usage_event_id="usage-3",
            right_type="master",
            base_rate_ugx=Decimal(100),
            venue_or_station_weight=Decimal(1),
            detected_duration_seconds=Decimal(1),
            reference_duration_seconds=Decimal(1),
        )
        with self.assertRaises(SplitValidationError):
            RoyaltyCalculator().calculate(
                usage,
                (SplitRecipient("line", "party", "artist", 9999),),
            )


if __name__ == "__main__":
    unittest.main()
