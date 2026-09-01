"""Shadow report structure and rendering (JSON + human-readable Markdown).

The report is explicitly non-financial. It carries usage observations and
accuracy evidence for the steering group — no tariffs, splits, amounts, or
payout data appear anywhere.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .metrics import SourceScorecard

REPORT_KIND = "radio_shadow_reconciliation"
REPORT_NON_FINANCIAL = True


@dataclass(frozen=True, slots=True)
class CandidateSummary:
    detection_id: str
    recording_id: str
    title: str
    source_code: str
    started_at: str
    votes: int
    confidence_hint: float
    log_reconciliation: str  # "confirmed_by_log" | "candidate_not_in_log"
    review_status: str       # candidate | verified | rejected | disputed


@dataclass(frozen=True, slots=True)
class ShadowSourceReport:
    source_code: str
    display_name: str
    region: str
    chunk_count: int
    candidate_count: int
    scorecard: SourceScorecard


@dataclass(frozen=True, slots=True)
class ShadowReport:
    report_id: str
    generated_at: str
    period_start: str
    period_end: str
    kind: str = REPORT_KIND
    non_financial: bool = REPORT_NON_FINANCIAL
    source_count: int = 0
    sources: tuple[ShadowSourceReport, ...] = ()
    candidates: tuple[CandidateSummary, ...] = ()
    log_reconciliation: tuple[dict[str, Any], ...] = ()
    unmatched_local_repertoire: tuple[dict[str, Any], ...] = ()
    review_outcomes: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)


def _iso(value: datetime | str) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def render_markdown(report: ShadowReport) -> str:
    """Render a weekly non-financial shadow report for the steering group."""

    lines: list[str] = []
    lines.append("# Radio shadow-monitoring reconciliation report")
    lines.append("")
    lines.append(
        f"- **Report:** `{report.report_id}`  ·  generated {report.generated_at}"
    )
    lines.append(f"- **Period:** {report.period_start} → {report.period_end}")
    lines.append(f"- **Sources monitored:** {report.source_count}")
    lines.append(
        "- **Status:** **non-financial shadow report.** Candidate detections are "
        "evidence for review only; no tariffs, rights shares, or money movement are applied."
    )
    lines.append("")

    lines.append("## Source scorecards")
    lines.append("")
    lines.append(
        "| Source | Region | Chunks | Candidates | Reviewed | Precision | Log agreement | Possible false-negatives |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for source in report.sources:
        sc = source.scorecard
        precision = "n/a" if sc.precision is None else f"{sc.precision:.1%}"
        agreement = "n/a" if sc.log_agreement_rate is None else f"{sc.log_agreement_rate:.1%}"
        lines.append(
            f"| {source.display_name} (`{source.source_code}`) | {source.region} | "
            f"{source.chunk_count} | {sc.total_candidates} | {sc.reviewed} "
            f"(✓{sc.verified} ✗{sc.rejected} ?{sc.unreviewed}) | {precision} | "
            f"{agreement} | {sc.possible_false_negatives} |"
        )
    lines.append("")

    reviewed = report.review_outcomes
    lines.append("## Review outcomes (across all sources)")
    lines.append("")
    lines.append(
        f"- verified: **{reviewed.get('verified', 0)}**  ·  "
        f"rejected (false positives): **{reviewed.get('rejected', 0)}**  ·  "
        f"still candidate/unreviewed: **{reviewed.get('candidate', 0)}**  ·  "
        f"disputed: **{reviewed.get('disputed', 0)}**"
    )
    lines.append("")

    lines.append("## Unmatched local repertoire for catalog outreach")
    lines.append("")
    if not report.unmatched_local_repertoire:
        lines.append("_None recorded this period._")
    else:
        lines.append("| Aired at | Title | Artist | Source |")
        lines.append("| --- | --- | --- | --- |")
        for item in report.unmatched_local_repertoire:
            lines.append(
                f"| {item.get('aired_at', '')} | {item.get('title', '')} | "
                f"{item.get('artist', '') or '—'} | {item.get('source_code', '')} |"
            )
    lines.append("")

    lines.append("## Station-log reconciliation exceptions")
    lines.append("")
    if not report.log_reconciliation:
        lines.append("_No log exceptions recorded._")
    else:
        lines.append("| Source | Candidate not in station log | Logged items with no candidate |")
        lines.append("| --- | --- | --- |")
        for entry in report.log_reconciliation:
            lines.append(
                f"| `{entry.get('source_code', '')}` | "
                f"{entry.get('candidate_not_in_log', 0)} | "
                f"{entry.get('possible_false_negatives', 0)} |"
            )
    lines.append("")

    lines.append("## Candidate plays (evidence, not payable)")
    lines.append("")
    if not report.candidates:
        lines.append("_No candidate detections in scope._")
    else:
        lines.append("| When | Source | Title | Votes | Confidence hint | Log | Review |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for candidate in report.candidates:
            lines.append(
                f"| {candidate.started_at} | `{candidate.source_code}` | {candidate.title} | "
                f"{candidate.votes} | {candidate.confidence_hint:.2f} | "
                f"{candidate.log_reconciliation} | {candidate.review_status} |"
            )
    lines.append("")

    if report.warnings:
        lines.append("## Warnings")
        lines.append("")
        for warning in report.warnings:
            lines.append(f"- {warning}")
        lines.append("")

    lines.append("---")
    lines.append(
        "_Thresholds are pilot calibration values, not approval gates. A candidate "
        "match never becomes payable without human/governance review, an active split, "
        "a governed tariff, and finance approval._"
    )
    return "\n".join(lines)
