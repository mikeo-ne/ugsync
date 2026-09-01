from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from kla_sync.audio.wav import read_wav_mono, write_wav_mono
from kla_sync.integrations.mobile_money import (
    PayoutInstruction,
    SandboxMobileMoneyGateway,
    WalletProvider,
)


class WavAndIntegrationTests(unittest.TestCase):
    def test_pcm_wav_round_trip_and_resample(self) -> None:
        samples = tuple((index % 20 - 10) / 10.0 for index in range(80))
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "capture.wav"
            write_wav_mono(path, samples, 8_000)
            audio = read_wav_mono(path, target_rate=4_000)

        self.assertEqual(audio.sample_rate, 4_000)
        self.assertEqual(len(audio.samples), 40)
        self.assertAlmostEqual(audio.duration_seconds, 0.01)
        self.assertAlmostEqual(audio.samples[0], -1.0, places=3)

    def test_sandbox_wallet_gateway_is_idempotent(self) -> None:
        gateway = SandboxMobileMoneyGateway()
        payout = PayoutInstruction(
            payout_id="payout-001",
            idempotency_key="idem-001",
            provider=WalletProvider.MTN_MOMO,
            payment_account_id="payment-account-001",
            amount_ugx=Decimal(12500),
            narration="KLA-Sync pilot statement",
        )
        first = gateway.submit(payout)
        second = gateway.submit(payout)

        self.assertEqual(first, second)
        self.assertEqual(first.provider_reference, "sandbox-mtn_momo-payout-001")


if __name__ == "__main__":
    unittest.main()
