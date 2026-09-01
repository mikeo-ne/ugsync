"""Hybrid segmentation of continuous DJ mixes into attributable track events.

A hard cut detector alone fails on beat-matched Afrobeats, Amapiano, Dancehall,
and Kidandali sets.  This reference pipeline therefore keeps two signals apart:

* acoustic novelty proposes structural boundaries; and
* repeated fingerprint match windows establish that a specific recording played.

The output deliberately permits overlapping track events during a crossfade or
beat-match.  Downstream royalty policy can then apply a transparent overlap rule
instead of incorrectly forcing a single winner for every second of a DJ mix.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise

from .dsp import (
    EPSILON,
    amplitude_to_db,
    median,
    median_absolute_deviation,
    moving_average,
    resample_linear,
    root_mean_square,
    stft_magnitudes,
)


@dataclass(frozen=True, slots=True)
class SegmenterConfig:
    """Conservative defaults for 20–60 second fingerprint query windows."""

    target_sample_rate: int = 11_025
    window_size: int = 1_024
    hop_size: int = 512
    minimum_match_confidence: float = 0.35
    maximum_evidence_gap_seconds: float = 12.0
    minimum_play_duration_seconds: float = 15.0
    boundary_minimum_gap_seconds: float = 8.0
    boundary_merge_seconds: float = 5.0
    novelty_smoothing_radius_frames: int = 2
    novelty_mad_multiplier: float = 3.0

    def __post_init__(self) -> None:
        if self.target_sample_rate <= 0:
            raise ValueError("target_sample_rate must be positive")
        if self.window_size < 64 or self.window_size & (self.window_size - 1):
            raise ValueError("window_size must be a power of two and at least 64")
        if not 1 <= self.hop_size <= self.window_size:
            raise ValueError("hop_size must be between 1 and window_size")
        if not 0.0 <= self.minimum_match_confidence <= 1.0:
            raise ValueError("minimum_match_confidence must be in [0, 1]")
        if min(
            self.maximum_evidence_gap_seconds,
            self.minimum_play_duration_seconds,
            self.boundary_minimum_gap_seconds,
            self.boundary_merge_seconds,
        ) < 0.0:
            raise ValueError("segmentation durations cannot be negative")


@dataclass(frozen=True, slots=True)
class FrameFeature:
    """Frame-level acoustic evidence, retained for model calibration/audit."""

    at_seconds: float
    rms_db: float
    spectral_centroid_hz: float
    spectral_flux: float


@dataclass(frozen=True, slots=True)
class AcousticBoundary:
    """A likely musical transition, never sufficient proof of a track identity."""

    at_seconds: float
    novelty_score: float
    reasons: tuple[str, ...] = ("acoustic_novelty",)


@dataclass(frozen=True, slots=True)
class MatchWindow:
    """A time-bounded fingerprint candidate emitted by the matching service."""

    track_id: str
    started_at_seconds: float
    ended_at_seconds: float
    confidence: float

    def __post_init__(self) -> None:
        if not self.track_id.strip():
            raise ValueError("track_id is required")
        if self.started_at_seconds < 0.0 or self.ended_at_seconds <= self.started_at_seconds:
            raise ValueError("match windows require a positive, forward time range")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("match confidence must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class TrackPlayEvent:
    """Consolidated evidence that one recording played in a continuous mix."""

    track_id: str
    started_at_seconds: float
    ended_at_seconds: float
    matched_seconds: float
    evidence_windows: int
    confidence: float
    nearby_boundary_seconds: tuple[float, ...]

    @property
    def duration_seconds(self) -> float:
        return self.ended_at_seconds - self.started_at_seconds


@dataclass(frozen=True, slots=True)
class MixSegmentation:
    """Attributable events plus separately auditable transition proposals."""

    track_events: tuple[TrackPlayEvent, ...]
    boundaries: tuple[AcousticBoundary, ...]


class DJMixSegmenter:
    """Fuse acoustic change points and temporal fingerprint evidence."""

    def __init__(self, config: SegmenterConfig | None = None) -> None:
        self.config = config or SegmenterConfig()

    def extract_features(self, samples: Sequence[float], sample_rate: int) -> tuple[FrameFeature, ...]:
        """Calculate level, centroid, and positive spectral flux per frame."""

        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if not samples:
            return ()
        config = self.config
        analysis_samples = resample_linear(samples, sample_rate, config.target_sample_rate)
        spectrogram = stft_magnitudes(
            analysis_samples,
            config.window_size,
            config.hop_size,
            pad_end=True,
        )
        bin_hz = config.target_sample_rate / config.window_size
        features: list[FrameFeature] = []
        previous_spectrum: Sequence[float] | None = None

        for frame_index, spectrum in enumerate(spectrogram):
            start = frame_index * config.hop_size
            frame = analysis_samples[start : start + config.window_size]
            rms_db = amplitude_to_db(root_mean_square(frame))
            magnitude_total = sum(spectrum) + EPSILON
            centroid = sum(bin_index * bin_hz * value for bin_index, value in enumerate(spectrum))
            centroid /= magnitude_total
            if previous_spectrum is None:
                flux = 0.0
            else:
                positive_change = sum(
                    max(0.0, current - previous)
                    for current, previous in zip(spectrum, previous_spectrum, strict=True)
                )
                flux = positive_change / (sum(previous_spectrum) + EPSILON)
            features.append(
                FrameFeature(
                    at_seconds=frame_index * config.hop_size / config.target_sample_rate,
                    rms_db=rms_db,
                    spectral_centroid_hz=centroid,
                    spectral_flux=flux,
                )
            )
            previous_spectrum = spectrum
        return tuple(features)

    def detect_acoustic_boundaries(
        self, samples: Sequence[float], sample_rate: int
    ) -> tuple[AcousticBoundary, ...]:
        """Return robust novelty peaks with adaptive median/MAD thresholding."""

        return self.detect_boundaries_from_features(self.extract_features(samples, sample_rate))

    def detect_boundaries_from_features(
        self, features: Sequence[FrameFeature]
    ) -> tuple[AcousticBoundary, ...]:
        if len(features) < 3:
            return ()
        flux = [feature.spectral_flux for feature in features]
        energy_change = [
            0.0,
            *[
                abs(features[index].rms_db - features[index - 1].rms_db)
                for index in range(1, len(features))
            ],
        ]
        centroid_change = [
            0.0,
            *[
                abs(
                    math.log((features[index].spectral_centroid_hz + 1.0)
                    / (features[index - 1].spectral_centroid_hz + 1.0))
                )
                for index in range(1, len(features))
            ],
        ]
        novelty = [
            max(0.0, self._robust_z(flux[index], flux))
            + 0.4 * max(0.0, self._robust_z(energy_change[index], energy_change))
            + 0.25 * max(0.0, self._robust_z(centroid_change[index], centroid_change))
            for index in range(len(features))
        ]
        scores = moving_average(novelty, self.config.novelty_smoothing_radius_frames)
        score_median = median(scores)
        score_mad = median_absolute_deviation(scores)
        # A tiny floor avoids declaring every frame in a perfectly steady signal.
        threshold = score_median + self.config.novelty_mad_multiplier * max(score_mad, 0.05)
        frame_seconds = max(
            features[1].at_seconds - features[0].at_seconds,
            self.config.hop_size / self.config.target_sample_rate,
        )
        radius = max(1, round(self.config.boundary_minimum_gap_seconds / frame_seconds / 2.0))
        boundaries: list[AcousticBoundary] = []
        last_boundary_time = -math.inf

        for index, score in enumerate(scores):
            if score < threshold:
                continue
            neighbourhood = scores[max(0, index - radius) : min(len(scores), index + radius + 1)]
            if score < max(neighbourhood):
                continue
            at_seconds = features[index].at_seconds
            if at_seconds - last_boundary_time < self.config.boundary_minimum_gap_seconds:
                continue
            boundaries.append(AcousticBoundary(at_seconds=at_seconds, novelty_score=round(score, 5)))
            last_boundary_time = at_seconds
        return tuple(boundaries)

    def segment(
        self,
        audio_duration_seconds: float,
        match_windows: Iterable[MatchWindow],
        *,
        acoustic_boundaries: Iterable[AcousticBoundary] = (),
    ) -> MixSegmentation:
        """Consolidate matching evidence and merge it with boundary proposals.

        ``audio_duration_seconds`` is used only for validation/clipping. The
        method does not manufacture identities for acoustically distinct but
        unrecognized material; those intervals stay absent from ``track_events``.
        """

        if audio_duration_seconds < 0.0:
            raise ValueError("audio_duration_seconds cannot be negative")
        windows = self._valid_windows(match_windows, audio_duration_seconds)
        evidence_events = self._consolidate_windows(windows)
        all_boundaries = list(acoustic_boundaries)
        all_boundaries.extend(self._transition_boundaries(evidence_events))
        merged_boundaries = self._merge_boundaries(all_boundaries)

        events: list[TrackPlayEvent] = []
        for event in evidence_events:
            nearby = tuple(
                round(boundary.at_seconds, 4)
                for boundary in merged_boundaries
                if event.started_at_seconds - self.config.boundary_merge_seconds
                <= boundary.at_seconds
                <= event.ended_at_seconds + self.config.boundary_merge_seconds
            )
            events.append(
                TrackPlayEvent(
                    track_id=event.track_id,
                    started_at_seconds=event.started_at_seconds,
                    ended_at_seconds=event.ended_at_seconds,
                    matched_seconds=event.matched_seconds,
                    evidence_windows=event.evidence_windows,
                    confidence=event.confidence,
                    nearby_boundary_seconds=nearby,
                )
            )
        return MixSegmentation(tuple(events), tuple(merged_boundaries))

    def segment_audio(
        self,
        samples: Sequence[float],
        sample_rate: int,
        match_windows: Iterable[MatchWindow],
    ) -> MixSegmentation:
        """Convenience entry point for a decoded continuous capture."""

        duration = len(samples) / sample_rate if sample_rate > 0 else 0.0
        boundaries = self.detect_acoustic_boundaries(samples, sample_rate)
        return self.segment(duration, match_windows, acoustic_boundaries=boundaries)

    @staticmethod
    def _robust_z(value: float, values: Sequence[float]) -> float:
        centre = median(values)
        # 1.4826 scales MAD to a normal-distribution standard deviation.
        scale = max(median_absolute_deviation(values) * 1.4826, 1e-6)
        return (value - centre) / scale

    def _valid_windows(
        self, match_windows: Iterable[MatchWindow], duration: float
    ) -> list[MatchWindow]:
        valid: list[MatchWindow] = []
        for window in match_windows:
            if window.confidence < self.config.minimum_match_confidence:
                continue
            start = min(max(window.started_at_seconds, 0.0), duration)
            end = min(max(window.ended_at_seconds, 0.0), duration)
            if end <= start:
                continue
            valid.append(MatchWindow(window.track_id, start, end, window.confidence))
        return sorted(valid, key=lambda window: (window.track_id, window.started_at_seconds, window.ended_at_seconds))

    def _consolidate_windows(self, windows: Sequence[MatchWindow]) -> list[TrackPlayEvent]:
        by_track: dict[str, list[MatchWindow]] = {}
        for window in windows:
            by_track.setdefault(window.track_id, []).append(window)

        events: list[TrackPlayEvent] = []
        for track_id, track_windows in by_track.items():
            cluster: list[MatchWindow] = []
            cluster_end = -math.inf
            for window in track_windows:
                if cluster and window.started_at_seconds - cluster_end > self.config.maximum_evidence_gap_seconds:
                    event = self._event_from_cluster(track_id, cluster)
                    if event.duration_seconds >= self.config.minimum_play_duration_seconds:
                        events.append(event)
                    cluster = []
                cluster.append(window)
                cluster_end = max(cluster_end, window.ended_at_seconds)
            if cluster:
                event = self._event_from_cluster(track_id, cluster)
                if event.duration_seconds >= self.config.minimum_play_duration_seconds:
                    events.append(event)
        return sorted(events, key=lambda event: (event.started_at_seconds, event.ended_at_seconds, event.track_id))

    @staticmethod
    def _event_from_cluster(track_id: str, cluster: Sequence[MatchWindow]) -> TrackPlayEvent:
        start = min(window.started_at_seconds for window in cluster)
        end = max(window.ended_at_seconds for window in cluster)
        intervals = sorted((window.started_at_seconds, window.ended_at_seconds) for window in cluster)
        covered = 0.0
        coverage_start, coverage_end = intervals[0]
        for interval_start, interval_end in intervals[1:]:
            if interval_start <= coverage_end:
                coverage_end = max(coverage_end, interval_end)
            else:
                covered += coverage_end - coverage_start
                coverage_start, coverage_end = interval_start, interval_end
        covered += coverage_end - coverage_start
        evidence_seconds = sum(window.ended_at_seconds - window.started_at_seconds for window in cluster)
        weighted_confidence = sum(
            (window.ended_at_seconds - window.started_at_seconds) * window.confidence for window in cluster
        ) / max(evidence_seconds, EPSILON)
        return TrackPlayEvent(
            track_id=track_id,
            started_at_seconds=round(start, 4),
            ended_at_seconds=round(end, 4),
            matched_seconds=round(covered, 4),
            evidence_windows=len(cluster),
            confidence=round(min(1.0, weighted_confidence), 4),
            nearby_boundary_seconds=(),
        )

    def _transition_boundaries(self, events: Sequence[TrackPlayEvent]) -> list[AcousticBoundary]:
        transitions: list[AcousticBoundary] = []
        for previous, following in pairwise(events):
            if previous.track_id == following.track_id:
                continue
            if following.started_at_seconds <= previous.ended_at_seconds:
                # Both can be valid in a beat-match; the new track's first stable
                # evidence is the least misleading transition marker.
                at_seconds = following.started_at_seconds
            else:
                at_seconds = (previous.ended_at_seconds + following.started_at_seconds) / 2.0
            transitions.append(
                AcousticBoundary(
                    at_seconds=round(at_seconds, 4),
                    novelty_score=round((previous.confidence + following.confidence) / 2.0, 5),
                    reasons=("fingerprint_identity_change",),
                )
            )
        return transitions

    def _merge_boundaries(self, boundaries: Iterable[AcousticBoundary]) -> list[AcousticBoundary]:
        ordered = sorted(boundaries, key=lambda boundary: boundary.at_seconds)
        if not ordered:
            return []
        clusters: list[list[AcousticBoundary]] = [[ordered[0]]]
        for boundary in ordered[1:]:
            if boundary.at_seconds - clusters[-1][-1].at_seconds <= self.config.boundary_merge_seconds:
                clusters[-1].append(boundary)
            else:
                clusters.append([boundary])

        merged: list[AcousticBoundary] = []
        for cluster in clusters:
            # Prefer a direct fingerprint transition time; otherwise use the
            # strongest acoustic novelty peak.
            identity_boundaries = [
                boundary
                for boundary in cluster
                if "fingerprint_identity_change" in boundary.reasons
            ]
            chosen = max(identity_boundaries or cluster, key=lambda boundary: boundary.novelty_score)
            reasons = tuple(sorted({reason for boundary in cluster for reason in boundary.reasons}))
            merged.append(
                AcousticBoundary(
                    at_seconds=round(chosen.at_seconds, 4),
                    novelty_score=round(max(boundary.novelty_score for boundary in cluster), 5),
                    reasons=reasons,
                )
            )
        return merged
