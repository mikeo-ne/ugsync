from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kla_sync.shadow.metrics import build_scorecard
from kla_sync.shadow.reconciliation import (
    CandidatePlay,
    StationLogEntry,
    normalize_title,
    reconcile_with_station_log,
    split_unmatched_repertoire,
    title_artist_key,
)
from kla_sync.shadow.report import render_markdown
from kla_sync.shadow.service import ShadowMonitoringService
from kla_sync.shadow.sources import load_pilot_sources, pilot_sources_template


def chunk_payload(edge_chunk_id: str, source_code: str, *, started: datetime) -> dict[str, object]:
    return {
        "edge_chunk_id": edge_chunk_id,
        "source_code": source_code,
        "device_id": "edge-demo",
        "started_at": started.isoformat(),
        "ended_at": (started + timedelta(seconds=30)).isoformat(),
        "content_sha256": "a" * 64,
        "byte_count": 1000,
        "fingerprint_schema_id": "kla-landmark-ratio-v1:test",
        "capture_policy": "hashes_only",
        "landmarks": [
            {"anchor_frame": 1, "frequency_ratio_bin": 2, "delta_frames": 3},
            {"anchor_frame": 4, "frequency_ratio_bin": -1, "delta_frames": 7},
        ],
    }


def candidate(recording_id: str, *, votes: int = 40, confidence: float = 0.6, title: str = "Obulungi") -> dict[str, object]:
    return {
        "recording_id": recording_id,
        "votes": votes,
        "confidence_hint": confidence,
        "tempo_scale": 1.0,
        "title": title,
        "artist": "Ssali",
    }


class NormalizationTests(unittest.TestCase):
    def test_normalize_title_case_punctuation(self) -> None:
        self.assertEqual(normalize_title("  Obulungi!! Buno "), "obulungi buno")

    def test_title_artist_key(self) -> None:
        self.assertEqual(
            title_artist_key("OBULUNGI", "Ssali"),
            title_artist_key("obulungi", "ssali"),
        )


class ReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.at = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
        self.play = CandidatePlay(
            detection_id="det-1",
            recording_id="rec-a",
            title="Obulungi Buno",
            artist="Ssali",
            started_at=self.at,
            ended_at=self.at + timedelta(seconds=30),
            votes=40,
            confidence_hint=0.7,
            tempo_scale=1.0,
        )

    def test_log_entry_within_window_confirms(self) -> None:
        log = [
            StationLogEntry(
                aired_at=self.at + timedelta(seconds=20),
                title="Obulungi Buno",
                artist="Ssali",
            )
        ]
        result = reconcile_with_station_log([self.play], log)
        self.assertEqual(result.confirmed_by_log, ("det-1",))
        self.assertEqual(result.candidate_not_in_log, ())
        self.assertAlmostEqual(result.agreement_rate, 1.0)

    def test_recording_id_match_within_window(self) -> None:
        log = [StationLogEntry(aired_at=self.at + timedelta(seconds=10), title="x", recording_id="rec-a")]
        result = reconcile_with_station_log([self.play], log)
        self.assertEqual(result.confirmed_by_log, ("det-1",))

    def test_candidate_outside_window_is_unlogged(self) -> None:
        log = [
            StationLogEntry(
                aired_at=self.at + timedelta(minutes=30),
                title="Obulungi Buno",
                artist="Ssali",
            )
        ]
        result = reconcile_with_station_log([self.play], log, tolerance_seconds=90)
        self.assertEqual(result.candidate_not_in_log, ("det-1",))

    def test_logged_known_item_without_candidate_is_false_negative(self) -> None:
        log = [
            StationLogEntry(aired_at=self.at + timedelta(seconds=5), title="Different Track", recording_id="rec-z"),
        ]
        catalog = {"rec-z": {"title": "Different Track", "artist": "Known Band"}}
        result = reconcile_with_station_log([self.play], log, catalog_identifiers=catalog)
        # play matches? title differs and recording id differs -> play unlogged;
        # the other log item is a known-catalog gap (possible false negative).
        self.assertEqual(len(result.logged_items_unmatched), 1)
        local, missed = split_unmatched_repertoire(result.logged_items_unmatched)
        self.assertEqual(len(missed), 1)
        self.assertEqual(local, [])

    def test_unknown_local_repertoire_is_outreach_not_false_negative(self) -> None:
        log = [
            StationLogEntry(
                aired_at=self.at + timedelta(seconds=5),
                title="Street Freestyle",
                known_catalog=False,
            ),
        ]
        result = reconcile_with_station_log([self.play], log)
        local, missed = split_unmatched_repertoire(result.logged_items_unmatched)
        self.assertEqual(len(local), 1)
        self.assertEqual(missed, [])
        self.assertEqual(local[0]["title"], "Street Freestyle")


