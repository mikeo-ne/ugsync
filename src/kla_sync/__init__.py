"""KLA-Sync core domain primitives.

The package intentionally keeps its reference algorithms dependency-light so that
an edge listener can run with a small Python installation. Production workers
should install the ``production`` extra for NumPy and service adapters.
"""

from .audio.fingerprint import FingerprintConfig, FingerprintExtractor, InMemoryFingerprintIndex
from .royalties.calculator import RoyaltyCalculator

__all__ = [
    "FingerprintConfig",
    "FingerprintExtractor",
    "InMemoryFingerprintIndex",
    "RoyaltyCalculator",
]

__version__ = "0.1.0"
