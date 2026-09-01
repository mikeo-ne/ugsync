from __future__ import annotations

import math
import unittest

from kla_sync.audio.dsp import resample_linear
from kla_sync.audio.fingerprint import (
    FingerprintConfig,
    FingerprintExtractor,
    InMemoryFingerprintIndex,
)


def musical_fixture(sample_rate: int, duration_seconds: float) -> tuple[float, ...]:
    """A changing, harmonic test phrase with repeatable spectral landmarks."""

    notes = (196.0, 246.94, 293.66, 220.0, 329.63, 261.63, 392.0, 293.66)
    samples: list[float] = []
    for index in range(round(sample_rate * duration_seconds)):
        time = index / sample_rate
        note = notes[int(time * 1.6) % len(notes)]
        local = (time * 1.6) % 1.0
        envelope = 0.55 + 0.45 * math.sin(math.pi * local)
        # Harmonics plus a changing rhythmic component make interval landmarks
        # less ambiguous than a single steady sine wave.
        value = envelope * (
            0.52 * math.sin(2.0 * math.pi * note * time)
            + 0.28 * math.sin(2.0 * math.pi * note * 1.5 * time)
            + 0.17 * math.sin(2.0 * math.pi * note * 2.03 * time)
            + 0.10 * math.sin(2.0 * math.pi * (70.0 + (index % 400) / 4.0) * time)
        )
        samples.append(value)
    return tuple(samples)


class FingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sample_rate = 8_000
        self.config = FingerprintConfig(
            target_sample_rate=self.sample_rate,
            window_size=512,
            hop_size=128,
            min_frequency_hz=70,
            max_frequency_hz=3_600,
            peak_neighborhood_frequency_bins=3,
            peak_neighborhood_time_frames=1,
            max_peaks_per_frame=8,
            max_delta_frames=30,
            fanout=6,
        )
        self.extractor = FingerprintExtractor(self.config)
        self.reference = musical_fixture(self.sample_rate, 18.0)

    def test_crop_matches_its_catalog_recording(self) -> None:
        catalog_fingerprint = self.extractor.extract_samples(self.reference, self.sample_rate)
        query = self.reference[3 * self.sample_rate : 11 * self.sample_rate]
        query_fingerprint = self.extractor.extract_samples(query, self.sample_rate)
        self.assertGreater(len(catalog_fingerprint.hashes), 100)
        self.assertGreater(len(query_fingerprint.hashes), 50)

        index = InMemoryFingerprintIndex(self.config)
        index.add("track-kampala", catalog_fingerprint)
        matches = index.match(query_fingerprint, min_votes=6)

        self.assertTrue(matches)
        self.assertEqual(matches[0].track_id, "track-kampala")
        self.assertGreaterEqual(matches[0].vote_count, 6)

    def test_five_percent_pitch_and_tempo_shift_still_generates_candidate(self) -> None:
        catalog_fingerprint = self.extractor.extract_samples(self.reference, self.sample_rate)
        original_query = self.reference[4 * self.sample_rate : 13 * self.sample_rate]
        # Reinterpret 8 kHz material as 8.4 kHz and render at 8 kHz: playback
        # becomes 5% faster and 5% higher, as with a DJ deck pitch adjustment.
        shifted_query = resample_linear(original_query, 8_400, self.sample_rate)
        shifted_fingerprint = self.extractor.extract_samples(shifted_query, self.sample_rate)

        index = InMemoryFingerprintIndex(self.config)
        index.add("track-jinja", catalog_fingerprint)
        matches = index.match(shifted_fingerprint, min_votes=5)

        self.assertTrue(matches)
        self.assertEqual(matches[0].track_id, "track-jinja")
        self.assertTrue(
            1.025 <= matches[0].reference_per_query_tempo_scale <= 1.075,
            matches[0],
        )

    def test_moderate_voiceover_like_tonal_noise_still_generates_candidate(self) -> None:
        catalog_fingerprint = self.extractor.extract_samples(self.reference, self.sample_rate)
        clean_query = self.reference[6 * self.sample_rate : 12 * self.sample_rate]
        noisy_query = tuple(
            max(
                -1.0,
                min(
                    1.0,
                    sample
                    + 0.05 * math.sin(2.0 * math.pi * 97.0 * index / self.sample_rate)
                    + 0.025 * math.sin(2.0 * math.pi * 523.0 * index / self.sample_rate),
                ),
            )
            for index, sample in enumerate(clean_query)
        )
        index = InMemoryFingerprintIndex(self.config)
        index.add("track-mbarara", catalog_fingerprint)
        matches = index.match(self.extractor.extract_samples(noisy_query, self.sample_rate), min_votes=5)

        self.assertTrue(matches)
        self.assertEqual(matches[0].track_id, "track-mbarara")
        self.assertLessEqual(matches[0].query_coverage, 1.0)
        self.assertLessEqual(matches[0].track_coverage, 1.0)

    def test_incompatible_fingerprint_schema_is_rejected(self) -> None:
        primary = self.extractor.extract_samples(self.reference, self.sample_rate)
        incompatible = FingerprintExtractor(
            FingerprintConfig(
                target_sample_rate=self.sample_rate,
                window_size=1024,
                hop_size=256,
                min_frequency_hz=70,
                max_frequency_hz=3_600,
            )
        ).extract_samples(self.reference, self.sample_rate)
        index = InMemoryFingerprintIndex(self.config)
        index.add("track-gulu", primary)
        with self.assertRaisesRegex(ValueError, "schema mismatch"):
            index.match(incompatible)


if __name__ == "__main__":
    unittest.main()
