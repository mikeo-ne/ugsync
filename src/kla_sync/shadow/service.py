"""Shadow-monitoring pipeline orchestration.

The service is a deterministic, offline batch: given pilot sources, captured
chunk manifests, an optional matcher (the production matcher from the
fingerprint index service), per-source station logs, reviewer decisions, and
catalog metadata, it produces a non-financial :class:`ShadowReport`.

It is deliberately decoupled from live network and audio capture — the radio
pilot captures chunks with the edge/ingestion stack and feeds this pipeline
from stored manifests, so a report can be regenerated and audited.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from ..ingestion_api.manifests import ChunkManifest, ManifestValidationError, parse_chunk_manifest
from .metrics import build_scorecard
from .reconciliation import (
    CandidatePlay,
    StationLogEntry,
    reconcile_with_station_log,
    split_unmatched_repertoire,
)
from .report import CandidateSummary, ShadowReport, ShadowSourceReport, render_markdown
from .sources import PilotSource, agreed_source_codes

# A matcher maps a manifest to ranked candidates (the production matcher wraps
# LandmarkIndexService.query). None means candidates are supplied directly.
Matcher = Callable[[ChunkManifest], tuple[Any, ...]]


def _parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class StationLog:
    source_code: str
    entries: tuple[StationLogEntry, ...]


def parse_station_logs(raw: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, tuple[StationLogEntry, ...]]:
    logs: dict[str, tuple[StationLogEntry, ...]] = {}
    for source_code, items in raw.items():
        entries: list[StationLogEntry] = []
        for item in items:
            entries.append(
                StationLogEntry(
                    aired_at=_parse_dt(str(item["aired_at"])),
                    title=str(item.get("title", "")),
                    artist=item.get("artist"),
                    recording_id=item.get("recording_id"),
                    isrc=item.get("isrc"),
                    known_catalog=bool(item.get("known_catalog", True)),
                )
            )
        logs[source_code] = tuple(entries)
    return logs


class ShadowMonitoringService:
    def __init__(self, sources: Sequence[PilotSource]) -> None:
        self._sources = tuple(sources)
        self._by_code = {s.source_code: s for s in self._sources}

    @property
    def sources(self) -> tuple[PilotSource, ...]:
        return self._sources

    def generate_report(
        self,
        *,
        period_start: str | datetime,
        period_end: str | datetime,
        chunk_payloads: Iterable[Mapping[str, Any]] = (),
        station_logs: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        review_decisions: Mapping[str, str] | None = None,
        catalog: Mapping[str, Mapping[str, Any]] | None = None,
        matcher: Matcher | None = None,
        supplied_candidates: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    ) -> ShadowReport:
        """Build a weekly shadow report.

        ``supplied_candidates`` maps ``edge_chunk_id`` to a list of candidate
        dicts (recording_id, votes, confidence_hint, tempo_scale, title, artist)
        for callers that already ran matching; otherwise ``matcher`` derives
        candidates from the manifest landmarks.
        """

        start = _parse_dt(period_start)
        end = _parse_dt(period_end)
        if end <= start:
            raise ValueError("period_end must be after period_start")
        decisions = dict(review_decisions or {})
        catalog_meta = dict(catalog or {})
        logs = parse_station_logs(station_logs or {})
        supplied = dict(supplied_candidates or {})

        chunks_per_source: dict[str, int] = {}
        all_candidates: list[tuple[CandidatePlay, str, str]] = []
        warnings: list[str] = []
        agreed = agreed_source_codes(self._sources)

        for index, payload in enumerate(chunk_payloads):
            try:
                manifest = parse_chunk_manifest(
                    dict(payload),
                    device_id=str(payload.get("device_id") or payload.get("source_code") or "shadow-runner"),
                )
            except ManifestValidationError as error:
                warnings.append(f"chunk[{index}] skipped: {error.errors[0]}")
                continue

            source_code = manifest.source_code
            chunks_per_source[source_code] = chunks_per_source.get(source_code, 0) + 1
            if source_code not in agreed:
                warnings.append(
                    f"chunk {manifest.edge_chunk_id} references non-pilot/non-agreed source "
                    f"'{source_code}'; excluded from the report"
                )
                continue

            raw_candidates = supplied.get(manifest.edge_chunk_id)
            if raw_candidates is None and matcher is not None:
                raw_candidates = [
                    {
                        "recording_id": c.track_id,
                        "votes": c.vote_count,
                        "confidence_hint": c.confidence_hint,
                        "tempo_scale": c.reference_per_query_tempo_scale,
                        "title": catalog_meta.get(c.track_id, {}).get("title", c.track_id),
                        "artist": catalog_meta.get(c.track_id, {}).get("artist"),
                    }
                    for c in matcher(manifest)
                ]
            if not raw_candidates:
                continue

            # Take the top candidate for the reconciliation view; full lists
            # remain in the matcher pipeline for the review queue.
            top = raw_candidates[0]
            recording_id = str(top["recording_id"])
            meta = catalog_meta.get(recording_id, {})
            detection_id = str(uuid4())
            play = CandidatePlay(
                detection_id=detection_id,
                recording_id=recording_id,
                title=str(top.get("title") or meta.get("title") or recording_id),
                artist=top.get("artist") or meta.get("artist"),
                started_at=manifest.started_at,
                ended_at=manifest.ended_at,
                votes=int(top.get("votes", 0)),
                confidence_hint=float(top.get("confidence_hint", 0.0)),
                tempo_scale=top.get("tempo_scale"),
            )
            status = decisions.get(manifest.edge_chunk_id, "candidate")
            all_candidates.append((play, source_code, status))

        source_reports: list[ShadowSourceReport] = []
        reconciliation_rows: list[dict[str, Any]] = []
        local_repertoire: list[dict[str, Any]] = []
        review_outcomes = {"candidate": 0, "verified": 0, "rejected": 0, "disputed": 0}
        candidate_summaries: list[CandidateSummary] = []

        for source in self._sources:
            source_candidates = [
                (play, status) for (play, code, status) in all_candidates if code == source.source_code
            ]
            plays = tuple(play for play, _ in source_candidates)
            log_entries = logs.get(source.source_code, ())
            reconciliation = reconcile_with_station_log(
                plays, log_entries, catalog_identifiers=catalog_meta
            )
            local_gaps, missed_known = split_unmatched_repertoire(
                dict(item, source_code=source.source_code)
                for item in reconciliation.logged_items_unmatched
            )
            for gap in local_gaps:
                gap["source_code"] = source.source_code
                local_repertoire.append(gap)

            confirmed = set(reconciliation.confirmed_by_log)
            verified = sum(1 for _, s in source_candidates if s == "verified")
            rejected = sum(1 for _, s in source_candidates if s == "rejected")
            unreviewed = sum(1 for _, s in source_candidates if s in ("candidate", "disputed"))
            for play, status in source_candidates:
                review_outcomes[status if status in review_outcomes else "candidate"] += 1
                candidate_summaries.append(
                    CandidateSummary(
                        detection_id=play.detection_id,
                        recording_id=play.recording_id,
                        title=play.title,
                        source_code=source.source_code,
                        started_at=play.started_at.isoformat(),
                        votes=play.votes,
                        confidence_hint=play.confidence_hint,
                        log_reconciliation=(
                            "confirmed_by_log" if play.detection_id in confirmed else "candidate_not_in_log"
                        ),
                        review_status=status,
                    )
                )

            reviewed_candidates = [
                {
                    "status": status,
                    "confidence_hint": play.confidence_hint,
                    "tempo_scale": play.tempo_scale,
                    "region": source.region,
                }
                for play, status in source_candidates
                if status in ("verified", "rejected")
            ]
            scorecard = build_scorecard(
                source.source_code,
                total_candidates=len(source_candidates),
                verified=verified,
                rejected=rejected,
                unreviewed=unreviewed,
                log_agreement_rate=reconciliation.agreement_rate,
                possible_false_negatives=len(missed_known),
                local_repertoire_gaps=len(local_gaps),
                reviewed_candidates=reviewed_candidates,
            )
            source_reports.append(
                ShadowSourceReport(
                    source_code=source.source_code,
                    display_name=source.display_name,
                    region=source.region,
                    chunk_count=chunks_per_source.get(source.source_code, 0),
                    candidate_count=len(source_candidates),
                    scorecard=scorecard,
                )
            )
            reconciliation_rows.append(
                {
                    "source_code": source.source_code,
                    "confirmed_by_log": len(confirmed),
                    "candidate_not_in_log": len(reconciliation.candidate_not_in_log),
                    "possible_false_negatives": len(missed_known),
                    "local_repertoire_gaps": len(local_gaps),
                    "agreement_rate": reconciliation.agreement_rate,
                }
            )

        report_id = f"shadow-{start.strftime('%Y%m%d')}-{uuid4().hex[:8]}"
        report = ShadowReport(
            report_id=report_id,
            generated_at=datetime.now(UTC).isoformat(),
            period_start=start.isoformat(),
            period_end=end.isoformat(),
            source_count=len(self._sources),
            sources=tuple(source_reports),
            candidates=tuple(candidate_summaries),
            log_reconciliation=tuple(reconciliation_rows),
            unmatched_local_repertoire=tuple(local_repertoire),
            review_outcomes=review_outcomes,
            warnings=tuple(warnings),
        )
        return report

    @staticmethod
    def write_outputs(report: ShadowReport, json_path: str, markdown_path: str | None = None) -> None:
        from pathlib import Path

        Path(json_path).write_text(report.to_json() + "\n", encoding="utf-8")
        if markdown_path:
            Path(markdown_path).write_text(render_markdown(report) + "\n", encoding="utf-8")
