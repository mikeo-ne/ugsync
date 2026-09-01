"""Pitch- and tempo-tolerant landmark fingerprints.

This module is a reference implementation of the KLA-Sync matching contract,
not a claim that an in-memory Python dictionary can meet production throughput.
It deliberately uses interval hashes (log-frequency ratios) instead of absolute
frequency hashes: a uniform DJ pitch shift preserves the ratio between two
spectral landmarks.  During lookup it searches small timing-scale variants to
tolerate approximately +/-10 percent tempo changes.

For production, persist the exact ``LandmarkHash.key`` and anchor offsets in a
Redis/Elasticsearch-backed inverted index.  The matching semantics and version
must remain the same across edge and server workers.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass

from .dsp import is_power_of_two, resample_linear, stft_magnitudes
from .wav import AudioBuffer

HASH_ALGORITHM_VERSION = "kla-landmark-ratio-v1"
DEFAULT_TEMPO_SCALES: tuple[float, ...] = (
    0.90,
    0.925,
    0.95,
    0.975,
    1.0,
    1.025,
    1.05,
    1.075,
    1.10,
)


@dataclass(frozen=True, slots=True)
class FingerprintConfig:
    """Analysis and hash parameters shared by catalog and query workers."""

    target_sample_rate: int = 11_025
    window_size: int = 2_048
    hop_size: int = 512
    min_frequency_hz: float = 80.0
    max_frequency_hz: float = 4_500.0
    peak_neighborhood_frequency_bins: int = 4
    peak_neighborhood_time_frames: int = 2
    relative_peak_floor_db: float = -28.0
    global_peak_floor_db: float = -52.0
    max_peaks_per_frame: int = 10
    min_delta_frames: int = 1
    max_delta_frames: int = 42
    fanout: int = 7
    ratio_quantization: int = 48
    ratio_tolerance_bins: int = 1
    tempo_delta_tolerance_frames: int = 1

    def __post_init__(self) -> None:
        if self.target_sample_rate <= 0:
            raise ValueError("target_sample_rate must be positive")
        if not is_power_of_two(self.window_size) or self.window_size < 64:
            raise ValueError("window_size must be a power of two and at least 64")
        if not 1 <= self.hop_size <= self.window_size:
            raise ValueError("hop_size must be between 1 and window_size")
        if not 0 <= self.min_frequency_hz < self.max_frequency_hz <= self.target_sample_rate / 2:
            raise ValueError("frequency range must fit inside the Nyquist frequency")
        if self.peak_neighborhood_frequency_bins < 1 or self.peak_neighborhood_time_frames < 0:
            raise ValueError("peak neighbourhoods must be non-negative / positive as appropriate")
        if self.max_peaks_per_frame < 1 or self.fanout < 1:
            raise ValueError("max_peaks_per_frame and fanout must be positive")
        if not 0 < self.min_delta_frames <= self.max_delta_frames:
            raise ValueError("landmark target-zone frame limits are invalid")
        if self.ratio_quantization < 1:
            raise ValueError("ratio_quantization must be positive")

    @property
    def frames_per_second(self) -> float:
        return self.target_sample_rate / self.hop_size

    @property
    def schema_id(self) -> str:
        """Stable ID that prevents incompatible fingerprints being mixed."""

        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"{HASH_ALGORITHM_VERSION}:{digest}"


@dataclass(frozen=True, slots=True)
class SpectralPeak:
    """A locally prominent spectral bin in an STFT frame."""

    frame: int
    frequency_bin: int
    magnitude: float


@dataclass(frozen=True, slots=True)
class LandmarkHash:
    """A relative-frequency landmark pair anchored at a frame.

    ``frequency_ratio_bin`` quantizes log2(target_frequency / anchor_frequency),
    so uniform pitch changes preserve it. ``delta_frames`` encodes local rhythm.
    """

    anchor_frame: int
    frequency_ratio_bin: int
    delta_frames: int

    @property
    def key(self) -> tuple[int, int]:
        return (self.frequency_ratio_bin, self.delta_frames)

    def key_at_tempo_scale(self, reference_per_query_scale: float) -> tuple[int, int]:
        """Return the key expected in a reference track at a timing scale."""

        scaled_delta = max(1, round(self.delta_frames * reference_per_query_scale))
        return (self.frequency_ratio_bin, scaled_delta)


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """Extracted landmark data suitable for registration or query."""

    schema_id: str
    duration_seconds: float
    peaks: tuple[SpectralPeak, ...]
    hashes: tuple[LandmarkHash, ...]


@dataclass(frozen=True, slots=True)
class FingerprintOccurrence:
    """One registered landmark position in the inverted index."""

    track_id: str
    anchor_frame: int
    hash_index: int


@dataclass(frozen=True, slots=True)
class MatchResult:
    """An explainable candidate match; scores are not payout approval decisions."""

    track_id: str
    vote_count: int
    query_coverage: float
    track_coverage: float
    reference_per_query_tempo_scale: float
    reference_offset_seconds: float

    @property
    def confidence_hint(self) -> float:
        """A conservative, uncalibrated hint for triage UI only.

        Production must calibrate approval thresholds with labelled Ugandan radio,
        club, and taxi recordings.  A human/review policy should still govern
        royalty-bearing detections.
        """

        coverage = min(self.query_coverage, self.track_coverage)
        vote_factor = min(1.0, self.vote_count / 20.0)
        return round(coverage * vote_factor, 4)


class FingerprintExtractor:
    """Extract robust relative landmark hashes from mono audio."""

    def __init__(self, config: FingerprintConfig | None = None) -> None:
        self.config = config or FingerprintConfig()

    def extract(self, audio: AudioBuffer) -> Fingerprint:
        """Extract a fingerprint from an :class:`AudioBuffer`."""

        return self.extract_samples(audio.samples, audio.sample_rate)

    def extract_samples(self, samples: Sequence[float], sample_rate: int) -> Fingerprint:
        """Extract landmarks after normalization and deterministic resampling."""

        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        original_duration = len(samples) / sample_rate
        if not samples:
            return Fingerprint(self.config.schema_id, 0.0, (), ())

        analysis_samples = resample_linear(samples, sample_rate, self.config.target_sample_rate)
        normalized = self._normalize(analysis_samples)
        if not normalized:
            return Fingerprint(self.config.schema_id, original_duration, (), ())

        spectrogram = stft_magnitudes(
            normalized,
            self.config.window_size,
            self.config.hop_size,
            pad_end=True,
        )
        peaks = self._find_peaks(spectrogram)
        hashes = self._pair_peaks(peaks)
        return Fingerprint(
            schema_id=self.config.schema_id,
            duration_seconds=original_duration,
            peaks=tuple(peaks),
            hashes=tuple(hashes),
        )

    @staticmethod
    def _normalize(samples: Sequence[float]) -> tuple[float, ...]:
        """Remove DC and normalize level without changing the spectral geometry."""

        if not samples:
            return ()
        dc_offset = sum(samples) / len(samples)
        centred = tuple(float(value) - dc_offset for value in samples)
        maximum = max(abs(value) for value in centred)
        if maximum < 1e-8:
            return ()
        return tuple(value / maximum for value in centred)

    def _find_peaks(self, spectrogram: Sequence[Sequence[float]]) -> list[SpectralPeak]:
        if not spectrogram:
            return []
        config = self.config
        bin_hz = config.target_sample_rate / config.window_size
        min_bin = max(1, math.ceil(config.min_frequency_hz / bin_hz))
        max_bin = min(len(spectrogram[0]) - 1, math.floor(config.max_frequency_hz / bin_hz))
        if min_bin >= max_bin:
            return []

        global_maximum = max(max(frame[min_bin : max_bin + 1], default=0.0) for frame in spectrogram)
        if global_maximum <= 0.0:
            return []
        relative_floor = 10 ** (config.relative_peak_floor_db / 20.0)
        global_floor = global_maximum * 10 ** (config.global_peak_floor_db / 20.0)
        frequency_radius = config.peak_neighborhood_frequency_bins
        time_radius = config.peak_neighborhood_time_frames
        peaks: list[SpectralPeak] = []

        for frame_index, frame in enumerate(spectrogram):
            frame_maximum = max(frame[min_bin : max_bin + 1], default=0.0)
            threshold = max(global_floor, frame_maximum * relative_floor)
            frame_candidates: list[SpectralPeak] = []
            for frequency_bin in range(min_bin + frequency_radius, max_bin - frequency_radius + 1):
                magnitude = frame[frequency_bin]
                if magnitude < threshold:
                    continue
                neighbourhood = frame[
                    frequency_bin - frequency_radius : frequency_bin + frequency_radius + 1
                ]
                if magnitude < max(neighbourhood):
                    continue

                # A near-local temporal maximum reduces stationary noise while
                # retaining repeating musical tones needed for short queries.
                temporal_start = max(0, frame_index - time_radius)
                temporal_end = min(len(spectrogram), frame_index + time_radius + 1)
                temporal_maximum = max(
                    other_frame[frequency_bin] for other_frame in spectrogram[temporal_start:temporal_end]
                )
                if magnitude + 1e-12 < temporal_maximum * 0.985:
                    continue
                frame_candidates.append(SpectralPeak(frame_index, frequency_bin, magnitude))

            frame_candidates.sort(key=lambda peak: (-peak.magnitude, peak.frequency_bin))
            peaks.extend(frame_candidates[: config.max_peaks_per_frame])
        return peaks

    def _pair_peaks(self, peaks: Sequence[SpectralPeak]) -> list[LandmarkHash]:
        """Create target-zone pairs using frequency *ratios*, not absolute bins."""

        config = self.config
        peaks_by_frame: defaultdict[int, list[SpectralPeak]] = defaultdict(list)
        for peak in peaks:
            peaks_by_frame[peak.frame].append(peak)
        for frame_peaks in peaks_by_frame.values():
            frame_peaks.sort(key=lambda peak: (-peak.magnitude, peak.frequency_bin))

        fingerprints: list[LandmarkHash] = []
        for anchor in peaks:
            candidates: list[SpectralPeak] = []
            first_target = anchor.frame + config.min_delta_frames
            final_target = anchor.frame + config.max_delta_frames
            for target_frame in range(first_target, final_target + 1):
                candidates.extend(peaks_by_frame.get(target_frame, ()))
            # Closer, stronger targets provide useful diversity without exploding
            # the index for an hour-long station capture.
            candidates.sort(
                key=lambda target: (target.frame - anchor.frame, -target.magnitude, target.frequency_bin)
            )
            for target in candidates[: config.fanout]:
                ratio = math.log2((target.frequency_bin + 0.5) / (anchor.frequency_bin + 0.5))
                fingerprints.append(
                    LandmarkHash(
                        anchor_frame=anchor.frame,
                        frequency_ratio_bin=round(ratio * config.ratio_quantization),
                        delta_frames=target.frame - anchor.frame,
                    )
                )
        return fingerprints


class InMemoryFingerprintIndex:
    """Reference inverted-index matcher with tempo-aware alignment voting.

    The class is useful for local validation and unit tests.  It is intentionally
    bounded to process memory; workers should implement the same key/offset
    contract against Redis or Elasticsearch for catalog-scale operation.
    """

    def __init__(self, config: FingerprintConfig | None = None) -> None:
        self.config = config or FingerprintConfig()
        self._inverted: defaultdict[tuple[int, int], list[FingerprintOccurrence]] = defaultdict(list)
        self._track_hashes: dict[str, tuple[LandmarkHash, ...]] = {}

    @property
    def schema_id(self) -> str:
        return self.config.schema_id

    @property
    def track_count(self) -> int:
        return len(self._track_hashes)

    def add(self, track_id: str, fingerprint: Fingerprint) -> None:
        """Register or replace one catalog recording fingerprint."""

        if not track_id or not track_id.strip():
            raise ValueError("track_id is required")
        self._require_compatible(fingerprint)
        if track_id in self._track_hashes:
            self.remove(track_id)
        hashes = tuple(fingerprint.hashes)
        self._track_hashes[track_id] = hashes
        for hash_index, landmark in enumerate(hashes):
            self._inverted[landmark.key].append(
                FingerprintOccurrence(track_id, landmark.anchor_frame, hash_index)
            )

    def remove(self, track_id: str) -> None:
        """Remove a catalog recording and all of its index occurrences."""

        hashes = self._track_hashes.pop(track_id, None)
        if hashes is None:
            return
        for landmark in hashes:
            key = landmark.key
            occurrences = self._inverted.get(key)
            if occurrences is None:
                continue
            filtered = [occurrence for occurrence in occurrences if occurrence.track_id != track_id]
            if filtered:
                self._inverted[key] = filtered
            else:
                self._inverted.pop(key, None)

    def match(
        self,
        query: Fingerprint,
        *,
        min_votes: int = 8,
        tempo_scales: Iterable[float] = DEFAULT_TEMPO_SCALES,
        limit: int = 5,
    ) -> tuple[MatchResult, ...]:
        """Return ranked candidates using hash and scaled-time offset consensus.

        ``reference_per_query_tempo_scale`` converts a query time coordinate to a
        catalog/reference time coordinate.  The default variants cover +/-10%.
        ``min_votes`` is deliberately a candidate threshold, never a final
        royalty approval threshold.
        """

        self._require_compatible(query)
        if min_votes < 1:
            raise ValueError("min_votes must be positive")
        if limit < 1 or not query.hashes:
            return ()

        scales = tuple(sorted({round(float(scale), 5) for scale in tempo_scales}))
        if not scales or any(scale <= 0.0 for scale in scales):
            raise ValueError("at least one positive tempo scale is required")

        CandidateKey = tuple[str, float, int]
        query_hits: defaultdict[CandidateKey, set[int]] = defaultdict(set)
        reference_hits: defaultdict[CandidateKey, set[int]] = defaultdict(set)
        config = self.config
        for scale in scales:
            for query_hash_index, landmark in enumerate(query.hashes):
                expected_ratio = landmark.frequency_ratio_bin
                expected_delta = max(1, round(landmark.delta_frames * scale))
                scaled_anchor = round(landmark.anchor_frame * scale)
                # A tolerant key search can retrieve duplicate representations of
                # the same query landmark. Count a landmark once per candidate
                # alignment so coverage is a real fraction in [0, 1].
                seen_candidates: set[CandidateKey] = set()
                for ratio_delta in range(-config.ratio_tolerance_bins, config.ratio_tolerance_bins + 1):
                    for time_delta in range(
                        -config.tempo_delta_tolerance_frames,
                        config.tempo_delta_tolerance_frames + 1,
                    ):
                        lookup_delta = expected_delta + time_delta
                        if lookup_delta < 1:
                            continue
                        for occurrence in self._inverted.get(
                            (expected_ratio + ratio_delta, lookup_delta), ()
                        ):
                            offset = occurrence.anchor_frame - scaled_anchor
                            candidate_key = (occurrence.track_id, scale, offset)
                            if candidate_key in seen_candidates:
                                continue
                            seen_candidates.add(candidate_key)
                            query_hits[candidate_key].add(query_hash_index)
                            reference_hits[candidate_key].add(occurrence.hash_index)

        best_per_track: dict[str, CandidateKey] = {}
        for candidate_key, candidate_query_hits in query_hits.items():
            track_id, _, _ = candidate_key
            current_key = best_per_track.get(track_id)
            if current_key is None or len(candidate_query_hits) > len(query_hits[current_key]):
                best_per_track[track_id] = candidate_key

        results: list[MatchResult] = []
        query_hash_count = len(query.hashes)
        for track_id, candidate_key in best_per_track.items():
            _, scale, offset = candidate_key
            vote_count = len(query_hits[candidate_key])
            if vote_count < min_votes:
                continue
            track_hash_count = max(1, len(self._track_hashes[track_id]))
            results.append(
                MatchResult(
                    track_id=track_id,
                    vote_count=vote_count,
                    query_coverage=round(vote_count / query_hash_count, 6),
                    track_coverage=round(len(reference_hits[candidate_key]) / track_hash_count, 6),
                    reference_per_query_tempo_scale=scale,
                    reference_offset_seconds=round(
                        offset * self.config.hop_size / self.config.target_sample_rate,
                        6,
                    ),
                )
            )
        results.sort(
            key=lambda result: (
                -result.vote_count,
                -result.query_coverage,
                -result.track_coverage,
                result.track_id,
            )
        )
        return tuple(results[:limit])

    def indexed_hash_count(self, track_id: str) -> int:
        """Return the number of registered hashes for diagnostics."""

        return len(self._track_hashes.get(track_id, ()))

    def occurrences_for(self, key: tuple[int, int]) -> tuple[FingerprintOccurrence, ...]:
        """Public read access to one inverted-index bucket (index adapters)."""

        return tuple(self._inverted.get(key, ()))

    def track_hash_count(self, track_id: str) -> int:
        """Number of registered hashes for one track (0 if unknown)."""

        return len(self._track_hashes.get(track_id, ()))

    def track_ids(self) -> frozenset[str]:
        """All currently indexed track ids."""

        return frozenset(self._track_hashes)

    def _require_compatible(self, fingerprint: Fingerprint) -> None:
        if fingerprint.schema_id != self.schema_id:
            raise ValueError(
                "fingerprint schema mismatch; re-extract catalog/query with the same FingerprintConfig"
            )


def fingerprint_wav(path: str, config: FingerprintConfig | None = None) -> Fingerprint:
    """Convenience helper for FFmpeg-decoded PCM WAV chunks."""

    from .wav import read_wav_mono

    extractor = FingerprintExtractor(config)
    audio = read_wav_mono(path, target_rate=extractor.config.target_sample_rate)
    return extractor.extract(audio)


def serialize_landmarks(landmarks: Iterable[LandmarkHash]) -> list[Mapping[str, int]]:
    """Produce a stable transport representation for queue/index adapters."""

    return [
        {
            "anchor_frame": landmark.anchor_frame,
            "frequency_ratio_bin": landmark.frequency_ratio_bin,
            "delta_frames": landmark.delta_frames,
        }
        for landmark in landmarks
    ]
