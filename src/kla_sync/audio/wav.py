"""Minimal PCM WAV I/O for edge-side reference processing.

The ingestion service should use FFmpeg to turn radio streams into mono,
fixed-rate PCM WAV chunks. Keeping this reader limited to uncompressed PCM makes
its failure mode clear and avoids silently fingerprinting an incorrectly decoded
file.
"""

from __future__ import annotations

import struct
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .dsp import resample_linear


@dataclass(frozen=True)
class AudioBuffer:
    """Normalized mono samples and their sample rate."""

    samples: tuple[float, ...]
    sample_rate: int

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")

    @property
    def duration_seconds(self) -> float:
        return len(self.samples) / self.sample_rate

    def resampled(self, target_rate: int) -> AudioBuffer:
        """Return a linearly resampled buffer, preserving mono normalization."""

        return AudioBuffer(resample_linear(self.samples, self.sample_rate, target_rate), target_rate)


def _decode_pcm(raw: bytes, sample_width: int) -> list[float]:
    if sample_width == 1:
        return [(value - 128) / 128.0 for value in raw]
    if sample_width == 2:
        count = len(raw) // 2
        return [value / 32768.0 for value in struct.unpack(f"<{count}h", raw)]
    if sample_width == 3:
        decoded: list[float] = []
        for offset in range(0, len(raw) - 2, 3):
            value = raw[offset] | (raw[offset + 1] << 8) | (raw[offset + 2] << 16)
            if value & 0x800000:
                value -= 1 << 24
            decoded.append(value / 8388608.0)
        return decoded
    if sample_width == 4:
        count = len(raw) // 4
        return [value / 2147483648.0 for value in struct.unpack(f"<{count}i", raw)]
    raise ValueError(f"unsupported PCM sample width: {sample_width} bytes")


def read_wav_mono(path: str | Path, *, target_rate: int | None = None) -> AudioBuffer:
    """Read an uncompressed PCM WAV file, averaging all channels to mono.

    Args:
        path: Local WAV capture path.
        target_rate: Optional rate for deterministic edge-side resampling.

    Raises:
        ValueError: if the file is compressed, malformed, or not PCM-compatible.
    """

    wav_path = Path(path)
    with wave.open(str(wav_path), "rb") as reader:
        if reader.getcomptype() != "NONE":
            raise ValueError("only uncompressed PCM WAV is supported; decode with FFmpeg first")
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        sample_rate = reader.getframerate()
        if channels < 1:
            raise ValueError("WAV file reports no audio channels")
        raw = reader.readframes(reader.getnframes())

    decoded = _decode_pcm(raw, sample_width)
    if len(decoded) % channels:
        raise ValueError("WAV sample data does not contain complete channel frames")
    mono = tuple(
        sum(decoded[offset : offset + channels]) / channels
        for offset in range(0, len(decoded), channels)
    )
    buffer = AudioBuffer(mono, sample_rate)
    return buffer.resampled(target_rate) if target_rate and target_rate != sample_rate else buffer


def write_wav_mono(path: str | Path, samples: Sequence[float], sample_rate: int) -> None:
    """Write normalized samples as 16-bit mono PCM WAV.

    This is intentionally a test/diagnostic utility; capture writers should use
    FFmpeg so stream reconnect and codec handling remain centralized.
    """

    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    encoded = bytearray()
    for sample in samples:
        clamped = max(-1.0, min(1.0, float(sample)))
        encoded.extend(struct.pack("<h", round(clamped * 32767.0)))
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(bytes(encoded))
