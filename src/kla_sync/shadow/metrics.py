"""Shadow-pilot accuracy metrics.

Metrics are reported **by source and by slice** (confidence, tempo scale,
region), never as one blended headline number. Precision comes from reviewed
candidates; a candidate with no review is "unreviewed" and excluded from the
denominator. Recall is estimated against the station log cross-check
(known-catalog logged items that produced no candidate).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PrecisionSlice:
    dimension: str
    value: str
    reviewed: int
    verified: int
    rejected: int
    precision: float | None


@dataclass(frozen=True, slots=True)
class SourceScorecard:
    source_code: str
    total_candidates: int
    reviewed: int
    verified: int
    rejected: int
    unreviewed: int
    precision: float | None
    log_agreement_rate: float | None
    possible_false_negatives: int
    local_repertoire_gaps: int
    slices: tuple[PrecisionSlice, ...]


def _precision(verified: int, rejected: int) -> float | None:
    decided = verified + rejected
    if decided == 0:
        return None
    return round(verified / decided, 4)


def build_scorecard(
    source_code: str,
    *,
    total_candidates: int,
    verified: int,
    rejected: int,
    unreviewed: int,
    log_agreement_rate: float | None,
    possible_false_negatives: int,
    local_repertoire_gaps: int,
    reviewed_candidates: Iterable[Mapping[str, object]] = (),
) -> SourceScorecard:
    """Aggregate per-source metrics, including confidence/tempo slices."""

    slices = _build_slices(reviewed_candidates)
    reviewed = verified + rejected
    return SourceScorecard(
        source_code=source_code,
        total_candidates=total_candidates,
        reviewed=reviewed,
        verified=verified,
        rejected=rejected,
        unreviewed=unreviewed,
        precision=_precision(verified, rejected),
        log_agreement_rate=log_agreement_rate,
        possible_false_negatives=possible_false_negatives,
        local_repertoire_gaps=local_repertoire_gaps,
        slices=slices,
    )


def _build_slices(reviewed_candidates: Iterable[Mapping[str, object]]) -> tuple[PrecisionSlice, ...]:
    buckets: dict[tuple[str, str], dict[str, int]] = {}

    def tally(dimension: str, value: str, was_verified: bool) -> None:
        key = (dimension, value)
        bucket = buckets.setdefault(key, {"reviewed": 0, "verified": 0, "rejected": 0})
        bucket["reviewed"] += 1
        bucket["verified" if was_verified else "rejected"] += 1

    for candidate in reviewed_candidates:
        status = str(candidate.get("status", ""))
        if status not in ("verified", "rejected"):
            continue
        was_verified = status == "verified"
        confidence = float(candidate.get("confidence_hint", 0.0) or 0.0)
        band = (
            "high" if confidence >= 0.6 else "medium" if confidence >= 0.35 else "low"
        )
        tally("confidence_band", band, was_verified)
        scale = candidate.get("tempo_scale")
        if scale is not None:
            scale_value = float(scale)
            if abs(scale_value - 1.0) < 0.025:
                tempo = "unshifted"
            elif scale_value > 1.0:
                tempo = "sped_up"
            else:
                tempo = "slowed_down"
            tally("tempo", tempo, was_verified)
        region = candidate.get("region")
        if region:
            tally("region", str(region), was_verified)

    slices = [
        PrecisionSlice(
            dimension=dimension,
            value=value,
            reviewed=counts["reviewed"],
            verified=counts["verified"],
            rejected=counts["rejected"],
            precision=_precision(counts["verified"], counts["rejected"]),
        )
        for (dimension, value), counts in sorted(buckets.items())
    ]
    return tuple(slices)
