from __future__ import annotations

import unittest

from kla_sync.audio.segmentation import (
    AcousticBoundary,
    DJMixSegmenter,
    MatchWindow,
    SegmenterConfig,
)


class SegmentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.segmenter = DJMixSegmenter(
            SegmenterConfig(
                minimum_match_confidence=0.4,
                minimum_play_duration_seconds=10.0,
                maximum_evidence_gap_seconds=8.0,
                boundary_merge_seconds=4.0,
            )
        )

    def test_overlapping_beatmatch_is_preserved_as_two_events(self) -> None:
        evidence = (
            MatchWindow("track-a", 0.0, 16.0, 0.90),
            MatchWindow("track-a", 15.0, 31.0, 0.86),
            MatchWindow("track-b", 27.0, 43.0, 0.82),
            MatchWindow("track-b", 42.0, 60.0, 0.88),
            MatchWindow("too-weak", 5.0, 40.0, 0.15),
        )
        result = self.segmenter.segment(
            60.0,
            evidence,
            acoustic_boundaries=(AcousticBoundary(28.0, 4.0),),
        )

        self.assertEqual([event.track_id for event in result.track_events], ["track-a", "track-b"])
        first, second = result.track_events
        self.assertEqual((first.started_at_seconds, first.ended_at_seconds), (0.0, 31.0))
        self.assertEqual((second.started_at_seconds, second.ended_at_seconds), (27.0, 60.0))
        self.assertGreater(first.ended_at_seconds, second.started_at_seconds)
        self.assertTrue(any("fingerprint_identity_change" in item.reasons for item in result.boundaries))

    def test_short_or_low_confidence_evidence_does_not_create_play(self) -> None:
        result = self.segmenter.segment(
            30.0,
            (
                MatchWindow("weak", 0.0, 25.0, 0.2),
                MatchWindow("short", 10.0, 15.0, 0.9),
            ),
        )
        self.assertEqual(result.track_events, ())

    def test_invalid_window_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MatchWindow("track", 2.0, 2.0, 0.5)


if __name__ == "__main__":
    unittest.main()
