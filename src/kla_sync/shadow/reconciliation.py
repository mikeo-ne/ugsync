"""Reconcile matcher candidates against a station's playlist log.

A station log is the broadcaster's own record of what aired, used as a
non-financial cross-check. Matching is deliberately simple and conservative:
a candidate agrees with the log when it is (a) a catalog match the station
also logged around the same window, or (b) a logged item with no catalog
recording attached (unmatched local repertoire). Identity comparisons use
catalog ISRC/recording id when available, then a normalized title+artist key.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# A candidate/observed play is considered to agree with a log entry if its
# midpoint falls within +/- this many seconds of the log's stated time.
DEFAULT_TOLERANCE_SECONDS = 90.0


@dataclass(frozen=True, slots=True)
class StationLogEntry:
    """One item from a station's playlist/schedule log."""

    aired_at: datetime
    title: str
    artist: str | None = None
    recording_id: str | None = None
    isrc: str | None = None
    # Local repertoire not in our catalog is still a logged item; it is not
    # treated as a false positive, only as a catalog gap.
    known_catalog: bool = True


@dataclass(frozen=True, slots=True)
class CandidatePlay:
    """A candidate detection summarized for reconciliation."""

    detection_id: str
    recording_id: str
    title: str
    artist: str | None
    started_at: datetime
    ended_at: datetime
    votes: int
    confidence_hint: float
    tempo_scale: float | None

    @property
    def midpoint(self) -> datetime:
        return self.started_at + (self.ended_at - self.started_at) / 2


@dataclass(frozen=True, slots=True)
class LogReconciliation:
    confirmed_by_log: tuple[str, ...]          # detection ids the log corroborates
    candidate_not_in_log: tuple[str, ...]      # candidates without a log entry (possible false positive)
    logged_items_unmatched: tuple[dict[str, Any], ...] = field(default=())  # logged plays with no candidate

    @property
    def agreement_rate(self) -> float | None:
        decided = len(self.confirmed_by_log) + len(self.candidate_not_in_log)
        if decided == 0:
            return None
        return round(len(self.confirmed_by_log) / decided, 4)


_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def normalize_title(value: str | None) -> str:
    if not value:
        return ""
    return _NORMALIZE_RE.sub(" ", value.lower()).strip()


def title_artist_key(title: str | None, artist: str | None) -> str:
    return f"{normalize_title(title)}|{normalize_title(artist)}"


def reconcile_with_station_log(
    candidates: Sequence[CandidatePlay],
    log_entries: Sequence[StationLogEntry],
    *,
    tolerance_seconds: float = DEFAULT_TOLERANCE_SECONDS,
    catalog_identifiers: dict[str, dict[str, str]] | None = None,
) -> LogReconciliation:
    """Tag each candidate as log-confirmed or unlogged, and find log gaps.

    ``catalog_identifiers`` maps recording_id -> {"title", "artist", "isrc"}
    so a log entry without a recording id can still match on normalized title.
    """

    catalog = catalog_identifiers or {}
    confirmed: list[str] = []
    unlogged: list[str] = []
    log_matched: set[int] = set()

    # Identifier sets so an unmatched log row can be classified as a known
    # catalog recording (possible false negative) vs an unknown local work
    # (catalog outreach gap).
    catalog_recording_ids = set(catalog)
    catalog_isrcs = {
        str(meta.get("isrc", "")).upper()
        for meta in catalog.values()
        if isinstance(meta, dict) and meta.get("isrc")
    }
    catalog_keys = {
        title_artist_key(meta.get("title"), meta.get("artist"))
        for meta in catalog.values()
        if isinstance(meta, dict)
    }

    log_by_recording: dict[str, list[int]] = {}
    log_by_key: dict[str, list[int]] = {}
    log_isrc: dict[str, int] = {}
    for index, entry in enumerate(log_entries):
        if entry.recording_id:
            log_by_recording.setdefault(entry.recording_id, []).append(index)
        if entry.isrc:
            log_isrc[entry.isrc.upper()] = index
        key = title_artist_key(entry.title, entry.artist)
        if key.strip("|"):
            log_by_key.setdefault(key, []).append(index)

    for candidate in candidates:
        meta = catalog.get(candidate.recording_id, {})
        isrc = (meta.get("isrc") or "").upper() or None
        log_index = _find_log_match(
            candidate, log_entries, log_by_recording, log_by_key, log_isrc,
            catalog, isrc, tolerance_seconds,
        )
        if log_index is not None:
            confirmed.append(candidate.detection_id)
            log_matched.add(log_index)
        else:
            unlogged.append(candidate.detection_id)

    # Logged items with no candidate are gaps: known catalog recordings are
    # possible false negatives; unknown works are local-repertoire outreach gaps.
    logged_unmatched = [
        {
            "title": entry.title,
            "artist": entry.artist,
            "aired_at": entry.aired_at.isoformat(),
            "recording_id": entry.recording_id,
            "known_catalog": (
                entry.known_catalog
                and (
                    bool(entry.recording_id and entry.recording_id in catalog_recording_ids)
                    or bool(entry.isrc and entry.isrc.upper() in catalog_isrcs)
                    or title_artist_key(entry.title, entry.artist) in catalog_keys
                )
            ),
        }
        for index, entry in enumerate(log_entries)
        if index not in log_matched
    ]
    return LogReconciliation(
        confirmed_by_log=tuple(confirmed),
        candidate_not_in_log=tuple(unlogged),
        logged_items_unmatched=tuple(logged_unmatched),
    )


def _find_log_match(
    candidate: CandidatePlay,
    log_entries: Sequence[StationLogEntry],
    log_by_recording: dict[str, list[int]],
    log_by_key: dict[str, list[int]],
    log_isrc: dict[str, int],
    catalog: dict[str, dict[str, str]],
    isrc: str | None,
    tolerance_seconds: float,
) -> int | None:
    candidate_indices: set[int] = set()
    candidate_indices.update(log_by_recording.get(candidate.recording_id, ()))
    meta = catalog.get(candidate.recording_id, {})
    key = title_artist_key(meta.get("title") or candidate.title, meta.get("artist") or candidate.artist)
    if key.strip("|"):
        candidate_indices.update(log_by_key.get(key, ()))
    if isrc and isrc in log_isrc:
        candidate_indices.add(log_isrc[isrc])

    for index in candidate_indices:
        entry = log_entries[index]
        delta = abs((entry.aired_at - candidate.midpoint).total_seconds())
        if delta <= tolerance_seconds:
            return index
    return None


def split_unmatched_repertoire(logged_unmatched: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split log gaps into unknown local repertoire (outreach) vs missed known."""

    local_gaps: list[dict[str, Any]] = []
    missed_known: list[dict[str, Any]] = []
    for item in logged_unmatched:
        (local_gaps if not item.get("known_catalog", True) else missed_known).append(item)
    return local_gaps, missed_known