class MetricsTests(unittest.TestCase):
    def test_precision_only_uses_reviewed(self) -> None:
        sc = build_scorecard(
            "s1", total_candidates=10, verified=7, rejected=3, unreviewed=0,
            log_agreement_rate=0.9, possible_false_negatives=2, local_repertoire_gaps=1,
        )
        self.assertAlmostEqual(sc.precision, 0.7)
        self.assertEqual(sc.reviewed, 10)

    def test_precision_none_when_no_reviews(self) -> None:
        sc = build_scorecard(
            "s1", total_candidates=5, verified=0, rejected=0, unreviewed=5,
            log_agreement_rate=None, possible_false_negatives=0, local_repertoire_gaps=0,
        )
        self.assertIsNone(sc.precision)

    def test_slices_by_confidence_and_tempo(self) -> None:
        reviewed = [
            {"status": "verified", "confidence_hint": 0.8, "tempo_scale": 1.0},
            {"status": "verified", "confidence_hint": 0.7, "tempo_scale": 1.05},
            {"status": "rejected", "confidence_hint": 0.2, "tempo_scale": 0.92},
        ]
        sc = build_scorecard(
            "s1", total_candidates=3, verified=2, rejected=1, unreviewed=0,
            log_agreement_rate=0.5, possible_false_negatives=0, local_repertoire_gaps=0,
            reviewed_candidates=reviewed,
        )
        bands = {s.value: s for s in sc.slices if s.dimension == "confidence_band"}
        self.assertEqual(bands["high"].verified, 2)
        self.assertEqual(bands["low"].rejected, 1)
        tempos = {s.value: s for s in sc.slices if s.dimension == "tempo"}
        self.assertIn("sped_up", tempos)
        self.assertIn("slowed_down", tempos)


class ShadowServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = load_pilot_sources()
        self.service = ShadowMonitoringService(self.sources)
        self.week_start = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
        self.week_end = self.week_start + timedelta(days=7)
        self.at = self.week_start + timedelta(hours=2)

    def _inputs(self, *, chunk_id: str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"):
        chunks = [chunk_payload(chunk_id, "kampala-radio-01", started=self.at)]
        supplied = {chunk_id: [candidate("rec-1", title="Obulungi Buno")]}
        logs = {
            "kampala-radio-01": [
                {"aired_at": (self.at + timedelta(seconds=15)).isoformat(),
                 "title": "Obulungi Buno", "artist": "Ssali", "recording_id": "rec-1",
                 "known_catalog": True},
                {"aired_at": (self.at + timedelta(minutes=20)).isoformat(),
                 "title": "Unknown Street Cypher", "known_catalog": False},
            ]
        }
        catalog = {"rec-1": {"title": "Obulungi Buno", "artist": "Ssali", "isrc": "UGAAA0000001"}}
        return chunks, supplied, logs, catalog

    def test_report_is_non_financial_and_scoped(self) -> None:
        chunks, supplied, logs, catalog = self._inputs()
        report = self.service.generate_report(
            period_start=self.week_start, period_end=self.week_end,
            chunk_payloads=chunks, station_logs=logs, catalog=catalog,
            supplied_candidates=supplied,
        )
        self.assertTrue(report.non_financial)
        self.assertEqual(report.kind, "radio_shadow_reconciliation")
        self.assertEqual(len(report.candidates), 1)
        candidate = report.candidates[0]
        self.assertEqual(candidate.log_reconciliation, "confirmed_by_log")
        self.assertEqual(candidate.review_status, "candidate")
        # local repertoire gap captured
        self.assertEqual(len(report.unmatched_local_repertoire), 1)
        self.assertEqual(report.unmatched_local_repertoire[0]["title"], "Unknown Street Cypher")
        self.assertEqual(report.review_outcomes["candidate"], 1)

    def test_review_decisions_drive_precision(self) -> None:
        chunks, supplied, logs, catalog = self._inputs()
        chunk_id = chunks[0]["edge_chunk_id"]
        report = self.service.generate_report(
            period_start=self.week_start, period_end=self.week_end,
            chunk_payloads=chunks, station_logs=logs, catalog=catalog,
            supplied_candidates=supplied,
            review_decisions={chunk_id: "verified"},
        )
        kam = next(s for s in report.sources if s.source_code == "kampala-radio-01")
        self.assertAlmostEqual(kam.scorecard.precision, 1.0)
        self.assertEqual(report.review_outcomes["verified"], 1)

    def test_non_pilot_source_is_excluded_with_warning(self) -> None:
        chunks = [chunk_payload("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "rogue-station-99", started=self.at)]
        report = self.service.generate_report(
            period_start=self.week_start, period_end=self.week_end,
            chunk_payloads=chunks,
            supplied_candidates={"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb": [candidate("rec-1")]},
        )
        self.assertEqual(report.candidates, ())
        self.assertTrue(any("non-agreed source" in w for w in report.warnings))

    def test_bad_chunk_is_warned_not_crashed(self) -> None:
        bad = {"edge_chunk_id": "not-a-uuid", "source_code": "kampala-radio-01"}
        report = self.service.generate_report(
            period_start=self.week_start, period_end=self.week_end,
            chunk_payloads=[bad],
        )
        self.assertTrue(report.warnings)

    def test_report_renders_markdown_without_finance_data(self) -> None:
        chunks, supplied, logs, catalog = self._inputs()
        report = self.service.generate_report(
            period_start=self.week_start, period_end=self.week_end,
            chunk_payloads=chunks, station_logs=logs, catalog=catalog,
            supplied_candidates=supplied,
        )
        md = render_markdown(report)
        self.assertIn("non-financial shadow report", md)
        self.assertIn("Source scorecards", md)
        for forbidden in ("payout", "wallet", "tariff_ugx", "base_rate", "split_line"):
            self.assertNotIn(forbidden, md.lower())
        # JSON round-trip
        data = json.loads(report.to_json())
        self.assertEqual(data["kind"], "radio_shadow_reconciliation")

    def test_template_covers_four_regions(self) -> None:
        sources = pilot_sources_template()
        regions = {s.region for s in sources}
        self.assertEqual(regions, {"Kampala", "Jinja", "Mbarara", "Gulu"})
        self.assertGreaterEqual(len(sources), 4)

    def test_matcher_callable_path(self) -> None:
        """The pipeline can call a live matcher to derive candidates per chunk."""

        class StubCandidate:
            def __init__(self, track_id: str, votes: int) -> None:
                self.track_id = track_id
                self.vote_count = votes
                self.confidence_hint = 0.66
                self.reference_per_query_tempo_scale = 1.0

        def matcher(_manifest: object) -> tuple[object, ...]:
            return (StubCandidate("rec-live", 55),)

        chunks = [chunk_payload("cccccccc-cccc-4ccc-8ccc-cccccccccccc", "jinja-radio-01", started=self.at)]
        catalog = {"rec-live": {"title": "Live Match", "artist": "DJ A", "isrc": "UGAAA0000099"}}
        logs = {
            "jinja-radio-01": [
                {"aired_at": (self.at + timedelta(seconds=10)).isoformat(),
                 "title": "Live Match", "artist": "DJ A", "recording_id": "rec-live"},
            ]
        }
        report = self.service.generate_report(
            period_start=self.week_start, period_end=self.week_end,
            chunk_payloads=chunks, station_logs=logs, catalog=catalog, matcher=matcher,
        )
        self.assertEqual(len(report.candidates), 1)
        self.assertEqual(report.candidates[0].recording_id, "rec-live")
        self.assertEqual(report.candidates[0].log_reconciliation, "confirmed_by_log")

    def test_write_outputs_to_disk(self) -> None:
        chunks, supplied, logs, catalog = self._inputs()
        report = self.service.generate_report(
            period_start=self.week_start, period_end=self.week_end,
            chunk_payloads=chunks, station_logs=logs, catalog=catalog,
            supplied_candidates=supplied,
        )
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "report.json"
            md_path = Path(tmp) / "report.md"
            ShadowMonitoringService.write_outputs(report, str(json_path), str(md_path))
            self.assertTrue(json_path.exists() and md_path.exists())
            loaded = json.loads(json_path.read_text())
            self.assertEqual(loaded["report_id"], report.report_id)


if __name__ == "__main__":
    unittest.main()
