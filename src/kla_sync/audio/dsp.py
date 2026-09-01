"""Small, deterministic DSP helpers used by the reference edge algorithms.

The implementation has a pure-Python FFT fallback so unit tests and constrained
edge images do not require native wheels.  Install the ``production`` extra to
use NumPy's considerably faster FFT implementation for real monitoring loads.
"""

from __future__ import annotations

import cmath
import math
from collections.abc import Iterable, Iterator, Sequence
from functools import lru_cache

try:  # NumPy is optional; edge images may intentionally omit it.
    import numpy as _np  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised by the default CI image
    _np = None


EPSILON = 1e-12


def is_power_of_two(value: int) -> bool:
    """Return whether *value* is a positive power of two."""

    return value > 0 and value & (value - 1) == 0


def next_power_of_two(value: int) -> int:
    """Return the smallest power of two greater than or equal to *value*."""

    if value < 1:
        return 1
    return 1 << (value - 1).bit_length()


@lru_cache(maxsize=32)
def hann_window(size: int) -> tuple[float, ...]:
    """Return a periodic Hann analysis window."""

    if size < 2:
        raise ValueError("window size must be at least 2")
    # The symmetric form is appropriate for finite, frame-wise analysis.
    return tuple(0.5 - 0.5 * math.cos((2.0 * math.pi * index) / (size - 1)) for index in range(size))


def fft(values: Sequence[complex]) -> list[complex]:
    """Compute a radix-2, forward discrete Fourier transform.

    This is deliberately compact rather than a replacement for NumPy.  It
    exists to make the reference extraction pipeline runnable on a fresh Python
    installation and on low-resource device test images.
    """

    size = len(values)
    if not is_power_of_two(size):
        raise ValueError("FFT input length must be a power of two")

    result = [complex(value) for value in values]
    # Bit-reversal permutation.
    swap_index = 0
    for index in range(1, size):
        bit = size >> 1
        while swap_index & bit:
            swap_index ^= bit
            bit >>= 1
        swap_index ^= bit
        if index < swap_index:
            result[index], result[swap_index] = result[swap_index], result[index]

    span = 2
    while span <= size:
        half_span = span // 2
        twiddle_step = cmath.exp((-2j * math.pi) / span)
        for start in range(0, size, span):
            twiddle = 1 + 0j
            for offset in range(half_span):
                even = result[start + offset]
                odd = twiddle * result[start + offset + half_span]
                result[start + offset] = even + odd
                result[start + offset + half_span] = even - odd
                twiddle *= twiddle_step
        span <<= 1
    return result


def rfft_magnitudes(frame: Sequence[float]) -> list[float]:
    """Return magnitudes for the non-negative spectrum of a real frame."""

    if not is_power_of_two(len(frame)):
        raise ValueError("analysis frame must have a power-of-two length")
    if _np is not None:
        spectrum = _np.fft.rfft(_np.asarray(frame, dtype=float))
        return _np.abs(spectrum).astype(float).tolist()
    transformed = fft([complex(value, 0.0) for value in frame])
    return [abs(value) for value in transformed[: len(frame) // 2 + 1]]


def iter_frames(
    samples: Sequence[float],
    window_size: int,
    hop_size: int,
    *,
    pad_end: bool = True,
) -> Iterator[tuple[float, ...]]:
    """Yield fixed-length frames, optionally zero-padding the tail.

    At least one frame is emitted for a non-empty input shorter than the window.
    This behaviour is helpful for short radio IDs and station jingles.
    """

    if window_size < 2 or not is_power_of_two(window_size):
        raise ValueError("window_size must be a power of two and at least 2")
    if hop_size < 1:
        raise ValueError("hop_size must be positive")
    if not samples:
        return

    sample_count = len(samples)
    start = 0
    while start < sample_count:
        frame = tuple(samples[start : start + window_size])
        if len(frame) < window_size:
            if not pad_end:
                break
            frame += (0.0,) * (window_size - len(frame))
        yield frame
        if start + window_size >= sample_count and not pad_end:
            break
        start += hop_size
        # Without this condition, tail padding could create an unbounded stream.
        if start >= sample_count:
            break


def stft_magnitudes(
    samples: Sequence[float],
    window_size: int,
    hop_size: int,
    *,
    pad_end: bool = True,
) -> list[list[float]]:
    """Calculate a windowed magnitude spectrogram."""

    window = hann_window(window_size)
    result: list[list[float]] = []
    for frame in iter_frames(samples, window_size, hop_size, pad_end=pad_end):
        windowed = [sample * coefficient for sample, coefficient in zip(frame, window, strict=True)]
        result.append(rfft_magnitudes(windowed))
    return result


def resample_linear(
    samples: Sequence[float], original_rate: int, target_rate: int
) -> tuple[float, ...]:
    """Resample mono PCM using deterministic linear interpolation.

    FFmpeg should perform production-quality capture resampling.  This helper is
    only used by the reference CLI and tests, where a dependency-free method is
    more valuable than high-order filtering.
    """

    if original_rate <= 0 or target_rate <= 0:
        raise ValueError("sample rates must be positive")
    if not samples or original_rate == target_rate:
        return tuple(float(sample) for sample in samples)

    output_count = max(1, round(len(samples) * target_rate / original_rate))
    rate_ratio = original_rate / target_rate
    output: list[float] = []
    last_index = len(samples) - 1
    for output_index in range(output_count):
        source_position = output_index * rate_ratio
        left_index = min(int(source_position), last_index)
        right_index = min(left_index + 1, last_index)
        fraction = source_position - left_index
        output.append(
            float(samples[left_index]) * (1.0 - fraction) + float(samples[right_index]) * fraction
        )
    return tuple(output)


def amplitude_to_db(amplitude: float) -> float:
    """Convert an amplitude to decibels with a finite silence floor."""

    return 20.0 * math.log10(max(abs(amplitude), EPSILON))


def root_mean_square(values: Iterable[float]) -> float:
    """Return RMS amplitude, treating an empty input as silence."""

    values_tuple = tuple(values)
    if not values_tuple:
        return 0.0
    return math.sqrt(sum(value * value for value in values_tuple) / len(values_tuple))


def median(values: Sequence[float]) -> float:
    """Return the median of a non-empty finite sequence."""

    if not values:
        raise ValueError("median requires at least one value")
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[midpoint])
    return float((ordered[midpoint - 1] + ordered[midpoint]) / 2.0)


def median_absolute_deviation(values: Sequence[float]) -> float:
    """Return an unscaled median absolute deviation."""

    centre = median(values)
    return median([abs(value - centre) for value in values])


def moving_average(values: Sequence[float], radius: int) -> list[float]:
    """Smooth values with a centred moving average and clipped edge windows."""

    if radius < 0:
        raise ValueError("radius cannot be negative")
    if not values:
        return []
    smoothed: list[float] = []
    for index in range(len(values)):
        start = max(0, index - radius)
        end = min(len(values), index + radius + 1)
        smoothed.append(sum(values[start:end]) / (end - start))
    return smoothed
